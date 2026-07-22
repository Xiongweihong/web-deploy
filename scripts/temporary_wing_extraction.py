from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
import time
import traceback
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlparse

TARGET_URL = "https://careers.wing.vc/companies"
ORIGIN = "https://careers.wing.vc"
COLLECTION_ID = "43520"
API_URL = f"https://api.getro.com/api/v2/collections/{COLLECTION_ID}/search/companies"
OUT = Path("artifact")
RAW = OUT / "raw"
NORMALIZED = OUT / "normalized"
USER_AGENT = "Mozilla/5.0 (compatible; wing-evidence-crawler/1.0)"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def first(record: dict, *keys: str):
    for key in keys:
        value = record.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def source_id(company: dict) -> str:
    value = first(company, "id", "objectID", "object_id", "slug")
    return clean(value) or "name:" + clean(first(company, "name", "title")).casefold()


def normalize_url(value: object) -> str:
    if isinstance(value, dict):
        value = value.get("url") or value.get("href") or value.get("value")
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
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    host = parsed.netloc.casefold().removeprefix("www.")
    return f"https://{host}"


def request(method: str, url: str, *, payload: dict | None = None, attempts: int = 5) -> tuple[bytes, dict]:
    body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {
        "accept": "application/json,text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        "user-agent": USER_AGENT,
    }
    if payload is not None:
        headers.update(
            {
                "content-type": "application/json",
                "origin": ORIGIN,
                "referer": TARGET_URL,
            }
        )
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=90) as response:
                raw = response.read()
                return raw, {
                    "requested_url": url,
                    "final_url": response.url,
                    "method": method,
                    "status": response.status,
                    "content_type": response.headers.get("content-type"),
                    "date": response.headers.get("date"),
                    "etag": response.headers.get("etag"),
                    "last_modified": response.headers.get("last-modified"),
                    "request_payload": payload,
                    "fetched_at_epoch": time.time(),
                    "bytes": len(raw),
                    "sha256": sha256(raw),
                }
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if isinstance(exc, urllib.error.HTTPError) and exc.code < 500 and exc.code not in {408, 429}:
                detail = exc.read().decode("utf-8", "replace")
                raise RuntimeError(f"HTTP {exc.code} for {url}: {detail[:1000]}") from exc
            if attempt < attempts:
                time.sleep(min(20, 2**attempt))
    raise RuntimeError(f"request failed after {attempts} attempts: {url}: {last_error}")


def save_raw(path: Path, raw: bytes, meta: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    path.with_suffix(path.suffix + ".meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )


def fetch_source_page(snapshot: int) -> dict:
    raw, meta = request("GET", TARGET_URL)
    path = RAW / f"snapshot-{snapshot:02d}" / "source-page.html"
    save_raw(path, raw, meta)
    text = raw.decode("utf-8", "replace")
    showing = re.search(r"Showing\s+([0-9,]+)\s+companies", text, re.I)
    network = re.search(r"across\s+([0-9,]+)\s+Wing portfolio companies", text, re.I)
    return {
        "displayed_company_count": int(showing.group(1).replace(",", "")) if showing else None,
        "network_marketing_count": int(network.group(1).replace(",", "")) if network else None,
        "sha256": meta["sha256"],
        "fetched_at_epoch": meta["fetched_at_epoch"],
    }


def fetch_company_snapshot(snapshot: int) -> dict:
    raw_dir = RAW / f"snapshot-{snapshot:02d}" / "companies"
    raw_dir.mkdir(parents=True, exist_ok=True)
    all_records: list[dict] = []
    page_summaries: list[dict] = []
    reported_total: int | None = None
    seen_page_hashes: set[str] = set()
    terminal: dict | None = None

    for page in range(100):
        payload = {"hitsPerPage": 100, "page": page, "query": "", "filters": ""}
        raw, meta = request("POST", API_URL, payload=payload)
        data = json.loads(raw)
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, dict):
            raise RuntimeError(f"page {page}: missing results object")
        records = results.get("companies") or []
        if not isinstance(records, list):
            raise RuntimeError(f"page {page}: companies is not a list")
        current_total = results.get("count")
        if reported_total is None:
            reported_total = int(current_total) if current_total is not None else None
        elif current_total is not None and int(current_total) != reported_total:
            raise RuntimeError(f"reported total changed inside snapshot: {reported_total} -> {current_total}")

        page_path = raw_dir / f"page-{page:06d}.json"
        meta.update(
            {
                "snapshot": snapshot,
                "page": page,
                "record_count": len(records),
                "reported_total": reported_total,
            }
        )
        save_raw(page_path, raw, meta)

        if records and meta["sha256"] in seen_page_hashes:
            raise RuntimeError(f"repeated non-empty page body at page {page}")
        seen_page_hashes.add(meta["sha256"])
        all_records.extend(records)
        page_summaries.append(
            {"page": page, "record_count": len(records), "sha256": meta["sha256"]}
        )
        print(
            f"snapshot={snapshot} page={page} records={len(records)} "
            f"accumulated={len(all_records)} total={reported_total}"
        )

        if not records:
            terminal = {"type": "empty_page", "page": page}
            break
        if reported_total is not None and len(all_records) >= reported_total:
            terminal = {
                "type": "reported_total_reached",
                "page": page,
                "count": len(all_records),
            }
            break
    else:
        raise RuntimeError("max page guard reached")

    ids = [source_id(record) for record in all_records]
    duplicate_ids = sorted(key for key, count in Counter(ids).items() if key and count > 1)
    empty_ids = [index for index, key in enumerate(ids) if not key]

    recheck_payload = {"hitsPerPage": 100, "page": 0, "query": "", "filters": ""}
    recheck_raw, recheck_meta = request("POST", API_URL, payload=recheck_payload)
    recheck_data = json.loads(recheck_raw)
    recheck_results = recheck_data.get("results") or {}
    recheck_records = recheck_results.get("companies") or []
    recheck_total = int(recheck_results.get("count") or 0)
    save_raw(raw_dir / "page-zero-recheck.json", recheck_raw, recheck_meta)

    first_page_count = page_summaries[0]["record_count"] if page_summaries else 0
    first_page_ids = [source_id(record) for record in all_records[:first_page_count]]
    recheck_ids = [source_id(record) for record in recheck_records]
    stable = (
        reported_total is not None
        and reported_total == recheck_total
        and len(all_records) == reported_total
        and len(set(ids)) == reported_total
        and not duplicate_ids
        and not empty_ids
        and first_page_ids == recheck_ids
    )
    summary = {
        "snapshot": snapshot,
        "reported_total": reported_total,
        "reported_total_after": recheck_total,
        "raw_count": len(all_records),
        "unique_id_count": len(set(ids)),
        "duplicate_ids": duplicate_ids,
        "empty_id_indexes": empty_ids,
        "page_count": len(page_summaries),
        "page_sizes": [item["record_count"] for item in page_summaries],
        "terminal": terminal,
        "first_page_stable": first_page_ids == recheck_ids,
        "stable": stable,
    }
    (raw_dir / "snapshot-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    return {"records": all_records, "summary": summary, "raw_dir": raw_dir}


def normalize_companies(records: list[dict]) -> list[dict]:
    rows = []
    for company in records:
        domain_value = first(company, "domain", "website", "website_url", "url")
        website = normalize_url(domain_value)
        rows.append(
            {
                "source_id": source_id(company),
                "source_slug": clean(first(company, "slug")),
                "name": clean(first(company, "name", "title")),
                "website": website,
                "source_domain": clean(domain_value),
                "active_jobs_count": first(company, "active_jobs_count", "jobs_count"),
                "website_provenance": "getro_company_domain" if website else "missing",
            }
        )
    rows.sort(key=lambda row: (row["name"].casefold(), row["source_id"]))
    return rows


def reconstruct_from_raw(raw_dir: Path) -> tuple[list[dict], dict]:
    records: list[dict] = []
    hash_errors: list[str] = []
    totals: list[int] = []
    for path in sorted(raw_dir.glob("page-*.json")):
        if path.name == "page-zero-recheck.json":
            continue
        meta_path = path.with_suffix(path.suffix + ".meta.json")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        raw = path.read_bytes()
        if sha256(raw) != meta.get("sha256"):
            hash_errors.append(path.name)
        data = json.loads(raw)
        results = data.get("results") or {}
        records.extend(results.get("companies") or [])
        if results.get("count") is not None:
            totals.append(int(results["count"]))
    return normalize_companies(records), {
        "raw_hash_errors": hash_errors,
        "reported_totals": sorted(set(totals)),
        "raw_record_count": len(records),
    }


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    RAW.mkdir(parents=True)
    NORMALIZED.mkdir(parents=True)

    source_pages = [fetch_source_page(1), fetch_source_page(2)]
    captures = [fetch_company_snapshot(1), fetch_company_snapshot(2)]
    rows_by_snapshot = [normalize_companies(capture["records"]) for capture in captures]
    rows = rows_by_snapshot[-1]

    txt_path = NORMALIZED / "wing-companies.txt"
    json_path = NORMALIZED / "wing-companies.json"
    txt_path.write_text(
        "\n".join(f'{row["name"]} + {row["website"]}' for row in rows)
        + ("\n" if rows else ""),
        encoding="utf-8",
    )
    json_path.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )

    snapshot_maps = [
        {
            row["source_id"]: (
                row["name"],
                row["website"],
                row["active_jobs_count"],
            )
            for row in snapshot_rows
        }
        for snapshot_rows in rows_by_snapshot
    ]
    snapshot_comparison = {
        "status": "PASS" if snapshot_maps[0] == snapshot_maps[1] else "FAIL",
        "snapshot_1_count": len(snapshot_maps[0]),
        "snapshot_2_count": len(snapshot_maps[1]),
        "added_ids": sorted(set(snapshot_maps[1]) - set(snapshot_maps[0])),
        "removed_ids": sorted(set(snapshot_maps[0]) - set(snapshot_maps[1])),
        "changed_ids": sorted(
            key
            for key in set(snapshot_maps[0]) & set(snapshot_maps[1])
            if snapshot_maps[0][key] != snapshot_maps[1][key]
        ),
    }
    (OUT / "snapshot-comparison.json").write_text(
        json.dumps(snapshot_comparison, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    reconstructed_rows, audit_meta = reconstruct_from_raw(captures[-1]["raw_dir"])
    primary_map = {
        row["source_id"]: (row["name"], row["website"], row["active_jobs_count"])
        for row in rows
    }
    audit_map = {
        row["source_id"]: (row["name"], row["website"], row["active_jobs_count"])
        for row in reconstructed_rows
    }
    txt_lines = [line for line in txt_path.read_text(encoding="utf-8").splitlines() if line]
    expected_lines = [f'{row["name"]} + {row["website"]}' for row in rows]
    independent_audit = {
        **audit_meta,
        "reconstructed_count": len(reconstructed_rows),
        "source_id_maps_equal": primary_map == audit_map,
        "txt_lines_equal": txt_lines == expected_lines,
        "txt_line_count": len(txt_lines),
    }
    independent_audit["status"] = (
        "PASS"
        if not independent_audit["raw_hash_errors"]
        and primary_map == audit_map
        and txt_lines == expected_lines
        else "FAIL"
    )
    (OUT / "independent-audit.json").write_text(
        json.dumps(independent_audit, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    ids = [row["source_id"] for row in rows]
    names = [row["name"].casefold() for row in rows]
    domains = [urlparse(row["website"]).netloc.casefold().removeprefix("www.") for row in rows if row["website"]]
    duplicate_ids = {key: count for key, count in Counter(ids).items() if count > 1}
    duplicate_names = {key: count for key, count in Counter(names).items() if count > 1}
    duplicate_domains = {key: count for key, count in Counter(domains).items() if count > 1}
    missing_names = [row for row in rows if not row["name"]]
    missing_websites = [row for row in rows if not row["website"]]
    invalid_websites = []
    forbidden_hosts = {"careers.wing.vc", "api.getro.com", "getro.com"}
    for row in rows:
        if not row["website"]:
            continue
        parsed = urlparse(row["website"])
        host = parsed.netloc.casefold().removeprefix("www.")
        if parsed.scheme != "https" or not host or host in forbidden_hosts:
            invalid_websites.append(row)

    displayed_counts = [page["displayed_company_count"] for page in source_pages]
    api_total = captures[-1]["summary"]["reported_total"]
    errors: list[str] = []
    if displayed_counts[0] != displayed_counts[1]:
        errors.append("displayed company count changed between source-page snapshots")
    if displayed_counts[-1] != api_total:
        errors.append("displayed company count does not match API total")
    if any(not capture["summary"]["stable"] for capture in captures):
        errors.append("at least one API snapshot was not internally stable")
    if api_total != len(rows):
        errors.append("API reported total does not equal normalized count")
    if len(set(ids)) != len(rows):
        errors.append("normalized source IDs are not unique")
    if duplicate_ids:
        errors.append("duplicate source IDs")
    if duplicate_names:
        errors.append("duplicate company names")
    if duplicate_domains:
        errors.append("duplicate company domains")
    if missing_names:
        errors.append("missing company names")
    if missing_websites:
        errors.append("missing company websites")
    if invalid_websites:
        errors.append("invalid company websites")
    if snapshot_comparison["status"] != "PASS":
        errors.append("two API snapshots differ")
    if independent_audit["status"] != "PASS":
        errors.append("independent audit failed")

    validation = {
        "source_url": TARGET_URL,
        "provider": "Getro",
        "collection_id": COLLECTION_ID,
        "status": "PASS" if not errors else "NEEDS_REVIEW",
        "source_page_snapshots": source_pages,
        "displayed_company_counts": displayed_counts,
        "network_marketing_counts": [page["network_marketing_count"] for page in source_pages],
        "api_snapshots": [capture["summary"] for capture in captures],
        "api_reported_total": api_total,
        "normalized_company_count": len(rows),
        "output_line_count": len(txt_lines),
        "unique_source_id_count": len(set(ids)),
        "unique_name_count": len(set(names)),
        "unique_domain_count": len(set(domains)),
        "missing_name_count": len(missing_names),
        "missing_website_count": len(missing_websites),
        "invalid_website_count": len(invalid_websites),
        "duplicate_id_groups": duplicate_ids,
        "duplicate_name_groups": duplicate_names,
        "duplicate_domain_groups": duplicate_domains,
        "missing_names": missing_names,
        "missing_websites": missing_websites,
        "invalid_websites": invalid_websites,
        "snapshot_comparison": snapshot_comparison,
        "independent_audit": independent_audit,
        "errors": errors,
        "notes": [
            "The displayed 88-company count is the authoritative scope for the target companies page.",
            "The separate marketing statement referencing 91 portfolio companies is retained as context but is not used as the page-list total.",
            "Websites are normalized from Getro company domain/website fields without name-to-domain guessing.",
        ],
    }
    (OUT / "validation.json").write_text(
        json.dumps(validation, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    manifest = {"generated_at_epoch": time.time(), "status": validation["status"], "files": {}}
    for path in sorted(OUT.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            manifest["files"][str(path.relative_to(OUT))] = {
                "bytes": path.stat().st_size,
                "sha256": sha256(path.read_bytes()),
            }
    (OUT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    print("VALIDATION_SUMMARY_START")
    print(
        json.dumps(
            {
                "status": validation["status"],
                "displayed_company_counts": displayed_counts,
                "network_marketing_counts": validation["network_marketing_counts"],
                "api_reported_total": api_total,
                "normalized_company_count": len(rows),
                "output_line_count": len(txt_lines),
                "missing_website_count": len(missing_websites),
                "duplicate_name_groups": duplicate_names,
                "duplicate_domain_groups": duplicate_domains,
                "snapshot_comparison": snapshot_comparison["status"],
                "independent_audit": independent_audit["status"],
                "errors": errors,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    print("VALIDATION_SUMMARY_END")
    if errors:
        sys.exit(2)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        OUT.mkdir(parents=True, exist_ok=True)
        error = {
            "status": "FATAL",
            "error": repr(exc),
            "traceback": traceback.format_exc(),
        }
        (OUT / "fatal-error.json").write_text(
            json.dumps(error, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )
        print(error["traceback"], file=sys.stderr)
        sys.exit(1)
