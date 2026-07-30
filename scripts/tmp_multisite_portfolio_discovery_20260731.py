from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

OUT = Path("artifact")
OUT.mkdir(parents=True, exist_ok=True)
UA = "Mozilla/5.0 (compatible; PublicPortfolioAudit/1.0)"

TARGETS = {
    "iconiq": "https://www.iconiq.com/growth/companies",
    "general-catalyst": "https://www.generalcatalyst.com/portfolio",
    "index-ventures": "https://www.indexventures.com/companies/",
    "capitalg-portfolio": "https://capitalg.com/portfolio/",
    "capitalg-companies": "https://careers.capitalg.com/companies",
    "bcv-portfolio": "https://baincapitalventures.com/portfolio/",
    "bcv-companies": "https://jobs.baincapitalventures.com/companies",
    "cvs-health-ventures": "https://www.cvshealthventures.com/portfolio.html",
    "hanabi": "https://www.hanabi.com/companies",
    "a-star": "https://www.a-star.co/companies",
    "a-capital": "https://acapital.com/portfolio",
}

CLICK_PATTERNS = [
    r"^Load More$",
    r"^Load more$",
    r"^Show More$",
    r"^Show more$",
    r"^See More$",
    r"^See more$",
    r"^View More$",
    r"^View more$",
    r"^View all Companies$",
    r"^View all companies$",
    r"^See all companies$",
    r"^Show all$",
]


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def safe_name(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-")
    return value[:150] or "response"


def dismiss_overlays(page) -> list[str]:
    clicked = []
    labels = [
        "Accept All", "Accept all", "Accept", "Allow all", "I Accept",
        "Agree", "Got it", "Continue", "Close", "Dismiss",
    ]
    for label in labels:
        locator = page.get_by_role("button", name=re.compile(rf"^{re.escape(label)}$", re.I))
        for index in range(locator.count()):
            try:
                if locator.nth(index).is_visible():
                    locator.nth(index).click(timeout=2500, force=True)
                    clicked.append(label)
                    page.wait_for_timeout(200)
            except Exception:
                pass
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    return clicked


def visible_text_count(page) -> dict[str, int]:
    return page.evaluate(
        r"""
        () => {
          const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
          const norm = s => (s || '').replace(/\s+/g, ' ').trim();
          const headings = [...document.querySelectorAll('h1,h2,h3,h4,h5,h6')].filter(visible);
          const anchors = [...document.querySelectorAll('a[href]')].filter(visible);
          return {
            visible_headings: headings.length,
            unique_heading_texts: new Set(headings.map(x => norm(x.innerText || x.textContent)).filter(Boolean)).size,
            visible_anchors: anchors.length,
            unique_anchor_hrefs: new Set(anchors.map(x => x.href)).size,
            body_chars: norm(document.body?.innerText || '').length,
          };
        }
        """
    )


def click_expand_controls(page) -> list[dict]:
    logs: list[dict] = []
    for iteration in range(120):
        before = visible_text_count(page)
        candidate = page.evaluate(
            r"""
            (patterns) => {
              const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
              const norm = s => (s || '').replace(/\s+/g, ' ').trim();
              const regexes = patterns.map(p => new RegExp(p, 'i'));
              const elements = [...document.querySelectorAll('button,a,[role="button"]')]
                .filter(visible)
                .filter(el => regexes.some(re => re.test(norm(el.innerText || el.textContent))));
              if (!elements.length) return null;
              const el = elements[elements.length - 1];
              const text = norm(el.innerText || el.textContent);
              el.scrollIntoView({block: 'center'});
              el.click();
              return {tag: el.tagName, text, href: el.href || '', className: typeof el.className === 'string' ? el.className : ''};
            }
            """,
            CLICK_PATTERNS,
        )
        if not candidate:
            break
        page.wait_for_timeout(900)
        try:
            page.wait_for_load_state("networkidle", timeout=6000)
        except Exception:
            pass
        after = visible_text_count(page)
        logs.append({"iteration": iteration + 1, "before": before, "after": after, "clicked": candidate})
        grew = any(after.get(key, 0) > before.get(key, 0) for key in ["unique_heading_texts", "unique_anchor_hrefs", "body_chars"])
        if not grew:
            break
    return logs


def scroll_to_bottom(page) -> list[int]:
    heights = []
    stable = 0
    previous = -1
    for _ in range(80):
        height = page.evaluate("document.documentElement.scrollHeight")
        heights.append(height)
        page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
        page.wait_for_timeout(350)
        new_height = page.evaluate("document.documentElement.scrollHeight")
        if new_height == previous == height:
            stable += 1
        else:
            stable = 0
        previous = new_height
        if stable >= 3:
            break
    return heights


def extract_dom(page) -> dict:
    return page.evaluate(
        r"""
        () => {
          const norm = s => (s || '').replace(/\s+/g, ' ').trim();
          const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
          const anchors = [...document.querySelectorAll('a[href]')].map((a, index) => ({
            index,
            href: a.href,
            text: norm(a.innerText || a.textContent),
            visible: visible(a),
            target: a.target || '',
            rel: a.rel || '',
            className: typeof a.className === 'string' ? a.className : '',
            parentText: norm(a.parentElement?.innerText || a.parentElement?.textContent).slice(0, 500),
            html: a.outerHTML.slice(0, 2000),
          }));
          const headings = [...document.querySelectorAll('h1,h2,h3,h4,h5,h6')].map((h, index) => ({
            index,
            tag: h.tagName,
            text: norm(h.innerText || h.textContent),
            visible: visible(h),
            className: typeof h.className === 'string' ? h.className : '',
            parentText: norm(h.parentElement?.innerText || h.parentElement?.textContent).slice(0, 1000),
            parentHtml: (h.parentElement?.outerHTML || '').slice(0, 5000),
          }));
          const scripts = [...document.querySelectorAll('script')].map((s, index) => ({
            index,
            src: s.src || '',
            type: s.type || '',
            id: s.id || '',
            text: (s.textContent || '').slice(0, 20000),
          })).filter(x => x.src || /company|portfolio|graphql|api|algolia|search|getro|consider/i.test(x.text));
          return {
            title: document.title,
            url: location.href,
            anchors,
            headings,
            scripts,
            bodyText: norm(document.body?.innerText || ''),
            bodyHtmlPreview: (document.body?.outerHTML || '').slice(0, 100000),
          };
        }
        """
    )


def main() -> None:
    summary: dict[str, object] = {}
    static_session = requests.Session()
    static_session.headers.update({"User-Agent": UA, "Accept": "text/html,application/json;q=0.9,*/*;q=0.8"})

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=UA, viewport={"width": 1440, "height": 1100})
        for slug, url in TARGETS.items():
            site_dir = OUT / slug
            site_dir.mkdir(parents=True, exist_ok=True)
            static_info = {}
            try:
                response = static_session.get(url, timeout=120, allow_redirects=True)
                raw = response.content
                (site_dir / "static.html").write_bytes(raw)
                static_info = {
                    "status": response.status_code,
                    "requested_url": url,
                    "final_url": response.url,
                    "bytes": len(raw),
                    "sha256": sha256(raw),
                    "headers": dict(response.headers),
                    "title": BeautifulSoup(raw, "lxml").title.get_text(" ", strip=True) if raw else None,
                }
            except Exception as exc:
                static_info = {"requested_url": url, "error": repr(exc)}

            page = context.new_page()
            network_meta: list[dict] = []
            saved_responses: list[dict] = []
            response_counter = 0

            def on_response(resp):
                nonlocal response_counter
                req = resp.request
                ctype = (resp.headers.get("content-type") or "").lower()
                item = {
                    "url": resp.url,
                    "status": resp.status,
                    "method": req.method,
                    "resource_type": req.resource_type,
                    "post_data": req.post_data,
                    "content_type": ctype,
                }
                network_meta.append(item)
                if req.resource_type in {"xhr", "fetch"} and any(token in ctype for token in ["json", "graphql", "text/plain", "octet-stream"]):
                    try:
                        body = resp.body()
                        if len(body) <= 8_000_000:
                            response_counter += 1
                            extension = ".json" if "json" in ctype else ".bin"
                            filename = f"response-{response_counter:04d}-{safe_name(urlparse(resp.url).path)}{extension}"
                            (site_dir / filename).write_bytes(body)
                            saved_responses.append({"filename": filename, "url": resp.url, "bytes": len(body), "sha256": sha256(body), "content_type": ctype})
                    except Exception as exc:
                        item["body_error"] = repr(exc)

            page.on("response", on_response)
            browser_info = {}
            try:
                nav = page.goto(url, wait_until="domcontentloaded", timeout=120_000)
                try:
                    page.wait_for_load_state("networkidle", timeout=25_000)
                except Exception:
                    pass
                page.wait_for_timeout(1200)
                dismissed = dismiss_overlays(page)
                scroll_before = scroll_to_bottom(page)
                expand_logs = click_expand_controls(page)
                scroll_after = scroll_to_bottom(page)
                # One more expansion pass catches controls revealed by scrolling.
                expand_logs_2 = click_expand_controls(page)
                page.wait_for_timeout(800)
                dom = extract_dom(page)
                (site_dir / "browser.html").write_text(page.content(), encoding="utf-8")
                page.screenshot(path=str(site_dir / "browser.png"), full_page=True)
                write_json(site_dir / "dom.json", dom)
                write_json(site_dir / "network.json", network_meta)
                browser_info = {
                    "status": nav.status if nav else None,
                    "final_url": page.url,
                    "dismissed": dismissed,
                    "scroll_before": scroll_before,
                    "expand_logs": expand_logs + expand_logs_2,
                    "scroll_after": scroll_after,
                    "dom_anchor_count": len(dom["anchors"]),
                    "dom_heading_count": len(dom["headings"]),
                    "visible_heading_text_count": len({x["text"] for x in dom["headings"] if x["visible"] and x["text"]}),
                    "saved_response_count": len(saved_responses),
                }
            except Exception as exc:
                browser_info = {"error": repr(exc), "final_url": page.url}
                try:
                    (site_dir / "browser-failure.html").write_text(page.content(), encoding="utf-8")
                    page.screenshot(path=str(site_dir / "browser-failure.png"), full_page=True)
                except Exception:
                    pass
            finally:
                page.close()

            write_json(site_dir / "saved-responses.json", saved_responses)
            summary[slug] = {"url": url, "static": static_info, "browser": browser_info}
            print(f"DISCOVERED {slug}: {json.dumps(browser_info, ensure_ascii=False)}", flush=True)

        browser.close()

    write_json(OUT / "discovery-summary.json", summary)
    print("MULTISITE_DISCOVERY_START")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("MULTISITE_DISCOVERY_END")


if __name__ == "__main__":
    main()
