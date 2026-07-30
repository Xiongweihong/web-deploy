from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

TARGET = "https://foundationcapital.com/portfolio"
OUT = Path("artifact")
OUT.mkdir(parents=True, exist_ok=True)
UA = "Mozilla/5.0 (compatible; FoundationCapitalPortfolioAudit/1.0)"


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


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
            "headers": dict(response.headers),
        })
        time.sleep(2)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=UA, viewport={"width": 1440, "height": 1200})
        page = context.new_page()
        network = []

        def on_response(response):
            request = response.request
            if request.resource_type in {"xhr", "fetch"}:
                item = {
                    "url": response.url,
                    "status": response.status,
                    "method": request.method,
                    "post_data": request.post_data,
                    "content_type": response.headers.get("content-type"),
                }
                network.append(item)

        page.on("response", on_response)
        response = page.goto(TARGET, wait_until="domcontentloaded", timeout=120_000)
        try:
            page.wait_for_load_state("networkidle", timeout=30_000)
        except Exception:
            pass
        page.wait_for_timeout(1500)

        # Dismiss common consent/dialog overlays where possible.
        for label in ["Accept All", "Accept all", "Accept", "Allow all", "Close"]:
            locator = page.get_by_role("button", name=re.compile(rf"^{re.escape(label)}$", re.I))
            for i in range(locator.count()):
                try:
                    if locator.nth(i).is_visible():
                        locator.nth(i).click(timeout=3000)
                        page.wait_for_timeout(300)
                except Exception:
                    pass

        def section_snapshot():
            return page.evaluate(
                r"""
                () => {
                  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
                  const heading = [...document.querySelectorAll('h1,h2,h3,h4,h5,h6')]
                    .find(el => /^All companies$/i.test(norm(el.innerText || el.textContent)));
                  if (!heading) throw new Error('All companies heading not found');
                  let root = heading.closest('section') || heading.parentElement;
                  // Prefer the nearest ancestor that contains the search input and Load More.
                  let cur = heading.parentElement;
                  while (cur && cur !== document.body) {
                    const hasSearch = [...cur.querySelectorAll('input')].some(input => /search companies/i.test(input.placeholder || ''));
                    const hasLoadMore = [...cur.querySelectorAll('*')].some(el => /^Load More$/i.test(norm(el.innerText || el.textContent)));
                    if (hasSearch || hasLoadMore) root = cur;
                    cur = cur.parentElement;
                  }
                  const headings = [...root.querySelectorAll('h3')]
                    .map(el => norm(el.innerText || el.textContent))
                    .filter(Boolean);
                  const uniqueHeadings = [...new Set(headings)];
                  const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
                  const loadMore = [...root.querySelectorAll('*')]
                    .filter(el => visible(el) && /^Load More$/i.test(norm(el.innerText || el.textContent)));
                  return {
                    rootTag: root.tagName,
                    rootId: root.id || '',
                    rootClass: typeof root.className === 'string' ? root.className : '',
                    h3Count: headings.length,
                    uniqueH3Count: uniqueHeadings.length,
                    firstHeadings: uniqueHeadings.slice(0, 20),
                    lastHeadings: uniqueHeadings.slice(-20),
                    loadMoreVisibleCount: loadMore.length,
                  };
                }
                """
            )

        click_log = []
        for iteration in range(200):
            before = section_snapshot()
            if before["loadMoreVisibleCount"] == 0:
                break
            result = page.evaluate(
                r"""
                () => {
                  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
                  const heading = [...document.querySelectorAll('h1,h2,h3,h4,h5,h6')]
                    .find(el => /^All companies$/i.test(norm(el.innerText || el.textContent)));
                  let root = heading.closest('section') || heading.parentElement;
                  let cur = heading.parentElement;
                  while (cur && cur !== document.body) {
                    const hasSearch = [...cur.querySelectorAll('input')].some(input => /search companies/i.test(input.placeholder || ''));
                    const hasLoadMore = [...cur.querySelectorAll('*')].some(el => /^Load More$/i.test(norm(el.innerText || el.textContent)));
                    if (hasSearch || hasLoadMore) root = cur;
                    cur = cur.parentElement;
                  }
                  const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
                  const candidates = [...root.querySelectorAll('button,a,[role="button"],div')]
                    .filter(el => visible(el) && /^Load More$/i.test(norm(el.innerText || el.textContent)));
                  if (!candidates.length) return {clicked: false};
                  const target = candidates[candidates.length - 1];
                  target.scrollIntoView({block: 'center'});
                  target.click();
                  return {clicked: true, tag: target.tagName, className: typeof target.className === 'string' ? target.className : ''};
                }
                """
            )
            page.wait_for_timeout(1200)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            after = section_snapshot()
            click_log.append({"iteration": iteration + 1, "before": before, "after": after, "click": result})
            if not result.get("clicked") or after["uniqueH3Count"] <= before["uniqueH3Count"]:
                break

        analysis = page.evaluate(
            r"""
            () => {
              const norm = s => (s || '').replace(/\s+/g, ' ').trim();
              const heading = [...document.querySelectorAll('h1,h2,h3,h4,h5,h6')]
                .find(el => /^All companies$/i.test(norm(el.innerText || el.textContent)));
              if (!heading) throw new Error('All companies heading not found');
              let root = heading.closest('section') || heading.parentElement;
              let cur = heading.parentElement;
              while (cur && cur !== document.body) {
                const hasSearch = [...cur.querySelectorAll('input')].some(input => /search companies/i.test(input.placeholder || ''));
                const hasLoadMore = [...cur.querySelectorAll('*')].some(el => /^Load More$/i.test(norm(el.innerText || el.textContent)));
                if (hasSearch || hasLoadMore) root = cur;
                cur = cur.parentElement;
              }
              const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
              const h3s = [...root.querySelectorAll('h3')];
              const records = h3s.map((h3, index) => {
                const name = norm(h3.innerText || h3.textContent);
                let container = h3.parentElement;
                while (container && container !== root) {
                  const containedH3 = container.querySelectorAll('h3').length;
                  const links = [...container.querySelectorAll('a[href]')];
                  if (containedH3 === 1 && links.length) break;
                  container = container.parentElement;
                }
                container = container || h3.parentElement;
                const links = [...container.querySelectorAll('a[href]')].map(a => ({
                  href: a.href,
                  text: norm(a.innerText || a.textContent),
                  target: a.target || '',
                  rel: a.rel || '',
                  className: typeof a.className === 'string' ? a.className : '',
                }));
                return {
                  index,
                  name,
                  links,
                  containerTag: container?.tagName || '',
                  containerId: container?.id || '',
                  containerClass: typeof container?.className === 'string' ? container.className : '',
                  containerText: norm(container?.innerText || container?.textContent).slice(0, 1000),
                  containerHtml: (container?.outerHTML || '').slice(0, 10000),
                };
              });
              const uniqueNames = [...new Set(records.map(r => r.name))];
              const loadMoreVisible = [...root.querySelectorAll('*')]
                .filter(el => visible(el) && /^Load More$/i.test(norm(el.innerText || el.textContent))).length;
              return {
                pageTitle: document.title,
                rootHtmlPreview: root.outerHTML.slice(0, 20000),
                recordCount: records.length,
                uniqueNameCount: uniqueNames.length,
                duplicateNames: Object.entries(records.reduce((acc, r) => { acc[r.name] = (acc[r.name] || 0) + 1; return acc; }, {})).filter(([,count]) => count > 1),
                recordsFirst: records.slice(0, 20),
                recordsLast: records.slice(-20),
                allRecords: records,
                loadMoreVisibleAtEnd: loadMoreVisible,
                bodyTextPreview: norm(document.body.innerText).slice(0, 10000),
              };
            }
            """
        )

        html = page.content()
        (OUT / "browser-expanded.html").write_text(html, encoding="utf-8")
        page.screenshot(path=str(OUT / "browser-expanded.png"), full_page=True)
        browser.close()

    summary = {
        "static": static,
        "static_hashes_equal": static[0]["sha256"] == static[1]["sha256"],
        "browser_status": response.status if response else None,
        "click_log": click_log,
        "network": network,
        "analysis": analysis,
    }
    write_json(OUT / "discovery.json", summary)
    print("FOUNDATION_DISCOVERY_START")
    print(json.dumps({
        "static": static,
        "static_hashes_equal": summary["static_hashes_equal"],
        "browser_status": summary["browser_status"],
        "click_iterations": len(click_log),
        "record_count": analysis["recordCount"],
        "unique_name_count": analysis["uniqueNameCount"],
        "duplicate_names": analysis["duplicateNames"],
        "load_more_visible_at_end": analysis["loadMoreVisibleAtEnd"],
        "first_names": [r["name"] for r in analysis["recordsFirst"][:10]],
        "last_names": [r["name"] for r in analysis["recordsLast"][-10:]],
        "network_count": len(network),
    }, ensure_ascii=False, indent=2))
    print("FOUNDATION_DISCOVERY_END")


if __name__ == "__main__":
    main()
