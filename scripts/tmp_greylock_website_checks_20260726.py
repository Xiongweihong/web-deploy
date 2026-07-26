from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

import requests

PROJECT = "uz672vkd"
DATASET = "production"
QUERY = '*[_type == "portfolioCompany"]|order(lower(name) asc)'
ENDPOINT = f"https://{PROJECT}.apicdn.sanity.io/v2025-02-19/data/query/{DATASET}"
OUT = Path("artifact") / "website-checks"
UA = "Mozilla/5.0 (compatible; greylock-evidence-crawler/1.1)"
WORKERS = 14


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def normalize_root(value: object) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return ""
    if text.startswith("//"):
        text = "https:" + text
    elif "://" not in text:
        text = "https://" + text
    parsed = urlparse(text)
    host = parsed.netloc.casefold().removeprefix("www.")
    return f"https://{host}" if host and "." in host else ""


def fetch_source() -> list[dict]:
    response = requests.get(
        ENDPOINT,
        params={"query": QUERY},
        timeout=120,
        headers={"User-Agent": UA, "Accept": "application/json"},
    )
    response.raise_for_status()
    raw = response.content
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "portfolio-companies.json").write_bytes(raw)
    write_json(
        OUT / "portfolio-companies.json.meta.json",
        {
            "requested_url": response.request.url,
            "status": response.status_code,
            "content_type": response.headers.get("content-type"),
            "date": response.headers.get("date"),
            "bytes": len(raw),
            "sha256": sha256(raw),
            "query": QUERY,
        },
    )
    payload = response.json()
    return payload.get("result") or []


def check_one(record: dict) -> dict:
    name = str(record.get("name") or "").strip()
    source_url = str(record.get("website") or "").strip()
    source_root = normalize_root(source_url)
    base = {
        "source_id": record.get("_id"),
        "name": name,
        "slug": (record.get("slug") or {}).get("current"),
        "status_label": record.get("status"),
        "source_url": source_url,
        "source_root": source_root,
    }
    if not source_url:
        return {**base, "status": None, "final_url": None, "final_root": None, "history": [], "error": "missing_source_website"}
    last_error = None
    for attempt in range(1, 3):
        try:
            with requests.get(
                source_url,
                timeout=(12, 25),
                allow_redirects=True,
                stream=True,
                headers={
                    "User-Agent": UA,
                    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                },
            ) as response:
                history = [
                    {
                        "status": hop.status_code,
                        "url": hop.url,
                        "location": hop.headers.get("location"),
                    }
                    for hop in response.history
                ]
                return {
                    **base,
                    "status": response.status_code,
                    "final_url": response.url,
                    "final_root": normalize_root(response.url),
                    "history": history,
                    "content_type": response.headers.get("content-type"),
                    "attempt": attempt,
                    "error": None,
                }
        except Exception as exc:  # noqa: BLE001
            last_error = repr(exc)
            if attempt < 2:
                time.sleep(1.0)
    return {**base, "status": None, "final_url": None, "final_root": None, "history": [], "error": last_error}


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    records = fetch_source()
    checks = []
    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = {executor.submit(check_one, record): record for record in records}
        for future in as_completed(futures):
            checks.append(future.result())
    checks.sort(key=lambda row: (str(row.get("name") or "").casefold(), str(row.get("source_id") or "")))
    write_json(OUT / "website-checks.json", checks)
    redirects = [row for row in checks if row.get("source_root") and row.get("final_root") and row["source_root"] != row["final_root"]]
    missing = [row for row in checks if row.get("error") == "missing_source_website"]
    errors = [row for row in checks if row.get("error") and row.get("error") != "missing_source_website"]
    summary = {
        "source_record_count": len(records),
        "check_count": len(checks),
        "missing_source_website_count": len(missing),
        "network_error_count": len(errors),
        "redirect_root_change_count": len(redirects),
        "status_counts": {},
        "missing": missing,
        "redirects": redirects,
        "errors": errors,
        "generated_at_epoch": time.time(),
    }
    for row in checks:
        key = str(row.get("status")) if row.get("status") is not None else "ERROR"
        summary["status_counts"][key] = summary["status_counts"].get(key, 0) + 1
    write_json(OUT / "website-check-summary.json", summary)
    print("WEBSITE_CHECK_SUMMARY_START")
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    print("WEBSITE_CHECK_SUMMARY_END")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        OUT.mkdir(parents=True, exist_ok=True)
        write_json(OUT / "fatal-error.json", {"error": repr(exc), "traceback": traceback.format_exc()})
        raise
