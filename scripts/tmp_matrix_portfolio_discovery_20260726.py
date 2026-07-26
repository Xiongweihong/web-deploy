from __future__ import annotations

import concurrent.futures
import hashlib
import json
import re
import shutil
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

TARGET = "https://matrix.vc/"
OUT = Path("artifact")
RAW = OUT / "raw"
UA = "Mozilla/5.0 (compatible; matrix-portfolio-evidence/2.0)"
HEADERS = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def save_bytes(path: Path, raw: bytes, meta: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    write_json(path.with_suffix(path.suffix + ".meta.json"), {**meta, "bytes": len(raw), "sha256": sha256(raw)})


def fetch(url: str) -> requests.Response:
    response = requests.get(url, headers=HEADERS, timeout=90, allow_redirects=True)
    response.raise_for_status()
    return response


def normalize_url(value: object) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text or text == "#":
        return ""
    if text.startswith("//"):
        text = "https:" + text
    elif "://" not in text:
        text = "https://" + text
    elif text.startswith("http://"):
        text = "https://" + text[7:]
    parsed = urlparse(text)
    host = parsed.netloc.casefold().removeprefix("www.")
    if not host or "." not in host:
        return ""
    path = (parsed.path or "").rstrip("/")
    return urlunparse(("https", host, path, "", "", ""))


def find_portfolio_list(soup: BeautifulSoup) -> object:
    section = soup.select_one("#portfolio")
    if section is None:
        raise RuntimeError("#portfolio section not found")
    candidates = []
    for index, node in enumerate(section.select(".w-dyn-list")):
        items = node.select(":scope > .w-dyn-items > .w-dyn-item")
        if len(items) >= 20:
            company_links = [
                a for a in node.select(":scope > .w-dyn-items > .w-dyn-item > a.text-size-large")
            ]
            candidates.append((len(items), len(company_links), index, node))
    if not candidates:
        raise RuntimeError("Portfolio CMS list candidate not found")
    candidates.sort(reverse=True, key=lambda x: (x[0], x[1]))
    return candidates[0][3]


def parse_page(raw: bytes, page_number: int, page_url: str) -> dict:
    soup = BeautifulSoup(raw, "lxml")
    node = find_portfolio_list(soup)
    items = node.select(":scope > .w-dyn-items > .w-dyn-item")
    records = []
    for item_index, item in enumerate(items, start=1):
        anchor = item.select_one(":scope > a.text-size-large")
        if anchor is None:
            anchor = item.find("a", href=True)
        name = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True) if anchor else "").strip()
        href = str(anchor.get("href") or "").strip() if anchor else ""
        categories = [
            re.sub(r"\s+", " ", tag.get_text(" ", strip=True)).strip()
            for tag in item.select("[fs-list-field='category']")
            if re.sub(r"\s+", " ", tag.get_text(" ", strip=True)).strip()
        ]
        records.append(
            {
                "source_position_on_page": item_index,
                "page_number": page_number,
                "name": name,
                "source_url": href,
                "source_url_normalized": normalize_url(href),
                "categories": categories,
                "item_attributes": dict(item.attrs),
                "item_html_sha256": sha256(str(item).encode("utf-8")),
            }
        )
    next_anchor = node.select_one(".w-pagination-next")
    prev_anchor = node.select_one(".w-pagination-previous")
    return {
        "page_number": page_number,
        "page_url": page_url,
        "record_count": len(records),
        "records": records,
        "next_href": str(next_anchor.get("href") or "").strip() if next_anchor else "",
        "previous_href": str(prev_anchor.get("href") or "").strip() if prev_anchor else "",
        "list_html_sha256": sha256(str(node).encode("utf-8")),
    }


def fetch_snapshot(snapshot: int) -> dict:
    pages = []
    current_url = TARGET
    seen = set()
    page_number = 1
    while page_number <= 20:
        if current_url in seen:
            raise RuntimeError(f"Repeated page URL: {current_url}")
        seen.add(current_url)
        response = fetch(current_url)
        raw = response.content
        path = RAW / f"snapshot-{snapshot:02d}" / f"page-{page_number:02d}.html"
        save_bytes(
            path,
            raw,
            {
                "requested_url": current_url,
                "final_url": response.url,
                "status": response.status_code,
                "content_type": response.headers.get("content-type"),
                "date": response.headers.get("date"),
                "etag": response.headers.get("etag"),
                "fetched_at_epoch": time.time(),
                "page_number": page_number,
            },
        )
        parsed = parse_page(raw, page_number, response.url)
        pages.append(parsed)
        if not parsed["next_href"]:
            break
        current_url = urljoin(response.url, parsed["next_href"])
        page_number += 1
    return {
        "pages": pages,
        "records": [record for page in pages for record in page["records"]],
    }


def website_check(record: dict) -> dict:
    url = record["source_url_normalized"]
    if not url:
        return {
            "name": record["name"],
            "source_url": record["source_url"],
            "status": None,
            "final_url": "",
            "final_root": "",
            "history": [],
            "error": "source URL missing",
        }
    try:
        response = requests.get(url, headers=HEADERS, timeout=35, allow_redirects=True, stream=True)
        final = normalize_url(response.url)
        result = {
            "name": record["name"],
            "source_url": url,
            "status": response.status_code,
            "final_url": response.url,
            "final_normalized": final,
            "final_root": (f"https://{urlparse(final).netloc}" if final else ""),
            "history": [
                {"status": prior.status_code, "url": prior.url, "location": prior.headers.get("location")}
                for prior in response.history
            ],
            "content_type": response.headers.get("content-type"),
            "error": "",
        }
        response.close()
        return result
    except Exception as exc:
        return {
            "name": record["name"],
            "source_url": url,
            "status": None,
            "final_url": "",
            "final_root": "",
            "history": [],
            "error": repr(exc),
        }


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    RAW.mkdir(parents=True)

    snapshot_1 = fetch_snapshot(1)
    time.sleep(2)
    snapshot_2 = fetch_snapshot(2)

    def record_map(snapshot: dict) -> list[tuple]:
        return [
            (
                record["page_number"],
                record["source_position_on_page"],
                record["name"],
                record["source_url"],
                tuple(record["categories"]),
            )
            for record in snapshot["records"]
        ]

    map_1 = record_map(snapshot_1)
    map_2 = record_map(snapshot_2)
    if map_1 != map_2:
        raise RuntimeError("Two portfolio snapshots differ")

    records = snapshot_2["records"]
    for index, record in enumerate(records, start=1):
        record["source_position"] = index

    names = [record["name"].casefold() for record in records]
    urls = [record["source_url_normalized"] for record in records if record["source_url_normalized"]]

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
        checks = list(executor.map(website_check, records))

    page_counts = [page["record_count"] for page in snapshot_2["pages"]]
    terminal_page = snapshot_2["pages"][-1]
    summary = {
        "status": "PASS",
        "page_count": len(snapshot_2["pages"]),
        "page_record_counts": page_counts,
        "record_count": len(records),
        "unique_name_count": len(set(names)),
        "unique_nonempty_source_url_count": len(set(urls)),
        "missing_name_count": sum(not record["name"] for record in records),
        "missing_source_url_count": sum(not record["source_url_normalized"] for record in records),
        "duplicate_name_groups": {key: value for key, value in Counter(names).items() if value > 1},
        "duplicate_source_url_groups": {key: value for key, value in Counter(urls).items() if value > 1},
        "snapshot_maps_equal": map_1 == map_2,
        "snapshot_1_page_hashes": [page["list_html_sha256"] for page in snapshot_1["pages"]],
        "snapshot_2_page_hashes": [page["list_html_sha256"] for page in snapshot_2["pages"]],
        "page_list_hashes_equal": [page["list_html_sha256"] for page in snapshot_1["pages"]] == [page["list_html_sha256"] for page in snapshot_2["pages"]],
        "terminal_next_href": terminal_page["next_href"],
        "terminal_previous_href": terminal_page["previous_href"],
        "website_check_status_counts": dict(sorted(Counter(str(check["status"]) if check["status"] is not None else "ERROR" for check in checks).items())),
        "first_10": records[:10],
        "last_10": records[-10:],
    }

    write_json(OUT / "snapshot-01.json", snapshot_1)
    write_json(OUT / "snapshot-02.json", snapshot_2)
    write_json(OUT / "portfolio-source-records.json", records)
    write_json(OUT / "website-checks.json", checks)
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
