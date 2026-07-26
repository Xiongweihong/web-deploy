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

TARGETS = [
    "https://jobs.cantos.vc/jobs",
    "https://jobs.cantos.vc/companies",
]
OUT = Path("artifact")
RAW = OUT / "raw"
UA = "Mozilla/5.0 (compatible; evidence-first-public-page-audit/1.0)"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def save_bytes(path: Path, raw: bytes, meta: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    write_json(path.with_suffix(path.suffix + ".meta.json"), {**meta, "bytes": len(raw), "sha256": sha256(raw)})


def clean_name(url: str) -> str:
    parsed = urlparse(url)
    stem = (parsed.netloc + parsed.path).strip("/") or "root"
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem)
    return stem[:180]


def main() -> None:
    if OUT.exists():
        import shutil
        shutil.rmtree(OUT)
    RAW.mkdir(parents=True)

    static_summaries = []
    for index, url in enumerate(TARGETS, start=1):
        response = requests.get(url, timeout=120, headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
        raw = response.content
        path = RAW / "static" / f"{index:02d}_{clean_name(url)}.html"
        save_bytes(path, raw, {
            "requested_url": url,
            "final_url": response.url,
            "status": response.status_code,
            "content_type": response.headers.get("content-type"),
            "date": response.headers.get("date"),
            "etag": response.headers.get("etag"),
            "fetched_at_epoch": time.time(),
        })
        text = raw.decode("utf-8", "replace")
        static_summaries.append({
            "url": url,
            "status": response.status_code,
            "final_url": response.url,
            "sha256": sha256(raw),
            "title": BeautifulSoup(raw, "lxml").title.get_text(" ", strip=True) if BeautifulSoup(raw, "lxml").title else "",
            "collection_id_candidates": sorted(set(re.findall(r"(?:collection(?:Id|_id)?|collection-id)[^0-9]{0,30}([0-9]{2,10})", text, flags=re.I))),
            "getro_url_candidates": sorted(set(re.findall(r"https?://[^\"'\\s<>]+(?:getro|collections|search)[^\"'\\s<>]*", text, flags=re.I)))[:200],
        })

    network_log: list[dict] = []
    response_index = 0
    browser_summaries = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=UA, viewport={"width": 1440, "height": 1100})

        for page_index, target in enumerate(TARGETS, start=1):
            page = context.new_page()
            page_network: list[dict] = []

            def on_request(request):
                entry = {
                    "event": "request",
                    "page": page_index,
                    "method": request.method,
                    "url": request.url,
                    "resource_type": request.resource_type,
                    "post_data": request.post_data,
                    "headers": dict(request.headers),
                }
                page_network.append(entry)
                network_log.append(entry)

            def on_response(response):
                nonlocal response_index
                request = response.request
                content_type = response.headers.get("content-type", "")
                entry = {
                    "event": "response",
                    "page": page_index,
                    "method": request.method,
                    "url": response.url,
                    "status": response.status,
                    "resource_type": request.resource_type,
                    "content_type": content_type,
                    "post_data": request.post_data,
                }
                page_network.append(entry)
                network_log.append(entry)
                lower_url = response.url.lower()
                interesting = (
                    "json" in content_type.lower()
                    or any(token in lower_url for token in ["getro", "collection", "companies", "jobs", "search", "graphql", "api/"])
                )
                if interesting and response.status < 500:
                    try:
                        body = response.body()
                    except Exception as exc:
                        entry["body_error"] = repr(exc)
                        return
                    response_index += 1
                    suffix = ".json" if "json" in content_type.lower() else ".bin"
                    path = RAW / "network" / f"{response_index:04d}_{clean_name(response.url)}{suffix}"
                    save_bytes(path, body, {
                        "url": response.url,
                        "status": response.status,
                        "content_type": content_type,
                        "method": request.method,
                        "post_data": request.post_data,
                        "page": page_index,
                    })
                    entry["saved_path"] = str(path)
                    entry["body_sha256"] = sha256(body)

            page.on("request", on_request)
            page.on("response", on_response)
            nav = page.goto(target, wait_until="domcontentloaded", timeout=120_000)
            try:
                page.wait_for_load_state("networkidle", timeout=35_000)
            except Exception:
                pass
            page.wait_for_timeout(2500)

            # Trigger lazy data paths without bypassing any access control.
            for _ in range(12):
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(500)
                candidates = page.locator("button:visible")
                clicked = False
                for i in range(min(candidates.count(), 25)):
                    button = candidates.nth(i)
                    label = (button.inner_text() or "").strip().casefold()
                    if any(word in label for word in ["load more", "show more", "more jobs", "more companies"]):
                        try:
                            button.click(timeout=3000)
                            page.wait_for_timeout(700)
                            clicked = True
                            break
                        except Exception:
                            pass
                if not clicked:
                    break

            html = page.content().encode("utf-8")
            save_bytes(RAW / "browser" / f"page-{page_index:02d}.html", html, {
                "target": target,
                "final_url": page.url,
                "status": nav.status if nav else None,
                "fetched_at_epoch": time.time(),
            })
            page.screenshot(path=str(RAW / "browser" / f"page-{page_index:02d}.png"), full_page=True)

            storage = page.evaluate("""
                () => ({
                  localStorage: Object.fromEntries(Object.entries(localStorage)),
                  sessionStorage: Object.fromEntries(Object.entries(sessionStorage)),
                  nextData: window.__NEXT_DATA__ || null,
                  bodyText: document.body.innerText,
                  scripts: [...document.scripts].map(s => ({src: s.src, text: (s.textContent || '').slice(0, 200000)})),
                  links: [...document.querySelectorAll('a[href]')].map(a => ({text: (a.innerText || a.textContent || '').replace(/\s+/g,' ').trim(), href: a.href})),
                })
            """)
            write_json(RAW / "browser" / f"page-{page_index:02d}-state.json", storage)
            write_json(RAW / "browser" / f"page-{page_index:02d}-network.json", page_network)

            combined = json.dumps(storage, ensure_ascii=False) + "\n" + html.decode("utf-8", "replace") + "\n" + json.dumps(page_network, ensure_ascii=False)
            browser_summaries.append({
                "target": target,
                "final_url": page.url,
                "body_text_prefix": str(storage.get("bodyText") or "")[:5000],
                "collection_id_candidates": sorted(set(re.findall(r"(?:collection(?:Id|_id)?|collection-id)[^0-9]{0,40}([0-9]{2,10})", combined, flags=re.I))),
                "api_urls": sorted({entry["url"] for entry in page_network if entry.get("event") == "response" and any(token in entry.get("url", "").lower() for token in ["getro", "collection", "companies", "jobs", "search", "graphql", "api/"])}),
            })
            page.close()

        browser.close()

    write_json(OUT / "network-log.json", network_log)
    write_json(OUT / "discovery-summary.json", {
        "targets": TARGETS,
        "static": static_summaries,
        "browser": browser_summaries,
        "network_event_count": len(network_log),
        "saved_response_count": response_index,
    })
    print("DISCOVERY_SUMMARY_START")
    print(json.dumps({
        "static": static_summaries,
        "browser": browser_summaries,
        "network_event_count": len(network_log),
        "saved_response_count": response_index,
    }, ensure_ascii=False, indent=2))
    print("DISCOVERY_SUMMARY_END")


if __name__ == "__main__":
    main()
