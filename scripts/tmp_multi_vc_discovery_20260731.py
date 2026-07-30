from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from playwright.sync_api import sync_playwright

OUT = Path("artifact")
OUT.mkdir(parents=True, exist_ok=True)
UA = "Mozilla/5.0 (compatible; MultiVCPortfolioAudit/1.0)"

PAGES = {
    "iconiq": "https://www.iconiq.com/growth/companies",
    "general-catalyst": "https://www.generalcatalyst.com/portfolio",
    "index-ventures": "https://www.indexventures.com/companies/",
    "capitalg-portfolio": "https://capitalg.com/portfolio/",
    "capitalg-jobs": "https://careers.capitalg.com/jobs",
    "bcv-portfolio": "https://baincapitalventures.com/portfolio/",
    "bcv-jobs": "https://jobs.baincapitalventures.com/jobs",
    "cvs-health-ventures": "https://www.cvshealthventures.com/portfolio.html",
    "hanabi": "https://www.hanabi.com/companies",
    "astar": "https://www.a-star.co/companies",
    "acapital": "https://acapital.com/portfolio",
    "khosla": "https://www.khoslaventures.com/portfolio",
}

EXPAND_RE = re.compile(
    r"^(?:load|view|show|see)\s+(?:more|all(?:\s+companies)?|all\s+investments)$|^view\s+all\s+companies$",
    re.I,
)


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def dump_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value)[:180]


def main() -> None:
    static_summary = {}
    for slug, url in PAGES.items():
        snapshots = []
        for index in (1, 2):
            response = requests.get(url, headers={"User-Agent": UA}, timeout=120, allow_redirects=True)
            raw = response.content
            (OUT / f"{slug}-static-{index}.html").write_bytes(raw)
            snapshots.append({
                "index": index,
                "status": response.status_code,
                "requested_url": url,
                "final_url": response.url,
                "bytes": len(raw),
                "sha256": sha256(raw),
                "content_type": response.headers.get("content-type"),
                "headers": dict(response.headers),
            })
            time.sleep(0.5)
        static_summary[slug] = {
            "snapshots": snapshots,
            "hash_equal": snapshots[0]["sha256"] == snapshots[1]["sha256"],
        }
    dump_json(OUT / "static-summary.json", static_summary)

    browser_summary = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=UA, viewport={"width": 1440, "height": 1200})

        for slug, url in PAGES.items():
            page = context.new_page()
            network = []
            response_index = 0

            def on_response(response):
                nonlocal response_index
                request = response.request
                if request.resource_type not in {"xhr", "fetch"}:
                    return
                item = {
                    "index": response_index,
                    "url": response.url,
                    "status": response.status,
                    "method": request.method,
                    "post_data": request.post_data,
                    "content_type": response.headers.get("content-type"),
                }
                try:
                    body = response.body()
                    if len(body) <= 15_000_000:
                        suffix = ".json" if "json" in (item["content_type"] or "") else ".bin"
                        filename = f"{slug}-xhr-{response_index:03d}-{safe_name(urlparse(response.url).path)}{suffix}"
                        (OUT / filename).write_bytes(body)
                        item["body_file"] = filename
                        item["body_bytes"] = len(body)
                        item["body_sha256"] = sha256(body)
                    else:
                        item["body_bytes"] = len(body)
                        item["body_omitted"] = True
                except Exception as exc:
                    item["body_error"] = repr(exc)
                network.append(item)
                response_index += 1

            page.on("response", on_response)
            try:
                nav = page.goto(url, wait_until="domcontentloaded", timeout=120_000)
                try:
                    page.wait_for_load_state("networkidle", timeout=30_000)
                except Exception:
                    pass
                page.wait_for_timeout(1200)

                for label in ["Accept All", "Accept all", "Accept", "Allow all", "I Agree", "Close"]:
                    loc = page.get_by_role("button", name=re.compile(rf"^{re.escape(label)}$", re.I))
                    for idx in range(loc.count()):
                        try:
                            if loc.nth(idx).is_visible():
                                loc.nth(idx).click(timeout=2500)
                                page.wait_for_timeout(150)
                        except Exception:
                            pass

                click_log = []
                for iteration in range(80):
                    before = page.evaluate(
                        """
                        () => ({
                          headings: document.querySelectorAll('h1,h2,h3,h4,h5,h6').length,
                          links: document.querySelectorAll('a[href]').length,
                          textLength: (document.body.innerText || '').length,
                        })
                        """
                    )
                    candidates = page.locator("button,a,[role='button']")
                    visible = []
                    for idx in range(candidates.count()):
                        loc = candidates.nth(idx)
                        try:
                            text = re.sub(r"\s+", " ", loc.inner_text(timeout=300)).strip()
                            if text and EXPAND_RE.match(text) and loc.is_visible():
                                visible.append((idx, text))
                        except Exception:
                            pass
                    if not visible:
                        break
                    idx, text = visible[-1]
                    target = candidates.nth(idx)
                    try:
                        target.scroll_into_view_if_needed(timeout=5000)
                        target.click(force=True, timeout=8000)
                    except Exception as exc:
                        click_log.append({"iteration": iteration + 1, "text": text, "before": before, "error": repr(exc)})
                        break
                    page.wait_for_timeout(1000)
                    try:
                        page.wait_for_load_state("networkidle", timeout=6000)
                    except Exception:
                        pass
                    after = page.evaluate(
                        """
                        () => ({
                          headings: document.querySelectorAll('h1,h2,h3,h4,h5,h6').length,
                          links: document.querySelectorAll('a[href]').length,
                          textLength: (document.body.innerText || '').length,
                        })
                        """
                    )
                    click_log.append({"iteration": iteration + 1, "text": text, "before": before, "after": after})
                    if after == before:
                        break

                analysis = page.evaluate(
                    """
                    () => {
                      const norm = s => (s || '').replace(/\s+/g, ' ').trim();
                      const anchors = [...document.querySelectorAll('a[href]')].map(a => ({
                        text: norm(a.innerText || a.textContent),
                        href: a.href,
                        target: a.target || '',
                        rel: a.rel || '',
                        className: typeof a.className === 'string' ? a.className : '',
                      }));
                      const host = location.hostname.replace(/^www\./, '');
                      const externals = anchors.filter(a => {
                        try {
                          const h = new URL(a.href).hostname.replace(/^www\./, '');
                          return h && h !== host && !h.endsWith('.' + host);
                        } catch { return false; }
                      });
                      const headings = [...document.querySelectorAll('h1,h2,h3,h4,h5,h6')].map(h => ({
                        tag: h.tagName,
                        text: norm(h.innerText || h.textContent),
                        id: h.id || '',
                        className: typeof h.className === 'string' ? h.className : '',
                      })).filter(x => x.text);
                      const buttons = [...document.querySelectorAll('button,a,[role="button"]')].map(el => ({
                        text: norm(el.innerText || el.textContent),
                        tag: el.tagName,
                        href: el.href || '',
                        visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
                        className: typeof el.className === 'string' ? el.className : '',
                      })).filter(x => x.text);
                      const scripts = [...document.querySelectorAll('script')].map((s, index) => ({
                        index,
                        id: s.id || '',
                        type: s.type || '',
                        src: s.src || '',
                        text: (s.textContent || '').slice(0, 100000),
                      })).filter(s => s.src || s.text);
                      return {
                        title: document.title,
                        url: location.href,
                        bodyTextPreview: norm(document.body.innerText).slice(0, 15000),
                        anchorCount: anchors.length,
                        externalCount: externals.length,
                        headingCount: headings.length,
                        buttonCount: buttons.length,
                        anchors,
                        externals,
                        headings,
                        buttons,
                        scripts,
                      };
                    }
                    """
                )
                (OUT / f"{slug}-browser.html").write_text(page.content(), encoding="utf-8")
                page.screenshot(path=str(OUT / f"{slug}-browser.png"), full_page=True)
                result = {
                    "status": nav.status if nav else None,
                    "final_url": page.url,
                    "click_log": click_log,
                    "analysis": analysis,
                    "network": network,
                }
            except Exception as exc:
                result = {"error": repr(exc), "network": network}
            browser_summary[slug] = result
            dump_json(OUT / f"{slug}-discovery.json", result)
            page.close()
        browser.close()

    dump_json(OUT / "browser-summary.json", browser_summary)
    compact = {}
    for slug, result in browser_summary.items():
        analysis = result.get("analysis", {}) if isinstance(result, dict) else {}
        compact[slug] = {
            "status": result.get("status") if isinstance(result, dict) else None,
            "error": result.get("error") if isinstance(result, dict) else None,
            "clicks": len(result.get("click_log", [])) if isinstance(result, dict) else 0,
            "headings": analysis.get("headingCount"),
            "anchors": analysis.get("anchorCount"),
            "externals": analysis.get("externalCount"),
            "network": len(result.get("network", [])) if isinstance(result, dict) else 0,
            "first_headings": [x.get("text") for x in analysis.get("headings", [])[:12]],
            "last_headings": [x.get("text") for x in analysis.get("headings", [])[-12:]],
            "expand_buttons_visible": [x.get("text") for x in analysis.get("buttons", []) if x.get("visible") and EXPAND_RE.match(x.get("text", ""))][:20],
        }
    print("MULTI_VC_DISCOVERY_START")
    print(json.dumps(compact, ensure_ascii=False, indent=2))
    print("MULTI_VC_DISCOVERY_END")


if __name__ == "__main__":
    main()
