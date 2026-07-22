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

URL = "https://hashkey.capital/portfolio/index.html"
OUT = Path("artifact")
RAW = OUT / "raw"
NETWORK = RAW / "network"
UA = "Mozilla/5.0 (compatible; hashkey-portfolio-audit/1.0)"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def safe_name(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9._-]+", "_", value).strip("_")
    return value[:180] or "root"


def save(path: Path, data: bytes, meta: dict | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    info = {**(meta or {}), "bytes": len(data), "sha256": sha256(data)}
    path.with_suffix(path.suffix + ".meta.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )


def source_inventory(html: bytes, base_url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    scripts = [
        {
            "src": urljoin(base_url, node.get("src")) if node.get("src") else None,
            "type": node.get("type"),
            "text_length": len(node.get_text("", strip=False)),
            "text_sample": node.get_text("", strip=False)[:500] if not node.get("src") else None,
        }
        for node in soup.find_all("script")
    ]
    links = []
    for node in soup.find_all("a", href=True):
        links.append(
            {
                "href": urljoin(base_url, node.get("href")),
                "text": clean(node.get_text(" ", strip=True)),
                "title": node.get("title"),
                "aria_label": node.get("aria-label"),
                "class": node.get("class"),
                "data": {k: v for k, v in node.attrs.items() if str(k).startswith("data-")},
            }
        )
    images = []
    for node in soup.find_all("img"):
        images.append(
            {
                "src": urljoin(base_url, node.get("src")) if node.get("src") else None,
                "data_src": urljoin(base_url, node.get("data-src")) if node.get("data-src") else None,
                "alt": node.get("alt"),
                "title": node.get("title"),
                "class": node.get("class"),
                "parent_href": urljoin(base_url, node.parent.get("href")) if node.parent and node.parent.get("href") else None,
            }
        )
    stylesheets = [urljoin(base_url, node.get("href")) for node in soup.find_all("link", href=True)]
    return {
        "title": clean(soup.title.get_text(" ", strip=True)) if soup.title else "",
        "scripts": scripts,
        "links": links,
        "images": images,
        "stylesheets": stylesheets,
        "body_text": soup.get_text("\n", strip=True),
        "counts": {
            "scripts": len(scripts),
            "links": len(links),
            "images": len(images),
            "stylesheets": len(stylesheets),
        },
    }


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    RAW.mkdir(parents=True)
    NETWORK.mkdir(parents=True)

    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept": "text/html,*/*"})
    response = session.get(URL, timeout=60, allow_redirects=True)
    response.raise_for_status()
    save(
        RAW / "source.html",
        response.content,
        {
            "requested_url": URL,
            "final_url": response.url,
            "status": response.status_code,
            "content_type": response.headers.get("content-type"),
            "date": response.headers.get("date"),
            "etag": response.headers.get("etag"),
            "fetched_at_epoch": time.time(),
        },
    )
    inventory = source_inventory(response.content, response.url)
    (OUT / "source-inventory.json").write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )

    # Fetch source-declared scripts and styles directly as a second discovery path.
    asset_rows = []
    asset_urls = []
    for row in inventory["scripts"]:
        if row.get("src"):
            asset_urls.append(row["src"])
    asset_urls.extend(inventory["stylesheets"])
    for index, asset_url in enumerate(dict.fromkeys(asset_urls)):
        try:
            asset = session.get(asset_url, timeout=60, allow_redirects=True)
            suffix = Path(urlparse(asset.url).path).suffix or ".bin"
            path = RAW / "declared-assets" / f"{index:03d}_{safe_name(urlparse(asset.url).path)}{suffix if not safe_name(urlparse(asset.url).path).endswith(suffix) else ''}"
            save(
                path,
                asset.content,
                {
                    "requested_url": asset_url,
                    "final_url": asset.url,
                    "status": asset.status_code,
                    "content_type": asset.headers.get("content-type"),
                    "fetched_at_epoch": time.time(),
                },
            )
            text = asset.text if "text" in (asset.headers.get("content-type") or "") or suffix in {".js", ".css", ".json"} else ""
            asset_rows.append(
                {
                    "url": asset_url,
                    "final_url": asset.url,
                    "status": asset.status_code,
                    "bytes": len(asset.content),
                    "path": str(path),
                    "contains_portfolio": "portfolio" in text.casefold(),
                    "contains_company": "company" in text.casefold(),
                    "absolute_urls": sorted(set(re.findall(r"https?://[^\"'\\s<>]+", text)))[:500],
                    "json_paths": sorted(set(re.findall(r"[^\"'\\s<>]+\.json(?:\?[^\"'\\s<>]*)?", text, re.I)))[:500],
                    "image_paths": sorted(set(re.findall(r"[^\"'\\s<>]+\.(?:png|jpe?g|webp|svg)(?:\?[^\"'\\s<>]*)?", text, re.I)))[:1000],
                }
            )
        except Exception as exc:
            asset_rows.append({"url": asset_url, "error": repr(exc)})
    (OUT / "declared-assets-inventory.json").write_text(
        json.dumps(asset_rows, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )

    network_rows = []
    total_saved = 0
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(user_agent=UA, viewport={"width": 1440, "height": 1200}, locale="en-US")
        page = context.new_page()

        def on_response(resp) -> None:
            nonlocal total_saved
            url = resp.url
            ctype = (resp.headers.get("content-type") or "").lower()
            parsed = urlparse(url)
            relevant = (
                parsed.netloc.endswith("hashkey.capital")
                or any(token in ctype for token in ("json", "javascript", "text", "html", "css"))
                or any(token in url.casefold() for token in ("portfolio", "company", "api", "json"))
            )
            if not relevant:
                return
            row = {
                "url": url,
                "status": resp.status,
                "content_type": ctype,
                "method": resp.request.method,
                "resource_type": resp.request.resource_type,
                "post_data": resp.request.post_data,
            }
            try:
                body = resp.body()
                row["bytes"] = len(body)
                row["sha256"] = sha256(body)
                if len(body) <= 8_000_000 and total_saved + len(body) <= 80_000_000:
                    suffix = Path(parsed.path).suffix or (".json" if "json" in ctype else ".txt")
                    path = NETWORK / f"{len(network_rows):04d}_{safe_name(parsed.netloc + parsed.path)}{suffix if not safe_name(parsed.path).endswith(suffix) else ''}"
                    save(path, body, {**row, "fetched_at_epoch": time.time()})
                    row["path"] = str(path)
                    total_saved += len(body)
            except Exception as exc:
                row["body_error"] = repr(exc)
            network_rows.append(row)

        page.on("response", on_response)
        page.goto(URL, wait_until="domcontentloaded", timeout=120000)
        page.wait_for_timeout(5000)
        for _ in range(25):
            page.evaluate("window.scrollBy(0, Math.max(600, window.innerHeight * 0.8))")
            page.wait_for_timeout(350)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(2500)

        rendered = page.content().encode("utf-8")
        save(RAW / "rendered.html", rendered, {"url": page.url, "fetched_at_epoch": time.time()})
        (OUT / "rendered-body.txt").write_text(page.locator("body").inner_text(), encoding="utf-8")
        page.screenshot(path=str(OUT / "rendered-page.png"), full_page=True)

        dom = page.evaluate(
            """
            () => {
              const rows = [];
              for (const el of document.querySelectorAll('*')) {
                const cs = getComputedStyle(el);
                const text = (el.innerText || el.textContent || '').replace(/\s+/g,' ').trim();
                const attrs = {};
                for (const a of el.attributes || []) {
                  if (a.name.startsWith('data-') || ['href','src','alt','title','aria-label','id','class'].includes(a.name)) attrs[a.name] = a.value;
                }
                const bg = cs.backgroundImage && cs.backgroundImage !== 'none' ? cs.backgroundImage : '';
                if (text || Object.keys(attrs).length || bg) {
                  rows.push({tag: el.tagName, text: text.slice(0,500), attrs, backgroundImage:bg});
                }
              }
              return {
                url: location.href,
                title: document.title,
                rows,
                localStorage: {...localStorage},
                sessionStorage: {...sessionStorage},
                globals: Object.keys(window).filter(k => /portfolio|company|project|invest/i.test(k)).sort(),
              };
            }
            """
        )
        (OUT / "rendered-dom.json").write_text(
            json.dumps(dom, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )
        browser.close()

    (OUT / "network-inventory.json").write_text(
        json.dumps(network_rows, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )

    # Search saved text assets for likely data-bearing snippets.
    snippets = []
    for path in sorted(RAW.rglob("*")):
        if not path.is_file() or path.name.endswith(".meta.json") or path.stat().st_size > 8_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for pattern in ("portfolio", "company", "website", "project", "invest", "filter", "search"):
            for match in list(re.finditer(pattern, text, re.I))[:10]:
                snippets.append(
                    {
                        "file": str(path),
                        "pattern": pattern,
                        "position": match.start(),
                        "snippet": text[max(0, match.start() - 400): match.start() + 1000],
                    }
                )
    (OUT / "candidate-snippets.json").write_text(
        json.dumps(snippets[:3000], ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )

    summary = {
        "source_status": response.status_code,
        "source_bytes": len(response.content),
        "source_counts": inventory["counts"],
        "declared_asset_count": len(asset_rows),
        "network_response_count": len(network_rows),
        "network_saved_bytes": total_saved,
        "rendered_dom_rows": len(dom.get("rows", [])),
        "candidate_snippet_count": len(snippets),
    }
    (OUT / "discovery-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    print("DISCOVERY_SUMMARY_START")
    print(json.dumps(summary, indent=2, sort_keys=True))
    print("DISCOVERY_SUMMARY_END")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        OUT.mkdir(parents=True, exist_ok=True)
        error = {"status": "FATAL", "error": repr(exc), "traceback": traceback.format_exc()}
        (OUT / "fatal-error.json").write_text(json.dumps(error, indent=2), encoding="utf-8")
        print(error["traceback"])
        raise
