from __future__ import annotations

import hashlib
import json
import shutil
import time
import traceback
from pathlib import Path

import requests

ORIGIN = "https://www.joinef.com"
OUT = Path("artifact")
RAW = OUT / "raw"
UA = "Mozilla/5.0 (compatible; joinef-evidence-crawler/1.1)"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def save(path: Path, response: requests.Response, extra: dict | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(response.content)
    meta = {
        "requested_url": response.request.url,
        "final_url": response.url,
        "status": response.status_code,
        "content_type": response.headers.get("content-type"),
        "date": response.headers.get("date"),
        "etag": response.headers.get("etag"),
        "x_wp_total": response.headers.get("x-wp-total"),
        "x_wp_totalpages": response.headers.get("x-wp-totalpages"),
        "bytes": len(response.content),
        "sha256": sha256(response.content),
    }
    if extra:
        meta.update(extra)
    path.with_suffix(path.suffix + ".meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )


def get(url: str, *, params: dict | None = None) -> requests.Response:
    last = None
    for attempt in range(1, 6):
        try:
            response = requests.get(
                url,
                params=params,
                timeout=90,
                headers={"User-Agent": UA, "Accept": "application/json,text/html;q=0.9,*/*;q=0.8"},
            )
            response.raise_for_status()
            return response
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < 5:
                time.sleep(min(20, 2**attempt))
    raise RuntimeError(f"GET failed: {url}: {last}")


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    RAW.mkdir(parents=True)

    summary: dict = {"company_pages": [], "generated_at_epoch": time.time()}

    for name, url, params in [
        ("company-search", f"{ORIGIN}/wp-json/wp/v2/company-search", None),
        ("company-page-1", f"{ORIGIN}/wp-json/wp/v2/company", {"per_page": 100, "page": 1, "orderby": "title", "order": "asc"}),
        ("company-page-1-acf", f"{ORIGIN}/wp-json/wp/v2/company", {"per_page": 100, "page": 1, "orderby": "title", "order": "asc", "acf_format": "standard"}),
        ("company-page-1-fields", f"{ORIGIN}/wp-json/wp/v2/company", {"per_page": 100, "page": 1, "orderby": "title", "order": "asc", "_fields": "id,date,modified,slug,status,link,title,content,excerpt,meta,acf"}),
    ]:
        response = get(url, params=params)
        save(RAW / f"{name}.json", response)
        try:
            payload = response.json()
            summary[name] = {
                "type": type(payload).__name__,
                "count": len(payload) if isinstance(payload, (list, dict)) else None,
                "first": payload[0] if isinstance(payload, list) and payload else payload,
                "headers": {
                    "x_wp_total": response.headers.get("x-wp-total"),
                    "x_wp_totalpages": response.headers.get("x-wp-totalpages"),
                },
            }
        except Exception as exc:  # noqa: BLE001
            summary[name] = {"json_error": repr(exc), "prefix": response.text[:1000]}

    page = 1
    all_records = []
    while True:
        response = get(
            f"{ORIGIN}/wp-json/wp/v2/company",
            params={"per_page": 100, "page": page, "orderby": "title", "order": "asc", "acf_format": "standard"},
        )
        save(RAW / "company-pages" / f"page-{page:03d}.json", response)
        payload = response.json()
        summary["company_pages"].append(
            {
                "page": page,
                "count": len(payload),
                "x_wp_total": int(response.headers.get("x-wp-total") or 0),
                "x_wp_totalpages": int(response.headers.get("x-wp-totalpages") or 0),
            }
        )
        all_records.extend(payload)
        total_pages = int(response.headers.get("x-wp-totalpages") or 1)
        if page >= total_pages:
            break
        page += 1

    summary["raw_record_count"] = len(all_records)
    summary["unique_id_count"] = len({record.get("id") for record in all_records})
    summary["record_keys"] = sorted({key for record in all_records for key in record})
    summary["acf_keys"] = sorted({key for record in all_records for key in (record.get("acf") or {})})
    summary["meta_keys"] = sorted({key for record in all_records for key in (record.get("meta") or {})})
    summary["sample_records"] = all_records[:5]

    (OUT / "api-probe-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    manifest = {"files": {}}
    for path in sorted(OUT.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            manifest["files"][str(path.relative_to(OUT))] = {
                "bytes": path.stat().st_size,
                "sha256": sha256(path.read_bytes()),
            }
    (OUT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    print("API_PROBE_SUMMARY_START")
    print(json.dumps({
        "raw_record_count": summary["raw_record_count"],
        "unique_id_count": summary["unique_id_count"],
        "record_keys": summary["record_keys"],
        "acf_keys": summary["acf_keys"],
        "company_pages": summary["company_pages"],
    }, ensure_ascii=False, indent=2, sort_keys=True))
    print("API_PROBE_SUMMARY_END")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "fatal-error.json").write_text(
            json.dumps({"error": repr(exc), "traceback": traceback.format_exc()}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        raise
