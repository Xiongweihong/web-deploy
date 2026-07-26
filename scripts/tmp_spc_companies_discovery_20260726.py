from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

TARGET = "https://www.southparkcommons.com/companies"
OUT = Path("artifact")
RAW = OUT / "raw"
UA = "Mozilla/5.0 (compatible; spc-evidence-crawler/1.0)"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def save_bytes(path: Path, raw: bytes, meta: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    write_json(path.with_suffix(path.suffix + ".meta.json"), {**meta, "bytes": len(raw), "sha256": sha256(raw)})


def request_get(url: str) -> requests.Response:
    last = None
    for attempt in range(1, 6):
        try:
            response = requests.get(
                url,
                timeout=120,
                headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8"},
            )
            response.raise_for_status()
            return response
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < 5:
                time.sleep(min(20, 2 ** attempt))
    raise RuntimeError(f"GET failed for {url}: {last}")


def parse_static(raw: bytes, base_url: str) -> dict:
    soup = BeautifulSoup(raw, "lxml")
    company_links = []
    pagination_links = []
    scripts = []
    all_links = []
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "").strip()
        absolute = urljoin(base_url, href)
        parsed = urlparse(absolute)
        text = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True)).strip()
        image = anchor.find("img")
        image_alt = str(image.get("alt") or "").strip() if image else ""
        item = {
            "href": href,
            "absolute": absolute,
            "text": text,
            "image_alt": image_alt,
            "class": " ".join(anchor.get("class") or []),
            "aria_label": str(anchor.get("aria-label") or "").strip(),
        }
        all_links.append(item)
        path = parsed.path.rstrip("/")
        if path.startswith("/companies/") and path != "/companies":
            company_links.append(item)
        if "_page=" in absolute or "w-pagination" in item["class"]:
            pagination_links.append(item)
    for script in soup.find_all("script", src=True):
        scripts.append(urljoin(base_url, str(script.get("src") or "").strip()))
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()
    return {
        "title": soup.title.get_text(" ", strip=True) if soup.title else "",
        "company_links": company_links,
        "pagination_links": pagination_links,
        "scripts": scripts,
        "all_link_count": len(all_links),
        "company_link_count": len(company_links),
        "pagination_link_count": len(pagination_links),
        "body_text_prefix": text[:10000],
        "ids": sorted({str(node.get("id")) for node in soup.find_all(id=True)}),
        "classes": sorted({cls for node in soup.find_all(class_=True) for cls in (node.get("class") or []) if any(token in cls.lower() for token in ("company", "collection", "pagination", "card", "filter", "grid"))}),
    }


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    RAW.mkdir(parents=True)

    static_summaries = []
    for index in (1, 2):
        response = request_get(TARGET)
        raw = response.content
        save_bytes(
            RAW / f"static-{index:02d}" / "companies.html",
            raw,
            {
                "requested_url": TARGET,
                "final_url": response.url,
                "status": response.status_code,
                "content_type": response.headers.get("content-type"),
                "date": response.headers.get("date"),
                "etag": response.headers.get("etag"),
                "fetched_at_epoch": time.time(),
            },
        )
        summary = parse_static(raw, response.url)
        static_summaries.append(summary)
        write_json(OUT / f"static-{index:02d}-summary.json", summary)
        if index == 1:
            time.sleep(2)

    network_index = []
    browser_summary = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(user_agent=UA, viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        response_counter = 0

        def on_response(response) -> None:
            nonlocal response_counter
            try:
                request = response.request
                resource_type = request.resource_type
                content_type = response.headers.get("content-type", "")
                if resource_type not in {"document", "xhr", "fetch", "script"} and not any(
                    token in content_type.lower() for token in ("json", "javascript", "text/html")
                ):
                    return
                response_counter += 1
                record = {
                    "index": response_counter,
                    "url": response.url,
                    "status": response.status,
                    "resource_type": resource_type,
                    "method": request.method,
                    "content_type": content_type,
                    "post_data": request.post_data,
                }
                network_index.append(record)
                try:
                    body = response.body()
                except Exception as exc:  # noqa: BLE001
                    record["body_error"] = repr(exc)
                    return
                if len(body) <= 8_000_000:
                    suffix = ".json" if "json" in content_type.lower() else ".js" if "javascript" in content_type.lower() else ".html" if "html" in content_type.lower() else ".bin"
                    filename = f"{response_counter:04d}{suffix}"
                    save_bytes(
                        RAW / "browser" / "responses" / filename,
                        body,
                        record,
                    )
                    record["saved_path"] = str((RAW / "browser" / "responses" / filename).relative_to(OUT))
                    record["sha256"] = sha256(body)
                    record["bytes"] = len(body)
            except Exception as exc:  # noqa: BLE001
                network_index.append({"listener_error": repr(exc), "url": getattr(response, "url", "")})

        page.on("response", on_response)
        nav = page.goto(TARGET, wait_until="domcontentloaded", timeout=120_000)
        try:
            page.wait_for_load_state("networkidle", timeout=30_000)
        except Exception:
            pass
        page.wait_for_timeout(3000)

        scroll_log = []
        previous_count = -1
        stable_rounds = 0
        for iteration in range(20):
            data = page.evaluate(
                """
                () => {
                  const links = [...document.querySelectorAll('a[href]')].filter(a => {
                    try { const u = new URL(a.href, location.href); return u.pathname.startsWith('/companies/') && u.pathname !== '/companies'; }
                    catch { return false; }
                  });
                  return {
                    height: document.documentElement.scrollHeight,
                    companyLinkCount: links.length,
                    uniqueCompanyHrefs: [...new Set(links.map(a => a.href))].length,
                  };
                }
                """
            )
            scroll_log.append({"iteration": iteration, **data})
            if data["uniqueCompanyHrefs"] == previous_count:
                stable_rounds += 1
            else:
                stable_rounds = 0
            previous_count = data["uniqueCompanyHrefs"]
            if stable_rounds >= 3:
                break
            page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
            page.wait_for_timeout(1500)

        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(500)
        html = page.content().encode("utf-8")
        save_bytes(
            RAW / "browser" / "rendered.html",
            html,
            {
                "url": page.url,
                "status": nav.status if nav else None,
                "fetched_at_epoch": time.time(),
            },
        )
        page.screenshot(path=str(RAW / "browser" / "full-page.png"), full_page=True)

        browser_summary = page.evaluate(
            """
            () => {
              const norm = s => (s || '').replace(/\s+/g, ' ').trim();
              const companyLinks = [...document.querySelectorAll('a[href]')].filter(a => {
                try { const u = new URL(a.href, location.href); return u.pathname.startsWith('/companies/') && u.pathname !== '/companies'; }
                catch { return false; }
              }).map(a => ({
                href: a.getAttribute('href'),
                absolute: a.href,
                text: norm(a.innerText || a.textContent),
                ariaLabel: a.getAttribute('aria-label') || '',
                className: a.className || '',
                imageAlts: [...a.querySelectorAll('img')].map(img => img.alt || ''),
                rect: (() => { const r=a.getBoundingClientRect(); return {x:r.x,y:r.y,width:r.width,height:r.height}; })(),
                visible: !!(a.offsetWidth || a.offsetHeight || a.getClientRects().length),
              }));
              const pagination = [...document.querySelectorAll('a[href], button')].filter(el => {
                const text = norm(el.innerText || el.textContent).toLowerCase();
                const cls = String(el.className || '').toLowerCase();
                const href = el.getAttribute && (el.getAttribute('href') || '');
                return cls.includes('pagination') || cls.includes('w-pagination') || href.includes('_page=') || ['next','previous','prev'].includes(text);
              }).map(el => ({
                tag: el.tagName,
                text: norm(el.innerText || el.textContent),
                href: el.getAttribute && (el.getAttribute('href') || ''),
                className: el.className || '',
                visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
              }));
              const candidates = [...document.querySelectorAll('*')].filter(el => {
                const cls = String(el.className || '').toLowerCase();
                return ['company','collection','pagination','card','filter','grid'].some(t => cls.includes(t));
              }).slice(0, 2000).map(el => ({
                tag: el.tagName,
                id: el.id || '',
                className: el.className || '',
                text: norm(el.innerText || el.textContent).slice(0, 500),
                href: el.getAttribute && (el.getAttribute('href') || ''),
                visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
              }));
              const webflowLists = [...document.querySelectorAll('[role=list], .w-dyn-list, .w-dyn-items, .w-dyn-item')].slice(0, 3000).map(el => ({
                tag: el.tagName,
                className: el.className || '',
                role: el.getAttribute('role') || '',
                childCount: el.children.length,
                text: norm(el.innerText || el.textContent).slice(0, 300),
                visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
              }));
              return {
                title: document.title,
                url: location.href,
                bodyText: norm(document.body.innerText).slice(0, 30000),
                companyLinks,
                pagination,
                candidates,
                webflowLists,
                allHrefsWithPageParams: [...new Set([...document.querySelectorAll('a[href]')].map(a => a.href).filter(h => h.includes('_page=')))],
                scripts: [...document.scripts].map(s => s.src).filter(Boolean),
              };
            }
            """
        )
        write_json(OUT / "browser-summary.json", browser_summary)
        write_json(OUT / "scroll-log.json", scroll_log)
        write_json(OUT / "network-index.json", network_index)
        context.close()
        browser.close()

    summary = {
        "target": TARGET,
        "static_company_link_counts": [item["company_link_count"] for item in static_summaries],
        "static_pagination_link_counts": [item["pagination_link_count"] for item in static_summaries],
        "static_company_href_sets_equal": {
            item["absolute"] for item in static_summaries[0]["company_links"]
        } == {
            item["absolute"] for item in static_summaries[1]["company_links"]
        },
        "browser_company_link_count": len(browser_summary.get("companyLinks") or []),
        "browser_unique_company_href_count": len({item["absolute"] for item in (browser_summary.get("companyLinks") or [])}),
        "browser_pagination": browser_summary.get("pagination") or [],
        "browser_page_param_hrefs": browser_summary.get("allHrefsWithPageParams") or [],
        "scroll_log": scroll_log,
        "network_response_count": len(network_index),
        "network_json_candidates": [
            item for item in network_index
            if "json" in str(item.get("content_type") or "").lower()
            or any(token in str(item.get("url") or "").lower() for token in ("api", "graphql", "collection", "cms"))
        ],
    }
    write_json(OUT / "discovery-summary.json", summary)

    manifest = {"files": {}}
    for path in sorted(OUT.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            manifest["files"][str(path.relative_to(OUT))] = {
                "bytes": path.stat().st_size,
                "sha256": sha256(path.read_bytes()),
            }
    write_json(OUT / "manifest.json", manifest)

    print("DISCOVERY_SUMMARY_START")
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    print("DISCOVERY_SUMMARY_END")


if __name__ == "__main__":
    main()
