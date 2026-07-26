from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
import traceback
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

TARGET = "https://500.co/portfolio"
OUT = Path("artifact")
RAW = OUT / "raw"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/150 Safari/537.36"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def save_bytes(path: Path, raw: bytes, meta: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    payload = {**meta, "bytes": len(raw), "sha256": sha256(raw)}
    write_json(path.with_suffix(path.suffix + ".meta.json"), payload)


def get(url: str, attempts: int = 5) -> requests.Response:
    last = None
    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(
                url,
                timeout=120,
                allow_redirects=True,
                headers={"User-Agent": UA, "Accept": "text/html,application/json,*/*"},
            )
            response.raise_for_status()
            return response
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < attempts:
                time.sleep(min(20, 2**attempt))
    raise RuntimeError(f"GET failed: {url}: {last}")


def extract_scripts(html: bytes) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    scripts = []
    for node in soup.find_all("script", src=True):
        scripts.append(urljoin(TARGET, clean(node.get("src"))))
    return list(dict.fromkeys(scripts))


def relevant_text_hits(text: str) -> list[str]:
    patterns = [
        r"https?://[^\"'\s)]+",
        r"/[A-Za-z0-9_./?=&%-]*(?:portfolio|compan|search|graphql|algolia|api)[A-Za-z0-9_./?=&%-]*",
    ]
    hits = []
    for pattern in patterns:
        for match in re.findall(pattern, text, flags=re.I):
            value = clean(match)
            low = value.casefold()
            if any(token in low for token in ("portfolio", "compan", "algolia", "graphql", "builder.io", "/api/")):
                hits.append(value[:1000])
    return list(dict.fromkeys(hits))[:1000]


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    RAW.mkdir(parents=True)

    static_summaries = []
    script_urls: list[str] = []
    for index in (1, 2):
        response = get(TARGET)
        raw = response.content
        save_bytes(
            RAW / f"static-{index:02d}" / "portfolio.html",
            raw,
            {
                "requested_url": response.request.url,
                "final_url": response.url,
                "status": response.status_code,
                "content_type": response.headers.get("content-type"),
                "date": response.headers.get("date"),
                "etag": response.headers.get("etag"),
                "fetched_at_epoch": time.time(),
            },
        )
        soup = BeautifulSoup(raw, "lxml")
        scripts = extract_scripts(raw)
        script_urls.extend(scripts)
        static_summaries.append(
            {
                "index": index,
                "sha256": sha256(raw),
                "bytes": len(raw),
                "title": clean(soup.title.get_text()) if soup.title else "",
                "script_count": len(scripts),
                "link_count": len(soup.find_all("a")),
                "text_prefix": clean(soup.get_text(" ", strip=True))[:3000],
                "portfolio_count_mentions": re.findall(r"\b\d{3,5}\b", clean(soup.get_text(" ", strip=True)))[:100],
            }
        )
        time.sleep(2)

    script_urls = list(dict.fromkeys(script_urls))
    write_json(OUT / "script-urls.json", script_urls)

    js_index = []
    for number, url in enumerate(script_urls, start=1):
        try:
            response = get(url, attempts=3)
            raw = response.content
            path = RAW / "scripts" / f"{number:04d}.js"
            save_bytes(
                path,
                raw,
                {
                    "requested_url": response.request.url,
                    "final_url": response.url,
                    "status": response.status_code,
                    "content_type": response.headers.get("content-type"),
                    "date": response.headers.get("date"),
                },
            )
            text = raw.decode("utf-8", "replace")
            hits = relevant_text_hits(text)
            js_index.append(
                {
                    "index": number,
                    "url": url,
                    "bytes": len(raw),
                    "sha256": sha256(raw),
                    "has_2243": "2243" in text,
                    "has_portfolio": "portfolio" in text.casefold(),
                    "has_algolia": "algolia" in text.casefold(),
                    "has_builder": "builder.io" in text.casefold(),
                    "hits": hits,
                }
            )
        except Exception as exc:  # noqa: BLE001
            js_index.append({"index": number, "url": url, "error": repr(exc)})
    write_json(OUT / "js-index.json", js_index)

    network_records = []
    response_counter = 0

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=UA,
            viewport={"width": 1440, "height": 1000},
            locale="en-US",
        )
        page = context.new_page()

        def handle_response(response) -> None:
            nonlocal response_counter
            try:
                url = response.url
                headers = response.headers
                content_type = headers.get("content-type", "")
                resource_type = response.request.resource_type
                record = {
                    "url": url,
                    "status": response.status,
                    "resource_type": resource_type,
                    "content_type": content_type,
                    "request_method": response.request.method,
                    "request_post_data": response.request.post_data,
                }
                should_save = (
                    "json" in content_type.casefold()
                    or resource_type in {"xhr", "fetch"}
                    or any(token in url.casefold() for token in ("portfolio", "compan", "algolia", "graphql", "builder.io", "/api/"))
                )
                if should_save:
                    response_counter += 1
                    try:
                        body = response.body()
                    except Exception as exc:  # noqa: BLE001
                        record["body_error"] = repr(exc)
                        body = b""
                    if body and len(body) <= 60_000_000:
                        suffix = ".json" if "json" in content_type.casefold() else ".bin"
                        path = RAW / "browser" / "responses" / f"{response_counter:04d}{suffix}"
                        save_bytes(
                            path,
                            body,
                            {
                                "url": url,
                                "status": response.status,
                                "resource_type": resource_type,
                                "content_type": content_type,
                                "request_method": response.request.method,
                                "request_post_data": response.request.post_data,
                            },
                        )
                        record["saved_path"] = str(path)
                        record["bytes"] = len(body)
                        text = body[:5_000_000].decode("utf-8", "replace")
                        record["has_2243"] = "2243" in text
                        record["has_company_terms"] = any(
                            token in text.casefold() for token in ("companyname", "company_name", "portfolio companies", "industries")
                        )
                        if record["has_2243"] or record["has_company_terms"]:
                            record["text_prefix"] = text[:2000]
                    network_records.append(record)
            except Exception as exc:  # noqa: BLE001
                network_records.append({"url": getattr(response, "url", ""), "handler_error": repr(exc)})

        page.on("response", handle_response)
        page.goto(TARGET, wait_until="domcontentloaded", timeout=180_000)
        page.wait_for_timeout(20_000)

        # Move to the portfolio section and exercise likely pagination controls.
        try:
            page.get_by_text("Portfolio Companies", exact=True).scroll_into_view_if_needed()
        except Exception:
            pass
        page.wait_for_timeout(5_000)

        click_log = []
        for round_number in range(1, 31):
            candidates = page.locator("button:visible")
            clicked = False
            for index in range(candidates.count()):
                button = candidates.nth(index)
                text = clean(button.inner_text())
                aria = clean(button.get_attribute("aria-label"))
                label = f"{text} {aria}".casefold()
                if any(token in label for token in ("show more", "load more", "next")):
                    try:
                        button.click(timeout=10_000)
                        page.wait_for_timeout(2_000)
                        click_log.append({"round": round_number, "text": text, "aria": aria})
                        clicked = True
                        break
                    except Exception as exc:  # noqa: BLE001
                        click_log.append({"round": round_number, "text": text, "aria": aria, "error": repr(exc)})
            if not clicked:
                break

        # Controlled scroll to force lazy data loading.
        scroll_log = []
        stable = 0
        last_height = 0
        for iteration in range(1, 41):
            height = page.evaluate("document.documentElement.scrollHeight")
            page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
            page.wait_for_timeout(1500)
            new_height = page.evaluate("document.documentElement.scrollHeight")
            scroll_log.append({"iteration": iteration, "before": height, "after": new_height})
            if new_height == last_height:
                stable += 1
            else:
                stable = 0
            last_height = new_height
            if stable >= 3:
                break

        page.wait_for_timeout(5_000)
        html = page.content().encode("utf-8")
        save_bytes(
            RAW / "browser" / "final.html",
            html,
            {"url": page.url, "fetched_at_epoch": time.time()},
        )
        page.screenshot(path=str(RAW / "browser" / "final.png"), full_page=True)

        performance_entries = page.evaluate(
            "performance.getEntriesByType('resource').map(e => ({name:e.name, initiatorType:e.initiatorType, duration:e.duration, transferSize:e.transferSize}))"
        )
        write_json(OUT / "performance-entries.json", performance_entries)
        write_json(OUT / "click-log.json", click_log)
        write_json(OUT / "scroll-log.json", scroll_log)

        # Broad DOM evidence for company cards, links and text.
        dom_summary = page.evaluate(
            """
            () => {
              const all = [...document.querySelectorAll('*')];
              const elements = all.filter(el => {
                const text = (el.innerText || '').trim();
                const cls = typeof el.className === 'string' ? el.className : '';
                return /company|portfolio|industry|country|stage/i.test(cls + ' ' + text) && text.length < 1000;
              }).slice(0, 10000).map(el => ({
                tag: el.tagName,
                text: (el.innerText || '').trim(),
                cls: typeof el.className === 'string' ? el.className : '',
                href: el.href || '',
                aria: el.getAttribute('aria-label') || '',
                data: {...el.dataset},
              }));
              return {
                title: document.title,
                bodyText: document.body.innerText,
                links: [...document.querySelectorAll('a[href]')].map(a => ({text:(a.innerText||'').trim(), href:a.href, cls:a.className||''})),
                elements,
                buttons: [...document.querySelectorAll('button')].map(b => ({text:(b.innerText||'').trim(), aria:b.getAttribute('aria-label')||'', disabled:b.disabled, cls:b.className||''})),
              };
            }
            """
        )
        write_json(OUT / "dom-summary.json", dom_summary)
        browser.close()

    write_json(OUT / "network-index.json", network_records)

    url_counts = Counter(urlparse(item.get("url", "")).netloc for item in network_records if item.get("url"))
    candidate_responses = [
        item
        for item in network_records
        if item.get("has_2243") or item.get("has_company_terms")
    ]
    summary = {
        "target": TARGET,
        "static_summaries": static_summaries,
        "static_hashes_equal": len(static_summaries) == 2 and static_summaries[0]["sha256"] == static_summaries[1]["sha256"],
        "script_count": len(script_urls),
        "js_files_with_2243": [item for item in js_index if item.get("has_2243")],
        "js_files_with_portfolio": sum(bool(item.get("has_portfolio")) for item in js_index),
        "network_record_count": len(network_records),
        "network_host_counts": dict(url_counts),
        "candidate_responses": candidate_responses,
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
    print(json.dumps({
        "static_hashes_equal": summary["static_hashes_equal"],
        "script_count": summary["script_count"],
        "network_record_count": summary["network_record_count"],
        "candidate_response_count": len(candidate_responses),
        "candidate_responses": candidate_responses[:20],
        "network_host_counts": summary["network_host_counts"],
    }, ensure_ascii=False, indent=2, sort_keys=True))
    print("DISCOVERY_SUMMARY_END")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        OUT.mkdir(parents=True, exist_ok=True)
        write_json(OUT / "fatal-error.json", {"error": repr(exc), "traceback": traceback.format_exc()})
        raise
