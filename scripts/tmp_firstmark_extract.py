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
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

COLLECTION_ID = "45303"
ORIGIN = "https://jobs.firstmark.com"
SOURCE_URL = ORIGIN + "/companies"
API_URL = f"https://api.getro.com/api/v2/collections/{COLLECTION_ID}/search/companies"
OUT = Path("artifact")
RAW = OUT / "raw"
NORMALIZED = OUT / "normalized"
USER_AGENT = "Mozilla/5.0 (compatible; firstmark-evidence-crawler/1.0)"


def digest(data: bytes) -> str:
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
    if value not in (None, ""):
        return clean(value)
    return "name:" + clean(first(company, "name", "title")).casefold()


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


def request_bytes(
    method: str,
    url: str,
    *,
    payload: dict | None = None,
    attempts: int = 5,
    timeout: int = 90,
) -> tuple[bytes, dict]:
    body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {
        "accept": "application/json,text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        "user-agent": USER_AGENT,
    }
    if payload is not None:
        headers.update(
            {
                "accept": "application/json",
                "content-type": "application/json",
                "origin": ORIGIN,
                "referer": SOURCE_URL,
            }
        )
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
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
                    "sha256": digest(raw),
                }
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if isinstance(exc, urllib.error.HTTPError) and exc.code < 500 and exc.code not in {408, 429}:
                detail = exc.read().decode("utf-8", "replace")
                raise RuntimeError(f"HTTP {exc.code} for {url}: {detail[:1500]}") from exc
            if attempt < attempts:
                time.sleep(min(20, 2**attempt))
    raise RuntimeError(f"request failed after {attempts} attempts: {url}: {last_error}")


def save_raw(path: Path, raw: bytes, meta: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    path.with_suffix(path.suffix + ".meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )


def fetch_snapshot(snapshot_number: int) -> dict:
    snapshot_dir = RAW / f"snapshot-{snapshot_number:02d}"
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    source_raw, source_meta = request_bytes("GET", SOURCE_URL)
    save_raw(snapshot_dir / "source-page.html", source_raw, source_meta)
    source_text = source_raw.decode("utf-8", "replace")
    displayed_match = re.search(r"Showing\s+([0-9,]+)\s+companies", source_text, re.I)
    displayed_count = int(displayed_match.group(1).replace(",", "")) if displayed_match else None

    all_companies: list[dict] = []
    page_summaries: list[dict] = []
    reported_total: int | None = None
    first_page_ids: list[str] = []
    seen_nonempty_hashes: set[str] = set()
    terminal: dict | None = None

    for page in range(1000):
        payload = {"hitsPerPage": 100, "page": page, "query": "", "filters": ""}
        raw, meta = request_bytes("POST", API_URL, payload=payload)
        path = snapshot_dir / "pages" / f"page-{page:06d}.json"
        data = json.loads(raw)
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, dict):
            raise RuntimeError(f"page {page}: missing results object")
        companies = results.get("companies") or []
        if not isinstance(companies, list):
            raise RuntimeError(f"page {page}: companies is not a list")
        current_total = results.get("count")
        if current_total is not None:
            current_total = int(current_total)
        if reported_total is None:
            reported_total = current_total
        elif current_total is not None and current_total != reported_total:
            raise RuntimeError(f"reported total changed within snapshot: {reported_total} -> {current_total}")

        meta.update(
            {
                "snapshot": snapshot_number,
                "page": page,
                "record_count": len(companies),
                "reported_total": reported_total,
            }
        )
        save_raw(path, raw, meta)

        if companies and meta["sha256"] in seen_nonempty_hashes:
            raise RuntimeError(f"repeated non-empty page body at page {page}")
        if companies:
            seen_nonempty_hashes.add(meta["sha256"])

        ids = [source_id(company) for company in companies]
        if page == 0:
            first_page_ids = ids
        all_companies.extend(companies)
        page_summaries.append(
            {
                "page": page,
                "record_count": len(companies),
                "sha256": meta["sha256"],
                "first_id": ids[0] if ids else None,
                "last_id": ids[-1] if ids else None,
            }
        )
        print(
            f"snapshot={snapshot_number} page={page} records={len(companies)} "
            f"accumulated={len(all_companies)} total={reported_total}"
        )

        if not companies:
            terminal = {"type": "empty_page", "page": page}
            break
        if reported_total is not None and len(all_companies) >= reported_total:
            terminal = {"type": "reported_total_reached", "page": page, "count": len(all_companies)}
            break
    else:
        raise RuntimeError("max page guard reached")

    recheck_payload = {"hitsPerPage": 100, "page": 0, "query": "", "filters": ""}
    recheck_raw, recheck_meta = request_bytes("POST", API_URL, payload=recheck_payload)
    recheck_data = json.loads(recheck_raw)
    recheck_results = (recheck_data.get("results") or {}) if isinstance(recheck_data, dict) else {}
    recheck_companies = recheck_results.get("companies") or []
    recheck_total = int(recheck_results.get("count") or 0)
    recheck_meta.update(
        {
            "snapshot": snapshot_number,
            "purpose": "first_page_recheck",
            "record_count": len(recheck_companies),
            "reported_total": recheck_total,
        }
    )
    save_raw(snapshot_dir / "page-zero-recheck.json", recheck_raw, recheck_meta)

    ids = [source_id(company) for company in all_companies]
    duplicate_ids = sorted(key for key, count in Counter(ids).items() if key and count > 1)
    empty_ids = [index for index, key in enumerate(ids) if not key]
    first_page_ids_after = [source_id(company) for company in recheck_companies]
    stable = (
        reported_total is not None
        and reported_total == recheck_total
        and len(all_companies) == reported_total
        and len(set(ids)) == reported_total
        and not duplicate_ids
        and not empty_ids
        and first_page_ids == first_page_ids_after
    )

    summary = {
        "snapshot": snapshot_number,
        "captured_at_epoch": time.time(),
        "source_page_sha256": source_meta["sha256"],
        "source_displayed_count": displayed_count,
        "reported_total": reported_total,
        "reported_total_after": recheck_total,
        "raw_count": len(all_companies),
        "unique_id_count": len(set(ids)),
        "duplicate_ids": duplicate_ids,
        "empty_id_indexes": empty_ids,
        "first_page_stable": first_page_ids == first_page_ids_after,
        "page_count": len(page_summaries),
        "page_sizes": [item["record_count"] for item in page_summaries],
        "terminal": terminal,
        "stable": stable,
    }
    (snapshot_dir / "snapshot-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    return {"companies": all_companies, "summary": summary, "dir": snapshot_dir}


def normalize_companies(companies: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for company in companies:
        domain_value = first(company, "domain", "website", "website_url")
        website = normalize_url(domain_value)
        rows.append(
            {
                "source_id": source_id(company),
                "source_slug": clean(first(company, "slug")),
                "name": clean(first(company, "name", "title")),
                "website": website,
                "raw_domain": clean(domain_value),
                "website_provenance": "getro_company_domain" if website else "missing",
                "active_jobs_count": first(company, "active_jobs_count", "jobs_count", "job_count"),
                "source_record": company,
            }
        )
    rows.sort(key=lambda row: (row["name"].casefold(), row["source_id"]))
    return rows


def independent_audit(snapshot_dir: Path, normalized_rows: list[dict], txt_path: Path) -> dict:
    raw_hash_errors: list[str] = []
    audit_companies: list[dict] = []
    reported_totals: list[int] = []

    for raw_path in sorted((snapshot_dir / "pages").glob("page-*.json")):
        meta_path = raw_path.with_suffix(raw_path.suffix + ".meta.json")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if digest(raw_path.read_bytes()) != meta.get("sha256"):
            raw_hash_errors.append(str(raw_path.relative_to(snapshot_dir)))
        payload = json.loads(raw_path.read_bytes())
        results = payload.get("results") or {}
        if results.get("count") is not None:
            reported_totals.append(int(results.get("count")))
        records = results.get("companies") or []
        audit_companies.extend(records)

    audit_rows: list[dict] = []
    for record in audit_companies:
        rid = clean(record.get("id") or record.get("objectID") or record.get("object_id") or record.get("slug"))
        if not rid:
            rid = "name:" + clean(record.get("name") or record.get("title")).casefold()
        name = clean(record.get("name") or record.get("title"))
        raw_domain = record.get("domain") or record.get("website") or record.get("website_url") or ""
        if isinstance(raw_domain, dict):
            raw_domain = raw_domain.get("url") or raw_domain.get("href") or raw_domain.get("value") or ""
        raw_domain = clean(raw_domain)
        website = ""
        if raw_domain:
            candidate = raw_domain
            if candidate.startswith("//"):
                candidate = "https:" + candidate
            elif "://" not in candidate:
                candidate = "https://" + candidate
            elif candidate.startswith("http://"):
                candidate = "https://" + candidate[7:]
            parsed = urlparse(candidate)
            if parsed.scheme in {"http", "https"} and parsed.netloc:
                website = "https://" + parsed.netloc.casefold().removeprefix("www.")
        audit_rows.append(
            {
                "source_id": rid,
                "name": name,
                "website": website,
                "active_jobs_count": record.get("active_jobs_count")
                or record.get("jobs_count")
                or record.get("job_count"),
            }
        )
    audit_rows.sort(key=lambda row: (row["name"].casefold(), row["source_id"]))

    primary_map = {
        row["source_id"]: (row["name"], row["website"], row["active_jobs_count"])
        for row in normalized_rows
    }
    audit_map = {
        row["source_id"]: (row["name"], row["website"], row["active_jobs_count"])
        for row in audit_rows
    }
    txt_lines = [line for line in txt_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    expected_lines = [f'{row["name"]} + {row["website"]}' for row in normalized_rows]
    reported_total = reported_totals[0] if reported_totals else None
    status = (
        "PASS"
        if not raw_hash_errors
        and reported_total == len(audit_companies)
        and len({row["source_id"] for row in audit_rows}) == len(audit_rows)
        and primary_map == audit_map
        and txt_lines == expected_lines
        else "FAIL"
    )
    return {
        "status": status,
        "raw_hash_errors": raw_hash_errors,
        "reported_total": reported_total,
        "raw_company_count": len(audit_companies),
        "reconstructed_company_count": len(audit_rows),
        "unique_reconstructed_id_count": len({row["source_id"] for row in audit_rows}),
        "source_id_maps_equal": primary_map == audit_map,
        "txt_lines_equal": txt_lines == expected_lines,
        "txt_line_count": len(txt_lines),
    }


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    RAW.mkdir(parents=True)
    NORMALIZED.mkdir(parents=True)

    snapshots = [fetch_snapshot(1), fetch_snapshot(2)]
    selected = snapshots[-1]
    rows = normalize_companies(selected["companies"])

    txt_path = NORMALIZED / "firstmark-companies.txt"
    json_path = NORMALIZED / "firstmark-companies.json"
    txt_path.write_text(
        "\n".join(f'{row["name"]} + {row["website"]}' for row in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )
    json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    snapshot_maps = [
        {
            source_id(company): (
                clean(first(company, "name", "title")),
                normalize_url(first(company, "domain", "website", "website_url")),
                first(company, "active_jobs_count", "jobs_count", "job_count"),
            )
            for company in snapshot["companies"]
        }
        for snapshot in snapshots
    ]
    comparison = {
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
        json.dumps(comparison, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )

    ids = [row["source_id"] for row in rows]
    names = [row["name"].casefold() for row in rows]
    domains = [urlparse(row["website"]).netloc.casefold().removeprefix("www.") for row in rows if row["website"]]
    duplicate_ids = {key: count for key, count in Counter(ids).items() if count > 1}
    duplicate_names = {key: count for key, count in Counter(names).items() if count > 1}
    duplicate_domains = {key: count for key, count in Counter(domains).items() if count > 1}
    missing_names = [row for row in rows if not row["name"]]
    missing_websites = [row for row in rows if not row["website"]]
    invalid_websites = [
        row
        for row in rows
        if row["website"]
        and (urlparse(row["website"]).scheme != "https" or not urlparse(row["website"]).netloc)
    ]

    audit = independent_audit(selected["dir"], rows, txt_path)
    (OUT / "independent-audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )

    errors: list[str] = []
    selected_summary = selected["summary"]
    if not selected_summary["stable"]:
        errors.append("selected snapshot is not internally stable")
    if selected_summary["reported_total"] != selected_summary["raw_count"]:
        errors.append("reported total does not equal raw count")
    if selected_summary["source_displayed_count"] not in (None, selected_summary["reported_total"]):
        errors.append("source displayed count does not equal API total")
    if duplicate_ids:
        errors.append("duplicate source IDs")
    if missing_names:
        errors.append("missing company names")
    if missing_websites:
        errors.append("missing company websites")
    if invalid_websites:
        errors.append("invalid company websites")
    if comparison["status"] != "PASS":
        errors.append("two source snapshots differ")
    if audit["status"] != "PASS":
        errors.append("independent audit failed")

    validation = {
        "source_url": SOURCE_URL,
        "api_url": API_URL,
        "collection_id": COLLECTION_ID,
        "status": "PASS" if not errors else "NEEDS_REVIEW",
        "source_displayed_count": selected_summary["source_displayed_count"],
        "api_reported_total": selected_summary["reported_total"],
        "raw_company_count": selected_summary["raw_count"],
        "page_sizes": selected_summary["page_sizes"],
        "terminal": selected_summary["terminal"],
        "output_line_count": len(rows),
        "unique_source_id_count": len(set(ids)),
        "unique_name_count": len(set(names)),
        "unique_domain_count": len(set(domains)),
        "missing_name_count": len(missing_names),
        "missing_website_count": len(missing_websites),
        "invalid_website_count": len(invalid_websites),
        "duplicate_id_groups": duplicate_ids,
        "duplicate_name_groups": duplicate_names,
        "duplicate_domain_groups": duplicate_domains,
        "snapshot_1": snapshots[0]["summary"],
        "snapshot_2": snapshots[1]["summary"],
        "snapshot_comparison": comparison,
        "independent_audit": audit,
        "errors": errors,
        "notes": [
            "Company names and websites are taken from the timestamped FirstMark/Getro company directory snapshot.",
            "Website normalization changes only scheme and host formatting; it does not infer a domain from the company name.",
        ],
    }
    (OUT / "validation.json").write_text(
        json.dumps(validation, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )

    manifest = {"generated_at_epoch": time.time(), "status": validation["status"], "files": {}}
    for path in sorted(OUT.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            manifest["files"][str(path.relative_to(OUT))] = {
                "bytes": path.stat().st_size,
                "sha256": digest(path.read_bytes()),
            }
    (OUT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )

    print("VALIDATION_SUMMARY_START")
    print(
        json.dumps(
            {
                "status": validation["status"],
                "source_displayed_count": validation["source_displayed_count"],
                "api_reported_total": validation["api_reported_total"],
                "raw_company_count": validation["raw_company_count"],
                "page_sizes": validation["page_sizes"],
                "output_line_count": validation["output_line_count"],
                "unique_source_id_count": validation["unique_source_id_count"],
                "unique_name_count": validation["unique_name_count"],
                "unique_domain_count": validation["unique_domain_count"],
                "missing_website_count": validation["missing_website_count"],
                "duplicate_name_groups": validation["duplicate_name_groups"],
                "duplicate_domain_groups": validation["duplicate_domain_groups"],
                "snapshot_comparison": comparison["status"],
                "independent_audit": audit["status"],
                "errors": errors,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    print("VALIDATION_SUMMARY_END")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        OUT.mkdir(parents=True, exist_ok=True)
        error = {"status": "FATAL", "error": repr(exc), "traceback": traceback.format_exc()}
        (OUT / "fatal-error.json").write_text(
            json.dumps(error, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )
        print(error["traceback"], file=sys.stderr)
        sys.exit(0)
