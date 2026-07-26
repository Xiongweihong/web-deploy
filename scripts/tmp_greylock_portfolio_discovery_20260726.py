from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
import traceback
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

TARGET = "https://greylock.com/portfolio/"
OUT = Path("artifact")
RAW = OUT / "raw"
UA = "Mozilla/5.0 (compatible; greylock-evidence-crawler/1.0)"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def save_bytes(path: Path, data: bytes, meta: dict | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    payload = dict(meta or {})
    payload.update({"bytes": len(data), "sha256": sha256(data)})
    write_json(path.with_suffix(path.suffix + ".meta.json"), payload)


def fetch(url: str, *, params: dict | None = None, attempts: int = 5) -> requests.Response:
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(
                url,
                params=params,
                timeout=120,
                allow_redirects=True,
                headers={
                    "User-Agent": UA,
                    "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
                },
            )
            response.raise_for_status()
            return response
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < attempts:
                time.sleep(min(30, 2**attempt))
    raise RuntimeError(f"GET failed: {url}: {last}")


def normalize_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def static_capture(number: int) -> dict:
    response = fetch(TARGET)
    root = RAW / f"static-{number:02d}"
    path = root / "portfolio.html"
    save_bytes(
        path,
        response.content,
        {
            "requested_url": response.request.url,
            "final_url": response.url,
            "status": response.status_code,
            "content_type": response.headers.get("content-type"),
            "date": response.headers.get("date"),
            "etag": response.headers.get("etag"),
            "last_modified": response.headers.get("last-modified"),
            "fetched_at_epoch": time.time(),
        },
    )
    soup = BeautifulSoup(response.content, "lxml")
    anchors = []
    for a in soup.find_all("a", href=True):
        href = urljoin(TARGET, a.get("href"))
        anchors.append(
            {
                "text": normalize_text(a.get_text(" ", strip=True)),
                "href": href,
                "class": a.get("class") or [],
                "aria_label": a.get("aria-label"),
                "title": a.get("title"),
                "images": [
                    {
                        "alt": image.get("alt"),
                        "src": image.get("src"),
                        "srcset": image.get("srcset"),
                    }
                    for image in a.find_all("img")
                ],
            }
        )
    company_links = []
    for row in anchors:
        parsed = urlparse(row["href"])
        path_value = parsed.path.rstrip("/") + "/"
        if parsed.netloc.casefold().removeprefix("www.") == "greylock.com" and path_value.startswith("/portfolio/") and path_value != "/portfolio/":
            company_links.append(row)
    scripts = []
    inline_text = []
    for index, script in enumerate(soup.find_all("script"), start=1):
        src = script.get("src")
        text = script.string or script.get_text(" ", strip=False) or ""
        scripts.append(
            {
                "index": index,
                "src": urljoin(TARGET, src) if src else None,
                "type": script.get("type"),
                "id": script.get("id"),
                "bytes": len(text.encode("utf-8")),
                "text_prefix": text[:500],
            }
        )
        if text.strip():
            inline_text.append(text)
    write_json(root / "anchors.json", anchors)
    write_json(root / "company-link-candidates.json", company_links)
    write_json(root / "scripts.json", scripts)
    (root / "inline-scripts.txt").write_text("\n\n===== SCRIPT =====\n\n".join(inline_text), encoding="utf-8")
    html_text = response.text
    sanity_pairs = sorted(
        set(
            re.findall(
                r"cdn\.sanity\.io/(?:images|files)/([a-z0-9]+)/(?:([A-Za-z0-9_-]+))/",
                html_text,
                re.I,
            )
        )
    )
    return {
        "number": number,
        "sha256": sha256(response.content),
        "bytes": len(response.content),
        "anchor_count": len(anchors),
        "company_link_candidate_count": len(company_links),
        "company_link_unique_href_count": len({row["href"] for row in company_links}),
        "script_count": len(scripts),
        "sanity_pairs": sanity_pairs,
    }


def sanity_query(project: str, dataset: str, name: str, query: str) -> dict:
    endpoint = f"https://{project}.apicdn.sanity.io/v2025-02-19/data/query/{dataset}"
    response = fetch(endpoint, params={"query": query})
    save_bytes(
        RAW / "sanity" / f"{name}.json",
        response.content,
        {
            "requested_url": response.request.url,
            "final_url": response.url,
            "status": response.status_code,
            "content_type": response.headers.get("content-type"),
            "date": response.headers.get("date"),
            "query": query,
        },
    )
    return response.json()


def capture_sanity(project: str, dataset: str) -> dict:
    types_payload = sanity_query(project, dataset, "types", "array::unique(*[]._type)")
    types = sorted(types_payload.get("result") or [])
    interesting = [
        item
        for item in types
        if any(token in item.casefold() for token in ("portfolio", "company", "investment"))
    ]
    write_json(
        RAW / "sanity" / "type-summary.json",
        {"project": project, "dataset": dataset, "types": types, "interesting": interesting},
    )
    captured = {}
    for type_name in interesting:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", type_name)
        payload = sanity_query(project, dataset, f"type-{safe}", f'*[_type == {json.dumps(type_name)}]')
        captured[type_name] = len(payload.get("result") or [])
    broad_query = r'''*[
      defined(title) || defined(name)
    ]{
      _id,_type,_createdAt,_updatedAt,title,name,slug,url,website,link,href,
      externalUrl,companyUrl,domain,status,currentStatus,description,summary,
      logo,logoAlt,heroImage,category,categories,sector,sectors,stage,
      "keys": keys(@)
    }'''
    broad = sanity_query(project, dataset, "broad-named-docs", broad_query)
    return {
        "project": project,
        "dataset": dataset,
        "types": types,
        "interesting_types": interesting,
        "interesting_type_counts": captured,
        "broad_named_document_count": len(broad.get("result") or []),
    }


def capture_browser() -> dict:
    browser_root = RAW / "browser"
    browser_root.mkdir(parents=True, exist_ok=True)
    network_records: list[dict] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 1200},
            user_agent=UA,
            locale="en-US",
        )
        page = context.new_page()

        def on_response(response) -> None:
            url = response.url
            if not any(token in url for token in ("sanity", "_next/data", "/api/", "query")):
                return
            try:
                body = response.body()
            except Exception:  # noqa: BLE001
                return
            index = len(network_records) + 1
            suffix = ".json" if "json" in (response.headers.get("content-type") or "") else ".bin"
            safe = re.sub(r"[^A-Za-z0-9._-]+", "_", urlparse(url).path.strip("/") or "root")[:140]
            path = browser_root / "network" / f"{index:04d}_{safe}{suffix}"
            save_bytes(
                path,
                body,
                {
                    "url": url,
                    "status": response.status,
                    "content_type": response.headers.get("content-type"),
                    "request_method": response.request.method,
                    "request_post_data": response.request.post_data,
                    "fetched_at_epoch": time.time(),
                },
            )
            network_records.append({"url": url, "status": response.status, "path": str(path)})

        page.on("response", on_response)
        page.goto(TARGET, wait_until="networkidle", timeout=180_000)
        page.wait_for_timeout(5000)
        page.screenshot(path=str(browser_root / "full-page.png"), full_page=True)
        html = page.content().encode("utf-8")
        save_bytes(browser_root / "rendered.html", html, {"url": page.url, "fetched_at_epoch": time.time()})
        dom = page.evaluate(
            """
            () => {
              const visible = (el) => {
                const style = getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== 'none' && style.visibility !== 'hidden' &&
                       Number(style.opacity || 1) > 0 && rect.width > 0 && rect.height > 0;
              };
              const anchors = [...document.querySelectorAll('a[href]')].map((el) => {
                const rect = el.getBoundingClientRect();
                return {
                  href: el.href,
                  text: (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim(),
                  visible: visible(el),
                  className: typeof el.className === 'string' ? el.className : '',
                  ariaLabel: el.getAttribute('aria-label'),
                  title: el.getAttribute('title'),
                  y: rect.top + scrollY,
                  width: rect.width,
                  height: rect.height,
                  images: [...el.querySelectorAll('img')].map(img => ({alt: img.alt, src: img.currentSrc || img.src}))
                };
              });
              return {
                title: document.title,
                url: location.href,
                scrollHeight: document.documentElement.scrollHeight,
                anchors,
                buttons: [...document.querySelectorAll('button,[role=button]')].map(el => ({
                  text: (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim(),
                  visible: visible(el),
                  className: typeof el.className === 'string' ? el.className : ''
                }))
              };
            }
            """
        )
        write_json(browser_root / "dom.json", dom)
        write_json(browser_root / "network-index.json", network_records)
        browser.close()
    company_anchors = [
        row
        for row in dom["anchors"]
        if row["visible"]
        and urlparse(row["href"]).netloc.casefold().removeprefix("www.") == "greylock.com"
        and urlparse(row["href"]).path.rstrip("/").startswith("/portfolio/")
        and urlparse(row["href"]).path.rstrip("/") != "/portfolio"
    ]
    return {
        "title": dom["title"],
        "anchor_count": len(dom["anchors"]),
        "visible_company_anchor_count": len(company_anchors),
        "visible_company_unique_href_count": len({row["href"] for row in company_anchors}),
        "network_record_count": len(network_records),
        "scroll_height": dom["scrollHeight"],
    }


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    RAW.mkdir(parents=True)
    static = [static_capture(1), static_capture(2)]
    sanity_pairs = sorted({tuple(pair) for item in static for pair in item["sanity_pairs"]})
    sanity_summaries = []
    for project, dataset in sanity_pairs:
        try:
            sanity_summaries.append(capture_sanity(project, dataset))
        except Exception as exc:  # noqa: BLE001
            sanity_summaries.append({"project": project, "dataset": dataset, "error": repr(exc)})
    browser = capture_browser()
    summary = {
        "target": TARGET,
        "static": static,
        "static_hashes_equal": static[0]["sha256"] == static[1]["sha256"],
        "sanity_pairs": sanity_pairs,
        "sanity": sanity_summaries,
        "browser": browser,
        "generated_at_epoch": time.time(),
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
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        OUT.mkdir(parents=True, exist_ok=True)
        write_json(OUT / "fatal-error.json", {"error": repr(exc), "traceback": traceback.format_exc()})
        raise
