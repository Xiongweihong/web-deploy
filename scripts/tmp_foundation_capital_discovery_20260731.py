from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

TARGET = "https://foundationcapital.com/portfolio"
OUT = Path("artifact")
OUT.mkdir(parents=True, exist_ok=True)
UA = "Mozilla/5.0 (compatible; FoundationCapitalPortfolioAudit/1.0)"


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def source_declared_count(raw: bytes) -> int | None:
    soup = BeautifulSoup(raw, "lxml")
    script = soup.find("script", id="__framer__handoverData")
    if not script or not script.string:
        return None
    data = json.loads(script.string)
    if not isinstance(data, list) or len(data) < 4 or not isinstance(data[1], list) or data[1][0] != "Map":
        return None
    map_items = data[1][1:]
    pairs = list(zip(map_items[0::2], map_items[1::2]))
    for query_index, result_index in pairs:
        query = data[query_index]
        result = data[result_index]
        if isinstance(query, str) and '"from":"695a2444-eada-42cb-ab20-a0b3fbc1567e"' in query and '"select":[]' in query:
            if isinstance(result, list):
                return len(result)
    return None


def main() -> None:
    static = []
    raws = []
    for index in (1, 2):
        response = requests.get(TARGET, headers={"User-Agent": UA}, timeout=120)
        response.raise_for_status()
        raw = response.content
        raws.append(raw)
        (OUT / f"static-{index}.html").write_bytes(raw)
        static.append({
            "index": index,
            "status": response.status_code,
            "final_url": response.url,
            "bytes": len(raw),
            "sha256": sha256(raw),
            "declared_count": source_declared_count(raw),
            "headers": dict(response.headers),
        })
        time.sleep(2)

    expected_count = static[0]["declared_count"]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=UA, viewport={"width": 1440, "height": 1200})
        page = context.new_page()
        network = []

        def on_response(response):
            request = response.request
            if request.resource_type in {"xhr", "fetch"}:
                network.append({
                    "url": response.url,
                    "status": response.status,
                    "method": request.method,
                    "post_data": request.post_data,
                    "content_type": response.headers.get("content-type"),
                })

        page.on("response", on_response)
        response = page.goto(TARGET, wait_until="domcontentloaded", timeout=120_000)
        try:
            page.wait_for_load_state("networkidle", timeout=30_000)
        except Exception:
            pass
        page.wait_for_timeout(1200)

        def visible_company_count() -> int:
            return page.evaluate(
                """
                () => [...document.querySelectorAll('#companies [data-framer-name="Company Listing"]')]
                  .filter(el => el.offsetWidth || el.offsetHeight || el.getClientRects().length).length
                """
            )

        def visible_load_more_count() -> int:
            return page.evaluate(
                """
                () => {
                  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
                  return [...document.querySelectorAll('#all-companies button')]
                    .filter(el => (el.offsetWidth || el.offsetHeight || el.getClientRects().length) && /^Load More$/i.test(norm(el.innerText || el.textContent))).length;
                }
                """
            )

        click_log = []
        for iteration in range(100):
            before = visible_company_count()
            visible_buttons = visible_load_more_count()
            if visible_buttons == 0 or (expected_count and before >= expected_count):
                break
            button = page.locator('#all-companies button').filter(has_text=re.compile(r'^\s*Load More\s*$', re.I)).last
            button.scroll_into_view_if_needed(timeout=10_000)
            button.click(force=True, timeout=15_000)
            deadline = time.time() + 12
            after = before
            while time.time() < deadline:
                page.wait_for_timeout(250)
                after = visible_company_count()
                if after > before:
                    break
            click_log.append({"iteration": iteration + 1, "before": before, "after": after})
            if after <= before:
                break

        final_count = visible_company_count()
        final_load_more = visible_load_more_count()

        records = page.evaluate(
            """
            () => {
              const norm = s => (s || '').replace(/\s+/g, ' ').trim();
              return [...document.querySelectorAll('#companies [data-framer-name="Company Listing"]')]
                .filter(el => el.offsetWidth || el.offsetHeight || el.getClientRects().length)
                .map((el, index) => ({
                  index,
                  name: norm(el.querySelector('h3')?.innerText || el.querySelector('h3')?.textContent),
                  className: typeof el.className === 'string' ? el.className : '',
                  html: el.outerHTML.slice(0, 5000),
                }));
            }
            """
        )

        expanded_samples = []
        cards = page.locator('#companies [data-framer-name="Company Listing"]:visible')
        for index in range(min(cards.count(), 5)):
            card = cards.nth(index)
            name = card.locator('h3').first.inner_text().strip()
            before_html = card.evaluate("el => el.outerHTML")
            card.click(force=True, timeout=10_000)
            page.wait_for_timeout(500)
            wrapper = card.locator("xpath=ancestor::div[starts-with(@class,'framer-1mjpqh6')][1]")
            if wrapper.count() == 0:
                wrapper = card
            info = wrapper.evaluate(
                """
                el => {
                  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
                  return {
                    text: norm(el.innerText || el.textContent),
                    links: [...el.querySelectorAll('a[href]')].map(a => ({href: a.href, text: norm(a.innerText || a.textContent)})),
                    html: el.outerHTML.slice(0, 20000),
                  };
                }
                """
            )
            expanded_samples.append({"index": index, "name": name, "before_html": before_html[:5000], "after": info})
            try:
                card.click(force=True, timeout=5000)
                page.wait_for_timeout(150)
            except Exception:
                pass

        html = page.content()
        (OUT / "browser-expanded.html").write_text(html, encoding="utf-8")
        page.screenshot(path=str(OUT / "browser-expanded.png"), full_page=True)
        browser.close()

    summary = {
        "static": static,
        "static_hashes_equal": static[0]["sha256"] == static[1]["sha256"],
        "declared_counts_equal": static[0]["declared_count"] == static[1]["declared_count"],
        "browser_status": response.status if response else None,
        "expected_count": expected_count,
        "click_log": click_log,
        "final_count": final_count,
        "final_load_more_visible": final_load_more,
        "records": records,
        "expanded_samples": expanded_samples,
        "network": network,
    }
    write_json(OUT / "discovery.json", summary)
    print("FOUNDATION_DISCOVERY_START")
    print(json.dumps({
        "static": static,
        "static_hashes_equal": summary["static_hashes_equal"],
        "declared_counts_equal": summary["declared_counts_equal"],
        "expected_count": expected_count,
        "click_iterations": len(click_log),
        "count_sequence": ([click_log[0]["before"]] + [x["after"] for x in click_log]) if click_log else [final_count],
        "final_count": final_count,
        "unique_name_count": len({r["name"] for r in records}),
        "final_load_more_visible": final_load_more,
        "first_names": [r["name"] for r in records[:10]],
        "last_names": [r["name"] for r in records[-10:]],
        "expanded_samples": [{"name": x["name"], "links": x["after"]["links"], "text": x["after"]["text"][:500]} for x in expanded_samples],
        "network_count": len(network),
    }, ensure_ascii=False, indent=2))
    print("FOUNDATION_DISCOVERY_END")


if __name__ == "__main__":
    main()
