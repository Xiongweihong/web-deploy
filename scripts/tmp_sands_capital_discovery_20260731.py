from __future__ import annotations

import json
import re
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

OUT = Path("artifact")
OUT.mkdir(exist_ok=True)

PAGES = {
    "global-ventures": "https://www.sandscapital.com/global-ventures/",
    "life-sciences-pulse": "https://www.sandscapital.com/life-sciences-pulse/",
    "global-innovation": "https://www.sandscapital.com/global-innovation/",
}


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> None:
    result: dict[str, object] = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (compatible; SandsCapitalAudit/1.0)",
            viewport={"width": 1440, "height": 1200},
        )
        for slug, url in PAGES.items():
            page = context.new_page()
            network: list[dict[str, object]] = []

            def on_response(response):
                request = response.request
                if request.resource_type in {"xhr", "fetch"}:
                    network.append({
                        "url": response.url,
                        "status": response.status,
                        "method": request.method,
                        "resource_type": request.resource_type,
                        "post_data": request.post_data,
                        "content_type": response.headers.get("content-type"),
                    })

            page.on("response", on_response)
            response = page.goto(url, wait_until="domcontentloaded", timeout=120_000)
            try:
                page.wait_for_load_state("networkidle", timeout=30_000)
            except Exception:
                pass
            page.wait_for_timeout(1500)

            clicks: list[dict[str, object]] = []
            for iteration in range(100):
                visit_before = page.locator("a", has_text="Visit Website").count()
                load_candidates = page.get_by_text(re.compile(r"^\s*Load More\s*$", re.I))
                visible = []
                for i in range(load_candidates.count()):
                    loc = load_candidates.nth(i)
                    try:
                        if loc.is_visible():
                            visible.append(loc)
                    except Exception:
                        pass
                if not visible:
                    break
                target = visible[-1]
                try:
                    target.scroll_into_view_if_needed(timeout=10_000)
                    target.click(timeout=15_000)
                except Exception as exc:
                    clicks.append({"iteration": iteration + 1, "error": repr(exc), "visit_before": visit_before})
                    break
                page.wait_for_timeout(1500)
                try:
                    page.wait_for_load_state("networkidle", timeout=10_000)
                except Exception:
                    pass
                visit_after = page.locator("a", has_text="Visit Website").count()
                clicks.append({"iteration": iteration + 1, "visit_before": visit_before, "visit_after": visit_after})
                if visit_after <= visit_before:
                    break

            html = page.content()
            (OUT / f"{slug}.html").write_text(html, encoding="utf-8")
            page.screenshot(path=str(OUT / f"{slug}.png"), full_page=True)

            analysis = page.evaluate(
                """
                () => {
                  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
                  const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
                  const visitLinks = [...document.querySelectorAll('a[href]')]
                    .filter(a => /^Visit Website$/i.test(norm(a.innerText || a.textContent)));
                  const samples = visitLinks.slice(0, 8).map(a => {
                    const lineage = [];
                    let cur = a;
                    for (let i = 0; i < 9 && cur; i++, cur = cur.parentElement) {
                      lineage.push({
                        depth: i,
                        tag: cur.tagName,
                        id: cur.id || '',
                        className: typeof cur.className === 'string' ? cur.className : '',
                        visitCount: [...cur.querySelectorAll('a[href]')].filter(x => /^Visit Website$/i.test(norm(x.innerText || x.textContent))).length,
                        showDetailsCount: [...cur.querySelectorAll('*')].filter(x => /^Show details$/i.test(norm(x.innerText || x.textContent))).length,
                        text: norm(cur.innerText || cur.textContent).slice(0, 1200),
                        html: cur.outerHTML.slice(0, 6000),
                      });
                    }
                    return {href: a.href, text: norm(a.innerText || a.textContent), lineage};
                  });
                  return {
                    title: document.title,
                    visitWebsiteCount: visitLinks.length,
                    visitWebsiteHrefs: visitLinks.map(a => a.href),
                    visitWebsiteSamples: samples,
                    visibleLoadMoreCount: [...document.querySelectorAll('*')].filter(el => visible(el) && /^Load More$/i.test(norm(el.innerText || el.textContent))).length,
                    bodyTextPreview: norm(document.body.innerText).slice(0, 5000),
                  };
                }
                """
            )
            result[slug] = {
                "url": page.url,
                "status": response.status if response else None,
                "clicks": clicks,
                "network": network,
                "analysis": analysis,
            }
            write_json(OUT / f"{slug}-discovery.json", result[slug])
            page.close()
        browser.close()

    write_json(OUT / "discovery-summary.json", result)
    print("SANDS_DISCOVERY_START")
    print(json.dumps({k: {
        "status": v["status"],
        "visitWebsiteCount": v["analysis"]["visitWebsiteCount"],
        "loadMoreClicks": len(v["clicks"]),
        "xhrCount": len(v["network"]),
    } for k, v in result.items()}, ensure_ascii=False, indent=2))
    print("SANDS_DISCOVERY_END")


if __name__ == "__main__":
    main()
