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
CMS_OUT = OUT / "cms_responses"
OUT.mkdir(parents=True, exist_ok=True)
CMS_OUT.mkdir(parents=True, exist_ok=True)
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
    for query_index, result_index in zip(map_items[0::2], map_items[1::2]):
        query = data[query_index]
        result = data[result_index]
        if isinstance(query, str) and '"from":"695a2444-eada-42cb-ab20-a0b3fbc1567e"' in query and '"select":[]' in query:
            if isinstance(result, list):
                return len(result)
    return None


def main() -> None:
    static = []
    for index in (1, 2):
        response = requests.get(TARGET, headers={"User-Agent": UA}, timeout=120)
        response.raise_for_status()
        raw = response.content
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
        saved_cms_urls: set[str] = set()

        def on_response(response):
            request = response.request
            if request.resource_type not in {"xhr", "fetch"}:
                return
            item = {
                "url": response.url,
                "status": response.status,
                "method": request.method,
                "post_data": request.post_data,
                "content_type": response.headers.get("content-type"),
            }
            if "framerusercontent.com/cms/" in response.url and response.url not in saved_cms_urls:
                saved_cms_urls.add(response.url)
                try:
                    body = response.body()
                    filename = hashlib.sha256(response.url.encode()).hexdigest()[:20] + ".framercms"
                    (CMS_OUT / filename).write_bytes(body)
                    item.update({"saved_file": filename, "body_bytes": len(body), "body_sha256": sha256(body)})
                except Exception as exc:
                    item["body_error"] = repr(exc)
            network.append(item)

        page.on("response", on_response)
        response = page.goto(TARGET, wait_until="domcontentloaded", timeout=120_000)
        try:
            page.wait_for_load_state("networkidle", timeout=30_000)
        except Exception:
            pass
        page.wait_for_timeout(1200)

        def visible_company_count() -> int:
            return page.evaluate("""() => [...document.querySelectorAll('#companies [data-framer-name="Company Listing"]')].filter(el => el.offsetWidth || el.offsetHeight || el.getClientRects().length).length""")

        def visible_load_more_count() -> int:
            return page.evaluate("""() => { const norm=s=>(s||'').replace(/\s+/g,' ').trim(); return [...document.querySelectorAll('#all-companies button')].filter(el => (el.offsetWidth||el.offsetHeight||el.getClientRects().length) && /^Load More$/i.test(norm(el.innerText||el.textContent))).length; }""")

        click_log = []
        for iteration in range(100):
            before = visible_company_count()
            if visible_load_more_count() == 0 or (expected_count and before >= expected_count):
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
        records = page.evaluate("""() => { const norm=s=>(s||'').replace(/\s+/g,' ').trim(); return [...document.querySelectorAll('#companies [data-framer-name="Company Listing"]')].filter(el=>el.offsetWidth||el.offsetHeight||el.getClientRects().length).map((el,index)=>({index,name:norm(el.querySelector('h3')?.innerText||el.querySelector('h3')?.textContent)})); }""")

        (OUT / "browser-expanded.html").write_text(page.content(), encoding="utf-8")
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
        "network": network,
        "cms_response_file_count": len(list(CMS_OUT.glob('*.framercms'))),
    }
    write_json(OUT / "discovery.json", summary)
    print("FOUNDATION_DISCOVERY_START")
    print(json.dumps({
        "static_hashes_equal": summary["static_hashes_equal"],
        "declared_counts_equal": summary["declared_counts_equal"],
        "expected_count": expected_count,
        "click_iterations": len(click_log),
        "count_sequence": ([click_log[0]["before"]] + [x["after"] for x in click_log]) if click_log else [final_count],
        "final_count": final_count,
        "unique_name_count": len({r["name"] for r in records}),
        "final_load_more_visible": final_load_more,
        "cms_response_file_count": summary["cms_response_file_count"],
        "cms_responses": [x for x in network if x.get("saved_file")],
    }, ensure_ascii=False, indent=2))
    print("FOUNDATION_DISCOVERY_END")


if __name__ == "__main__":
    main()
