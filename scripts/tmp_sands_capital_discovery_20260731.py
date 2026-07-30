from __future__ import annotations

import json
import re
from pathlib import Path

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

            # Remove overlays that intercept the site's own Load More control.
            page.evaluate(
                """
                () => {
                  for (const selector of ['#onetrust-consent-sdk', '#elementor-popup-modal-43621', '.notice-popup']) {
                    document.querySelectorAll(selector).forEach(el => el.remove());
                  }
                }
                """
            )

            clicks: list[dict[str, object]] = []
            for iteration in range(100):
                before = page.locator(".kurtosys-listing-grid__item .partnersTitle").count()
                clicked = page.evaluate(
                    """
                    () => {
                      const norm = s => (s || '').replace(/\s+/g, ' ').trim();
                      const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
                      const matches = [...document.querySelectorAll('button,a,span,div')]
                        .filter(el => visible(el) && /^Load More$/i.test(norm(el.innerText || el.textContent)));
                      if (!matches.length) return false;
                      const leaf = matches.find(el => ![...el.children].some(ch => /^Load More$/i.test(norm(ch.innerText || ch.textContent)))) || matches[0];
                      const button = leaf.closest('button,a,[role="button"],.elementor-button') || leaf;
                      button.click();
                      return true;
                    }
                    """
                )
                if not clicked:
                    break
                for _ in range(30):
                    page.wait_for_timeout(500)
                    after_now = page.locator(".kurtosys-listing-grid__item .partnersTitle").count()
                    if after_now > before:
                        break
                try:
                    page.wait_for_load_state("networkidle", timeout=10_000)
                except Exception:
                    pass
                after = page.locator(".kurtosys-listing-grid__item .partnersTitle").count()
                clicks.append({"iteration": iteration + 1, "company_before": before, "company_after": after})
                if after <= before:
                    break

            html = page.content()
            (OUT / f"{slug}.html").write_text(html, encoding="utf-8")
            page.screenshot(path=str(OUT / f"{slug}.png"), full_page=True)

            analysis = page.evaluate(
                """
                () => {
                  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
                  const records = [...document.querySelectorAll('.kurtosys-listing-grid__item')]
                    .map((item, index) => {
                      const name = norm(item.querySelector('.partnersTitle')?.innerText || item.querySelector('.partnersTitle')?.textContent);
                      if (!name) return null;
                      const websiteLinks = [...item.querySelectorAll('a[href]')]
                        .filter(a => /^Visit Website$/i.test(norm(a.innerText || a.textContent)));
                      const website = websiteLinks[0]?.href || '';
                      const statusEl = [...item.querySelectorAll('.kurtosys-listing-dynamic-field__content')]
                        .find(el => /^Status:/i.test(norm(el.innerText || el.textContent)));
                      const partnerEl = [...item.querySelectorAll('.kurtosys-listing-dynamic-field__content')]
                        .find(el => /^Partner Since:/i.test(norm(el.innerText || el.textContent)));
                      const categoryEl = [...item.querySelectorAll('.kurtosys-listing-dynamic-terms')]
                        .find(el => /^Category:/i.test(norm(el.innerText || el.textContent)));
                      return {
                        source_position: index + 1,
                        post_id: item.getAttribute('data-post-id') || '',
                        name,
                        website,
                        status: norm(statusEl?.innerText || statusEl?.textContent).replace(/^Status:\s*/i, ''),
                        category: norm(categoryEl?.innerText || categoryEl?.textContent).replace(/^Category:\s*/i, ''),
                        partner_since: norm(partnerEl?.innerText || partnerEl?.textContent).replace(/^Partner Since:\s*/i, ''),
                        website_link_count: websiteLinks.length,
                      };
                    }).filter(Boolean);
                  return {
                    title: document.title,
                    records,
                    companyCount: records.length,
                    uniquePostIds: new Set(records.map(r => r.post_id).filter(Boolean)).size,
                    uniqueNames: new Set(records.map(r => r.name.toLocaleLowerCase())).size,
                    nonemptyWebsites: records.filter(r => r.website).length,
                    loadMoreVisible: [...document.querySelectorAll('button,a,span,div')]
                      .some(el => (el.offsetWidth || el.offsetHeight || el.getClientRects().length) && /^Load More$/i.test(norm(el.innerText || el.textContent))),
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
        "companyCount": v["analysis"]["companyCount"],
        "uniquePostIds": v["analysis"]["uniquePostIds"],
        "uniqueNames": v["analysis"]["uniqueNames"],
        "nonemptyWebsites": v["analysis"]["nonemptyWebsites"],
        "loadMoreClicks": v["clicks"],
        "loadMoreVisibleAtEnd": v["analysis"]["loadMoreVisible"],
        "xhrCount": len(v["network"]),
    } for k, v in result.items()}, ensure_ascii=False, indent=2))
    print("SANDS_DISCOVERY_END")


if __name__ == "__main__":
    main()
