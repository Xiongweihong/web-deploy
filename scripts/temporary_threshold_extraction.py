from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
import traceback
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

TARGET = "https://jobs.threshold.vc/companies"
OUT = Path("artifact")
RAW = OUT / "raw"
NETWORK = RAW / "network"


def clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_name(index: int, url: str, suffix: str) -> str:
    host = urlparse(url).netloc.replace(":", "_")
    path = re.sub(r"[^a-zA-Z0-9._-]+", "_", urlparse(url).path).strip("_")[:100]
    return f"{index:04d}_{host}_{path or 'root'}.{suffix}"


def walk(value: object, path: tuple[object, ...] = ()):
    yield path, value
    if isinstance(value, dict):
        for key, child in value.items():
            yield from walk(child, path + (key,))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from walk(child, path + (index,))


def first(mapping: dict, *keys: str):
    for key in keys:
        value = mapping.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def company_record(obj: dict, source: str, path: tuple[object, ...]) -> dict | None:
    # Canonical company objects used by Consider's board APIs.
    name = first(obj, "name", "companyName", "company_name", "title")
    domain = first(obj, "domain", "companyDomain", "company_domain", "website", "websiteUrl", "website_url")
    slug = first(obj, "slug", "companySlug", "company_slug")
    board_url = first(obj, "boardUrl", "board_url")
    num_jobs = first(obj, "numJobs", "num_jobs", "jobCount", "jobsCount")
    if not name:
        return None
    key_text = " ".join(str(k) for k in obj.keys()).casefold()
    path_text = " ".join(str(k) for k in path).casefold()
    company_signals = sum(
        [
            bool(domain),
            bool(slug),
            bool(board_url),
            num_jobs is not None,
            "company" in key_text,
            "compan" in path_text,
            "logos" in obj,
            "markets" in obj,
        ]
    )
    if company_signals < 2:
        return None
    return {
        "name": clean(name),
        "domain": clean(domain),
        "slug": clean(slug),
        "board_url": clean(board_url),
        "num_jobs": num_jobs,
        "source": source,
        "path": list(path),
        "keys": sorted(str(k) for k in obj.keys()),
        "raw": obj,
    }


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    NETWORK.mkdir(parents=True)

    response_inventory: list[dict] = []
    json_documents: list[dict] = []
    response_counter = 0

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 1000},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126 Safari/537.36",
        )
        page = context.new_page()

        def on_response(response):
            nonlocal response_counter
            response_counter += 1
            request = response.request
            meta = {
                "index": response_counter,
                "url": response.url,
                "status": response.status,
                "method": request.method,
                "resource_type": request.resource_type,
                "request_post_data": request.post_data,
                "request_headers": request.headers,
                "response_headers": response.headers,
            }
            ctype = clean(response.headers.get("content-type")).casefold()
            should_capture = (
                "json" in ctype
                or "api" in response.url.casefold()
                or "graphql" in response.url.casefold()
                or request.resource_type in {"xhr", "fetch", "document"}
            )
            if should_capture:
                try:
                    body = response.body()
                    suffix = "json" if "json" in ctype else "bin"
                    filename = safe_name(response_counter, response.url, suffix)
                    path = NETWORK / filename
                    path.write_bytes(body)
                    meta.update({"body_file": filename, "bytes": len(body), "sha256": sha256(body)})
                    try:
                        payload = json.loads(body)
                        json_documents.append({"source": filename, "url": response.url, "payload": payload})
                        meta["json_type"] = type(payload).__name__
                        if isinstance(payload, dict):
                            meta["json_keys"] = sorted(str(k) for k in payload.keys())
                    except Exception:
                        pass
                except Exception as exc:
                    meta["body_error"] = repr(exc)
            response_inventory.append(meta)

        page.on("response", on_response)
        page.goto(TARGET, wait_until="domcontentloaded", timeout=120_000)
        page.wait_for_timeout(12_000)

        # Exhaust lazy loading and click generic load-more controls when present.
        scroll_history = []
        stable_rounds = 0
        previous_height = -1
        for round_index in range(40):
            for label in ["Load more", "Show more", "View more", "More companies"]:
                try:
                    locator = page.get_by_text(label, exact=False)
                    count = locator.count()
                    for i in range(min(count, 3)):
                        item = locator.nth(i)
                        if item.is_visible():
                            item.click(timeout=2_000)
                            page.wait_for_timeout(1_500)
                except Exception:
                    pass
            current = page.evaluate(
                """() => ({
                    height: document.documentElement.scrollHeight,
                    textLength: document.body.innerText.length,
                    anchors: document.querySelectorAll('a').length,
                    cards: document.querySelectorAll('[class*=company i], [data-testid*=company i]').length
                })"""
            )
            scroll_history.append({"round": round_index, **current})
            page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
            page.wait_for_timeout(1_250)
            if current["height"] == previous_height:
                stable_rounds += 1
            else:
                stable_rounds = 0
            previous_height = current["height"]
            if stable_rounds >= 5:
                break

        page.wait_for_timeout(5_000)
        final_html = page.content().encode("utf-8")
        (RAW / "final-page.html").write_bytes(final_html)
        (RAW / "final-page.meta.json").write_text(
            json.dumps({"url": page.url, "bytes": len(final_html), "sha256": sha256(final_html)}, indent=2),
            encoding="utf-8",
        )
        page.screenshot(path=str(RAW / "final-page.png"), full_page=True)

        body_text = page.locator("body").inner_text()
        (RAW / "body-text.txt").write_text(body_text, encoding="utf-8")
        anchors = page.eval_on_selector_all(
            "a",
            """els => els.map((e, i) => ({
                index: i,
                text: (e.innerText || e.textContent || '').replace(/\\s+/g,' ').trim(),
                href: e.href,
                ariaLabel: e.getAttribute('aria-label'),
                title: e.getAttribute('title'),
                className: typeof e.className === 'string' ? e.className : '',
                data: Object.fromEntries([...e.attributes].filter(a => a.name.startsWith('data-')).map(a => [a.name,a.value]))
            }))""",
        )
        (RAW / "dom-anchors.json").write_text(json.dumps(anchors, ensure_ascii=False, indent=2), encoding="utf-8")
        resources = page.evaluate("performance.getEntriesByType('resource').map(e => ({name:e.name, initiatorType:e.initiatorType}))")
        (RAW / "performance-resources.json").write_text(json.dumps(resources, indent=2), encoding="utf-8")
        storage = page.evaluate(
            """() => ({
                localStorage: Object.fromEntries(Object.keys(localStorage).map(k => [k, localStorage.getItem(k)])),
                sessionStorage: Object.fromEntries(Object.keys(sessionStorage).map(k => [k, sessionStorage.getItem(k)])),
                nextData: window.__NEXT_DATA__ || null,
                nuxtData: window.__NUXT__ || null
            })"""
        )
        (RAW / "browser-state.json").write_text(json.dumps(storage, ensure_ascii=False, indent=2), encoding="utf-8")
        for key in ("nextData", "nuxtData"):
            if storage.get(key) is not None:
                json_documents.append({"source": f"browser-state:{key}", "url": page.url, "payload": storage[key]})
        browser.close()

    (RAW / "response-inventory.json").write_text(
        json.dumps(response_inventory, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (RAW / "scroll-history.json").write_text(json.dumps(scroll_history, indent=2), encoding="utf-8")

    array_candidates: list[dict] = []
    object_candidates: list[dict] = []
    for document in json_documents:
        for path, value in walk(document["payload"]):
            if isinstance(value, list) and value and all(isinstance(x, dict) for x in value):
                extracted = [company_record(x, document["source"], path + (i,)) for i, x in enumerate(value)]
                extracted = [x for x in extracted if x]
                if extracted:
                    array_candidates.append(
                        {
                            "source": document["source"],
                            "url": document["url"],
                            "path": list(path),
                            "array_length": len(value),
                            "company_like_count": len(extracted),
                            "sample": [{k: row[k] for k in ("name", "domain", "slug", "num_jobs")} for row in extracted[:5]],
                        }
                    )
            if isinstance(value, dict):
                row = company_record(value, document["source"], path)
                if row:
                    object_candidates.append(row)

    # Deduplicate identical objects across nested paths and network retries.
    dedup: dict[tuple[str, str, str], dict] = {}
    for row in object_candidates:
        key = (row["name"].casefold(), row["domain"].casefold(), row["slug"].casefold())
        existing = dedup.get(key)
        score = sum([bool(row["domain"]), bool(row["slug"]), bool(row["board_url"]), row["num_jobs"] is not None])
        if existing is None:
            row["score"] = score
            dedup[key] = row
        elif score > existing.get("score", 0):
            row["score"] = score
            dedup[key] = row
    company_candidates = sorted(dedup.values(), key=lambda r: (-r.get("score", 0), r["name"].casefold()))

    (OUT / "array-candidates.json").write_text(
        json.dumps(sorted(array_candidates, key=lambda x: (-x["company_like_count"], -x["array_length"])), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (OUT / "company-candidates.json").write_text(
        json.dumps(company_candidates, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    summary = {
        "target": TARGET,
        "response_count": len(response_inventory),
        "json_document_count": len(json_documents),
        "array_candidate_count": len(array_candidates),
        "unique_company_candidate_count": len(company_candidates),
        "candidate_with_domain_count": sum(bool(x["domain"]) for x in company_candidates),
        "top_arrays": sorted(array_candidates, key=lambda x: (-x["company_like_count"], -x["array_length"]))[:20],
        "body_count_match": re.findall(r"(\d+)\s+companies", body_text, re.I),
        "body_jobs_match": re.findall(r"([\d,]+)\s+jobs", body_text, re.I),
    }
    (OUT / "discovery-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("DISCOVERY_SUMMARY_START")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("DISCOVERY_SUMMARY_END")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        OUT.mkdir(parents=True, exist_ok=True)
        error = {"status": "FATAL", "error": repr(exc), "traceback": traceback.format_exc()}
        (OUT / "fatal-error.json").write_text(json.dumps(error, indent=2), encoding="utf-8")
        print(error["traceback"])
