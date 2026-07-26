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

TARGET = "https://matrix.vc/"
OUT = Path("artifact")
RAW = OUT / "raw"
UA = "Mozilla/5.0 (compatible; matrix-portfolio-evidence/1.0)"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def save_bytes(path: Path, raw: bytes, meta: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    write_json(path.with_suffix(path.suffix + ".meta.json"), {**meta, "bytes": len(raw), "sha256": sha256(raw)})


def fetch_static(index: int) -> dict:
    response = requests.get(TARGET, headers={"User-Agent": UA}, timeout=90)
    response.raise_for_status()
    raw = response.content
    path = RAW / f"static-{index:02d}" / "index.html"
    meta = {
        "requested_url": TARGET,
        "final_url": response.url,
        "status": response.status_code,
        "content_type": response.headers.get("content-type"),
        "date": response.headers.get("date"),
        "etag": response.headers.get("etag"),
        "fetched_at_epoch": time.time(),
    }
    save_bytes(path, raw, meta)
    soup = BeautifulSoup(raw, "lxml")
    return {
        "sha256": sha256(raw),
        "bytes": len(raw),
        "pagination_links": sorted({str(a.get("href")) for a in soup.find_all("a", href=True) if "_page=" in str(a.get("href"))}),
        "webflow_collection_lists": len(soup.select(".w-dyn-list")),
        "webflow_collection_items": len(soup.select(".w-dyn-item")),
    }


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    RAW.mkdir(parents=True)

    static_1 = fetch_static(1)
    time.sleep(2)
    static_2 = fetch_static(2)

    pages: list[dict] = []
    seen_urls: set[str] = set()
    all_records: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=UA, viewport={"width": 1440, "height": 1200})
        page = context.new_page()
        current_url = TARGET
        selected_index = None
        page_number = 1

        while page_number <= 20:
            if current_url in seen_urls:
                raise RuntimeError(f"Repeated pagination URL: {current_url}")
            seen_urls.add(current_url)

            response = page.goto(current_url, wait_until="domcontentloaded", timeout=120_000)
            try:
                page.wait_for_load_state("networkidle", timeout=30_000)
            except Exception:
                pass
            page.wait_for_timeout(1200)

            # Use the explicit Portfolio section if available; otherwise anchor from the heading.
            section = page.locator("#portfolio")
            if section.count() == 0:
                heading = page.get_by_text("Portfolio", exact=True).last
                section = heading.locator("xpath=ancestor::*[self::section or self::div][1]")
            if section.count() == 0:
                raise RuntimeError("Portfolio section not found")

            candidates = section.evaluate(
                """
                root => [...root.querySelectorAll('.w-dyn-list')].map((list, index) => {
                  const items = [...list.querySelectorAll(':scope > .w-dyn-items > .w-dyn-item')];
                  const allItems = items.length ? items : [...list.querySelectorAll('.w-dyn-item')];
                  const links = [...list.querySelectorAll('a[href]')];
                  const next = list.querySelector('.w-pagination-next');
                  const previous = list.querySelector('.w-pagination-previous');
                  return {
                    index,
                    className: list.className,
                    itemCount: allItems.length,
                    externalLinkCount: links.filter(a => {
                      try { const u = new URL(a.href, location.href); return u.hostname !== location.hostname; }
                      catch { return false; }
                    }).length,
                    nextHref: next ? next.href : '',
                    nextVisible: !!(next && (next.offsetWidth || next.offsetHeight || next.getClientRects().length)),
                    previousHref: previous ? previous.href : '',
                    textPreview: (list.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 500),
                    attributes: Object.fromEntries([...list.attributes].map(a => [a.name, a.value])),
                  };
                })
                """
            )

            if selected_index is None:
                eligible = [c for c in candidates if c["itemCount"] >= 20 and c["externalLinkCount"] >= 10]
                if not eligible:
                    eligible = [c for c in candidates if c["itemCount"] >= 20]
                if not eligible:
                    raise RuntimeError(f"No portfolio collection candidate: {candidates}")
                selected = max(eligible, key=lambda c: (c["itemCount"], c["externalLinkCount"]))
                selected_index = selected["index"]
            else:
                match = [c for c in candidates if c["index"] == selected_index]
                if not match:
                    raise RuntimeError(f"Selected collection index {selected_index} missing on page {page_number}")
                selected = match[0]

            list_locator = section.locator(".w-dyn-list").nth(selected_index)
            extracted = list_locator.evaluate(
                """
                list => {
                  let items = [...list.querySelectorAll(':scope > .w-dyn-items > .w-dyn-item')];
                  if (!items.length) items = [...list.querySelectorAll('.w-dyn-item')];
                  return items.map((item, index) => {
                    const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
                    const headings = [...item.querySelectorAll('h1,h2,h3,h4,h5,h6')]
                      .filter(visible)
                      .map(el => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim())
                      .filter(Boolean);
                    const links = [...item.querySelectorAll('a[href]')].map(a => {
                      let external = false;
                      try { external = new URL(a.href, location.href).hostname !== location.hostname; } catch {}
                      return {
                        href: a.href,
                        text: (a.innerText || a.textContent || '').replace(/\s+/g, ' ').trim(),
                        external,
                        visible: visible(a),
                        className: a.className,
                      };
                    });
                    const externalLinks = links.filter(a => a.external && a.visible);
                    const textLines = (item.innerText || item.textContent || '')
                      .split(/\n+/)
                      .map(s => s.replace(/\s+/g, ' ').trim())
                      .filter(Boolean);
                    return {
                      itemIndex: index,
                      headings,
                      textLines,
                      links,
                      chosenName: headings[0] || textLines[0] || '',
                      chosenUrl: externalLinks[0]?.href || '',
                      outerHTML: item.outerHTML,
                      attributes: Object.fromEntries([...item.attributes].map(a => [a.name, a.value])),
                    };
                  });
                }
                """
            )

            page_info = {
                "pageNumber": page_number,
                "url": page.url,
                "status": response.status if response else None,
                "candidates": candidates,
                "selectedIndex": selected_index,
                "selected": selected,
                "records": extracted,
            }
            pages.append(page_info)
            all_records.extend({**record, "pageNumber": page_number, "pageUrl": page.url} for record in extracted)

            html = page.content().encode("utf-8")
            save_bytes(
                RAW / f"browser/page-{page_number:02d}.html",
                html,
                {"url": page.url, "status": response.status if response else None, "fetched_at_epoch": time.time()},
            )
            page.screenshot(path=str(RAW / f"browser/page-{page_number:02d}.png"), full_page=True)

            next_href = selected.get("nextHref") or ""
            next_visible = bool(selected.get("nextVisible"))
            if not next_href or not next_visible:
                break
            current_url = urljoin(page.url, next_href)
            page_number += 1

        browser.close()

    # Normalize source rows conservatively. Keep all raw candidate fields for later audit.
    normalized = []
    for index, record in enumerate(all_records, start=1):
        name = re.sub(r"\s+", " ", record.get("chosenName") or "").strip()
        website = str(record.get("chosenUrl") or "").strip()
        normalized.append({
            "source_position": index,
            "page_number": record["pageNumber"],
            "name": name,
            "website": website,
            "text_lines": record.get("textLines", []),
            "headings": record.get("headings", []),
            "links": record.get("links", []),
            "attributes": record.get("attributes", {}),
        })

    write_json(OUT / "static-summary.json", {"snapshot_1": static_1, "snapshot_2": static_2})
    write_json(OUT / "browser-pages.json", pages)
    write_json(OUT / "normalized-candidates.json", normalized)

    summary = {
        "status": "DISCOVERY_COMPLETE",
        "static_hashes_equal": static_1["sha256"] == static_2["sha256"],
        "page_count": len(pages),
        "page_record_counts": [len(p["records"]) for p in pages],
        "raw_record_count": len(normalized),
        "nonempty_name_count": sum(bool(r["name"]) for r in normalized),
        "nonempty_website_count": sum(bool(r["website"]) for r in normalized),
        "unique_name_count_casefold": len({r["name"].casefold() for r in normalized if r["name"]}),
        "unique_website_count": len({r["website"] for r in normalized if r["website"]}),
        "selected_collection_index": selected_index,
        "terminal_next_visible": pages[-1]["selected"].get("nextVisible") if pages else None,
        "terminal_next_href": pages[-1]["selected"].get("nextHref") if pages else None,
        "first_10": normalized[:10],
        "last_10": normalized[-10:],
    }
    write_json(OUT / "discovery-summary.json", summary)

    manifest = {}
    for path in sorted(OUT.rglob("*")):
        if path.is_file():
            manifest[str(path.relative_to(OUT))] = {"bytes": path.stat().st_size, "sha256": sha256(path.read_bytes())}
    write_json(OUT / "manifest.json", manifest)

    print("DISCOVERY_SUMMARY_START")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("DISCOVERY_SUMMARY_END")


if __name__ == "__main__":
    main()
