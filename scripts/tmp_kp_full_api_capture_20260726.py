from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
import traceback
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

BASE = "https://www.kleinerperkins.com"
TARGET = f"{BASE}/partnerships/"
API = f"{BASE}/wp-json/wp/v2/company"
OUT = Path("artifact-full-api")
RAW = OUT / "raw"
UA = "Mozilla/5.0 (compatible; kp-evidence-crawler/1.2)"

def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()

def normalize_url(value: object) -> str:
    text = clean(value)
    if not text:
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
    return f"https://{host}{path}" if path else f"https://{host}"

def write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

def fetch(url: str, params: dict | None = None, attempts: int = 6) -> tuple[bytes, dict]:
    last = None
    for attempt in range(1, attempts + 1):
        try:
            r = requests.get(
                url,
                params=params,
                timeout=120,
                allow_redirects=True,
                headers={"User-Agent": UA, "Accept": "application/json,text/html;q=0.9,*/*;q=0.8"},
            )
            r.raise_for_status()
            return r.content, {
                "requested_url": r.request.url,
                "final_url": r.url,
                "status": r.status_code,
                "content_type": r.headers.get("content-type"),
                "date": r.headers.get("date"),
                "etag": r.headers.get("etag"),
                "last_modified": r.headers.get("last-modified"),
                "x_wp_total": r.headers.get("x-wp-total"),
                "x_wp_totalpages": r.headers.get("x-wp-totalpages"),
                "fetched_at_epoch": time.time(),
                "attempt": attempt,
                "bytes": len(r.content),
                "sha256": sha256(r.content),
            }
        except Exception as exc:
            last = exc
            if attempt < attempts:
                time.sleep(min(30, 2 ** attempt))
    raise RuntimeError(f"GET failed: {url}: {last!r}")

def save(path: Path, raw: bytes, meta: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    write_json(path.with_suffix(path.suffix + ".meta.json"), meta)

def parse_page(raw: bytes) -> list[dict]:
    soup = BeautifulSoup(raw, "lxml")
    records = []
    for panel in soup.select(".js-companies[data-id][data-current]"):
        heading = panel.select_one("h2")
        name = clean(heading.get_text(" ", strip=True)) if heading else ""
        website = ""
        for anchor in panel.find_all("a", href=True):
            if clean(anchor.get_text(" ", strip=True)).casefold() == "website":
                website = clean(anchor.get("href"))
                break
        records.append({
            "source_id": int(panel.get("data-id")),
            "source_position": int(panel.get("data-current")),
            "name": name,
            "html_website": website,
            "html_website_normalized": normalize_url(website),
        })
    records.sort(key=lambda row: row["source_position"])
    return records

def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    RAW.mkdir(parents=True)

    page_captures = []
    for index in (1, 2):
        raw, meta = fetch(TARGET)
        save(RAW / f"snapshot-{index:02d}" / "partnerships.html", raw, meta)
        page_captures.append((raw, meta))
        time.sleep(2)
    page_records = parse_page(page_captures[-1][0])
    write_json(OUT / "page-records.json", page_records)

    api_records = []
    api_page_sizes = []
    total = None
    total_pages = None
    page = 1
    while True:
        params = {
            "per_page": 100,
            "page": page,
            "orderby": "title",
            "order": "asc",
            "acf_format": "standard",
            "_fields": "id,slug,title,link,modified,acf,sector,stage",
        }
        raw, meta = fetch(API, params=params)
        save(RAW / "api" / f"page-{page:03d}.json", raw, meta)
        payload = json.loads(raw)
        if not isinstance(payload, list):
            raise RuntimeError("company API payload is not a list")
        api_records.extend(payload)
        api_page_sizes.append(len(payload))
        total = int(meta.get("x_wp_total") or total or 0)
        total_pages = int(meta.get("x_wp_totalpages") or total_pages or 1)
        if page >= total_pages:
            break
        page += 1

    api_by_id = {int(record["id"]): record for record in api_records}
    page_ids = [record["source_id"] for record in page_records]
    missing_api_ids = sorted(set(page_ids) - set(api_by_id))
    hidden_api_ids = sorted(set(api_by_id) - set(page_ids))

    aligned = []
    title_mismatches = []
    website_mismatches = []
    for page_record in page_records:
        record = api_by_id.get(page_record["source_id"])
        if not record:
            continue
        acf = record.get("acf") or {}
        api_name = clean((record.get("title") or {}).get("rendered"))
        api_website = clean(acf.get("website_url"))
        item = {
            **page_record,
            "slug": clean(record.get("slug")),
            "api_name": api_name,
            "api_website": api_website,
            "api_website_normalized": normalize_url(api_website),
            "x_url": clean(acf.get("x_url")),
            "linkedin_url": clean(acf.get("linkedin_url")),
            "subhead": clean(acf.get("subhead")),
            "timing": clean(acf.get("timing")),
            "since_text": clean(acf.get("since_text")),
            "since_caption": clean(acf.get("since_caption")),
            "modified": record.get("modified"),
        }
        if api_name.casefold() != page_record["name"].casefold():
            title_mismatches.append(item)
        if item["api_website_normalized"] != page_record["html_website_normalized"]:
            website_mismatches.append(item)
        aligned.append(item)

    hidden_records = []
    for source_id in hidden_api_ids:
        record = api_by_id[source_id]
        acf = record.get("acf") or {}
        hidden_records.append({
            "source_id": source_id,
            "slug": clean(record.get("slug")),
            "name": clean((record.get("title") or {}).get("rendered")),
            "website": clean(acf.get("website_url")),
            "modified": record.get("modified"),
        })

    write_json(OUT / "aligned-page-api-records.json", aligned)
    write_json(OUT / "hidden-api-records.json", hidden_records)
    write_json(OUT / "all-api-records.json", api_records)
    write_json(OUT / "title-mismatches.json", title_mismatches)
    write_json(OUT / "website-mismatches.json", website_mismatches)

    website_count = sum(bool(row["api_website"]) for row in aligned)
    summary = {
        "page_capture_hashes": [meta["sha256"] for _, meta in page_captures],
        "page_capture_hashes_equal": page_captures[0][1]["sha256"] == page_captures[1][1]["sha256"],
        "page_company_count": len(page_records),
        "page_unique_id_count": len(set(page_ids)),
        "page_unique_name_count": len({row["name"].casefold() for row in page_records}),
        "page_website_count": sum(bool(row["html_website"]) for row in page_records),
        "api_reported_total": total,
        "api_reported_total_pages": total_pages,
        "api_page_sizes": api_page_sizes,
        "api_record_count": len(api_records),
        "api_unique_id_count": len(api_by_id),
        "page_ids_missing_from_api": missing_api_ids,
        "api_ids_not_on_page_count": len(hidden_api_ids),
        "api_ids_not_on_page": hidden_api_ids,
        "aligned_record_count": len(aligned),
        "aligned_website_count": website_count,
        "title_mismatch_count": len(title_mismatches),
        "website_mismatch_count": len(website_mismatches),
        "missing_website_count": len(aligned) - website_count,
        "missing_website_names": [row["name"] for row in aligned if not row["api_website"]],
        "hidden_api_records": hidden_records,
    }
    write_json(OUT / "full-api-summary.json", summary)

    manifest = {"files": {}}
    for path in sorted(OUT.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            manifest["files"][str(path.relative_to(OUT))] = {
                "bytes": path.stat().st_size,
                "sha256": sha256(path.read_bytes()),
            }
    write_json(OUT / "manifest.json", manifest)

    print("FULL_API_SUMMARY_START")
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    print("FULL_API_SUMMARY_END")

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        OUT.mkdir(parents=True, exist_ok=True)
        write_json(OUT / "fatal-error.json", {"error": repr(exc), "traceback": traceback.format_exc()})
        raise
