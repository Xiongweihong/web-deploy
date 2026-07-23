from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
import traceback
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

SOURCE_URL = "https://jobs.dragonfly.xyz/companies"
ORIGIN = "https://jobs.dragonfly.xyz"
COLLECTION_ID = "1118"
API_URL = f"https://api.getro.com/api/v2/collections/{COLLECTION_ID}/search/companies"
OUT = Path("artifact")
RAW = OUT / "raw"
NORMALIZED = OUT / "normalized"
PAGE_SIZE = 100
USER_AGENT = "Mozilla/5.0 (compatible; dragonfly-evidence-crawler/1.0)"


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
    if not host or "." not in host:
        return ""
    return f"https://{host}"


def request(method: str, url: str, *, payload: dict | None = None, attempts: int = 6) -> tuple[bytes, dict]:
    body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {
        "accept": "application/json" if payload is not None else "text/html,application/xhtml+xml",
        "user-agent": USER_AGENT,
    }
    if payload is not None:
        headers.update(
            {
                "content-type": "application/json",
                "origin": ORIGIN,
                "referer": SOURCE_URL,
            }
        )
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=120) as response:
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
                raise RuntimeError(f"HTTP {exc.code} for {url}: {detail[:1200]}") from exc
            if attempt < attempts:
                time.sleep(min(30, 2**attempt))
    raise RuntimeError(f"request failed after {attempts} attempts: {url}: {last_error}")


def save_raw(path: Path, raw: bytes, meta: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    path.with_suffix(path.suffix + ".meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )


def parse_display_count(html: str) -> int | None:
    match = re.search(r"Showing\s+([0-9,]+)\s+companies", html, re.I)
    return int(match.group(1).replace(",", "")) if match else None


def normalize_company(company: dict) -> dict:
    raw_site = first(company, "domain", "website", "website_url", "url")
    website = normalize_url(raw_site)
    return {
        "source_id": source_id(company),
        "slug": clean(first(company, "slug", "company_slug")),
        "name": clean(first(company, "name", "title")),
        "website": website,
        "source_domain": clean(first(company, "domain")),
        "source_website": raw_site,
        "active_jobs_count": int(first(company, "active_jobs_count", "activeJobsCount") or 0),
        "website_provenance": "getro_domain_or_website",
    }


def capture_snapshot(number: int) -> dict:
    snapshot_dir = RAW / f"snapshot-{number:02d}"
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    html_raw, html_meta = request("GET", SOURCE_URL)
    save_raw(snapshot_dir / "source-page.html", html_raw, html_meta)
    displayed_count = parse_display_count(html_raw.decode("utf-8", "replace"))

    all_companies: list[dict] = []
    page_sizes: list[int] = []
    reported_totals: list[int] = []
    page_hashes: list[str] = []
    terminal_empty_page = False
    expected_pages: int | None = None

    for page in range(20):
        payload = {"hitsPerPage": PAGE_SIZE, "page": page, "query": "", "filters": ""}
        raw, meta = request("POST", API_URL, payload=payload)
        save_raw(snapshot_dir / "companies" / f"page-{page:03d}.json", raw, meta)
        page_hashes.append(meta["sha256"])
        data = json.loads(raw)
        results = data.get("results") or {}
        companies = results.get("companies") or []
        total = int(results.get("count") or 0)
        reported_totals.append(total)
        page_sizes.append(len(companies))
        if expected_pages is None and total:
            expected_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
        if not companies:
            terminal_empty_page = True
            break
        all_companies.extend(companies)
        if total and len(all_companies) >= total:
            # Fetch one additional page to prove terminal exhaustion.
            continue
    else:
        raise RuntimeError("maximum page guard reached")

    by_id: dict[str, dict] = {}
    duplicate_conflicting_ids: list[str] = []
    duplicate_identical_ids: list[str] = []
    for company in all_companies:
        key = source_id(company)
        if key in by_id:
            if by_id[key] == company:
                duplicate_identical_ids.append(key)
            else:
                duplicate_conflicting_ids.append(key)
        else:
            by_id[key] = company

    rows = [normalize_company(company) for company in by_id.values()]
    rows.sort(key=lambda row: (row["name"].casefold(), row["source_id"]))
    total = max(reported_totals) if reported_totals else 0
    summary = {
        "snapshot": number,
        "captured_at_epoch": time.time(),
        "displayed_company_count": displayed_count,
        "reported_totals": sorted(set(reported_totals)),
        "reported_total": total,
        "raw_record_count": len(all_companies),
        "unique_source_id_count": len(by_id),
        "page_sizes": page_sizes,
        "page_hashes": page_hashes,
        "expected_nonempty_pages": expected_pages,
        "terminal_empty_page": terminal_empty_page,
        "duplicate_identical_ids": sorted(set(duplicate_identical_ids)),
        "duplicate_conflicting_ids": sorted(set(duplicate_conflicting_ids)),
        "source_page_sha256": html_meta["sha256"],
    }
    (snapshot_dir / "snapshot-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    return {"rows": rows, "summary": summary, "dir": snapshot_dir}


def audit_normalize(company: dict) -> tuple[str, str, str, int]:
    key = clean(company.get("id") or company.get("objectID") or company.get("object_id") or company.get("slug"))
    if not key:
        key = "name:" + clean(company.get("name") or company.get("title")).casefold()
    name = clean(company.get("name") or company.get("title"))
    raw_site = company.get("domain") or company.get("website") or company.get("website_url") or company.get("url")
    if isinstance(raw_site, dict):
        raw_site = raw_site.get("url") or raw_site.get("href") or raw_site.get("value")
    site = clean(raw_site)
    if site and "://" not in site:
        site = "https://" + site
    if site.startswith("http://"):
        site = "https://" + site[7:]
    parsed = urlparse(site) if site else None
    website = f"https://{parsed.netloc.casefold().removeprefix('www.')}" if parsed and parsed.netloc else ""
    jobs = int(company.get("active_jobs_count") or company.get("activeJobsCount") or 0)
    return key, name, website, jobs


def independent_audit(snapshot_dir: Path, rows: list[dict], txt_path: Path) -> dict:
    hash_errors: list[str] = []
    all_companies: list[dict] = []
    reported_totals: list[int] = []
    page_sizes: list[int] = []
    for path in sorted((snapshot_dir / "companies").glob("page-*.json")):
        meta_path = path.with_suffix(path.suffix + ".meta.json")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if sha256(path.read_bytes()) != meta.get("sha256"):
            hash_errors.append(str(path.relative_to(snapshot_dir)))
        data = json.loads(path.read_bytes())
        results = data.get("results") or {}
        companies = results.get("companies") or []
        page_sizes.append(len(companies))
        reported_totals.append(int(results.get("count") or 0))
        all_companies.extend(companies)

    reconstructed: dict[str, tuple[str, str, int]] = {}
    duplicate_ids: list[str] = []
    for company in all_companies:
        key, name, website, jobs = audit_normalize(company)
        if key in reconstructed:
            duplicate_ids.append(key)
        reconstructed[key] = (name, website, jobs)
    primary = {
        row["source_id"]: (row["name"], row["website"], row["active_jobs_count"])
        for row in rows
    }
    expected_lines = [f'{row["name"]} + {row["website"]}' for row in rows]
    actual_lines = [line for line in txt_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    total = max(reported_totals) if reported_totals else 0
    status = "PASS" if (
        not hash_errors
        and total == len(reconstructed)
        and primary == reconstructed
        and actual_lines == expected_lines
        and page_sizes
        and page_sizes[-1] == 0
        and not duplicate_ids
    ) else "FAIL"
    return {
        "status": status,
        "raw_hash_errors": hash_errors,
        "reported_total": total,
        "raw_record_count_including_duplicates": len(all_companies),
        "reconstructed_unique_count": len(reconstructed),
        "page_sizes": page_sizes,
        "terminal_empty_page": bool(page_sizes) and page_sizes[-1] == 0,
        "duplicate_ids": sorted(set(duplicate_ids)),
        "source_id_maps_equal": primary == reconstructed,
        "txt_lines_equal": actual_lines == expected_lines,
        "txt_line_count": len(actual_lines),
    }


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    RAW.mkdir(parents=True)
    NORMALIZED.mkdir(parents=True)

    snapshots = [capture_snapshot(1), capture_snapshot(2)]
    selected = snapshots[-1]
    rows = selected["rows"]

    txt_path = NORMALIZED / "dragonfly-companies.txt"
    json_path = NORMALIZED / "dragonfly-companies.json"
    txt_path.write_text(
        "\n".join(f'{row["name"]} + {row["website"]}' for row in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )
    json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    maps = [
        {row["source_id"]: (row["name"], row["website"], row["active_jobs_count"]) for row in item["rows"]}
        for item in snapshots
    ]
    snapshot_comparison = {
        "status": "PASS" if maps[0] == maps[1] else "FAIL",
        "snapshot_1_count": len(maps[0]),
        "snapshot_2_count": len(maps[1]),
        "added_ids": sorted(set(maps[1]) - set(maps[0])),
        "removed_ids": sorted(set(maps[0]) - set(maps[1])),
        "changed_ids": sorted(key for key in set(maps[0]) & set(maps[1]) if maps[0][key] != maps[1][key]),
    }
    (OUT / "snapshot-comparison.json").write_text(
        json.dumps(snapshot_comparison, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )

    names = [row["name"].casefold() for row in rows]
    ids = [row["source_id"] for row in rows]
    domains = [urlparse(row["website"]).netloc.casefold().removeprefix("www.") for row in rows if row["website"]]
    duplicate_names = {name: count for name, count in Counter(names).items() if count > 1}
    duplicate_ids = {key: count for key, count in Counter(ids).items() if count > 1}
    duplicate_domains = {domain: count for domain, count in Counter(domains).items() if count > 1}
    missing_names = [row for row in rows if not row["name"]]
    missing_websites = [row for row in rows if not row["website"]]
    invalid_websites = [
        row for row in rows
        if row["website"] and (urlparse(row["website"]).scheme != "https" or not urlparse(row["website"]).netloc)
    ]
    board_host_errors = [row for row in rows if urlparse(row["website"]).netloc.endswith("dragonfly.xyz")]

    audit = independent_audit(selected["dir"], rows, txt_path)
    (OUT / "independent-audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )

    summary = selected["summary"]
    errors: list[str] = []
    if summary["displayed_company_count"] != summary["reported_total"]:
        errors.append("displayed count does not equal API total")
    if summary["reported_total"] != summary["unique_source_id_count"]:
        errors.append("API total does not equal unique source ID count")
    if not summary["terminal_empty_page"]:
        errors.append("terminal empty page was not observed")
    if summary["duplicate_conflicting_ids"] or summary["duplicate_identical_ids"]:
        errors.append("duplicate company IDs found")
    if missing_names:
        errors.append("missing company names")
    if missing_websites:
        errors.append("missing company websites")
    if invalid_websites:
        errors.append("invalid company websites")
    if board_host_errors:
        errors.append("job board host used as company website")
    if duplicate_ids:
        errors.append("duplicate final IDs")
    if duplicate_names:
        errors.append("duplicate final names")
    if duplicate_domains:
        errors.append("duplicate final domains")
    if snapshot_comparison["status"] != "PASS":
        errors.append("two full snapshots differ")
    if audit["status"] != "PASS":
        errors.append("independent audit failed")

    validation = {
        "source_url": SOURCE_URL,
        "provider": "Getro",
        "collection_id": COLLECTION_ID,
        "status": "PASS" if not errors else "NEEDS_REVIEW",
        "captured_at_epoch": summary["captured_at_epoch"],
        "displayed_company_count": summary["displayed_company_count"],
        "api_reported_total": summary["reported_total"],
        "raw_record_count": summary["raw_record_count"],
        "unique_source_id_count": summary["unique_source_id_count"],
        "page_sizes": summary["page_sizes"],
        "terminal_empty_page": summary["terminal_empty_page"],
        "output_line_count": len(rows),
        "unique_name_count": len(set(names)),
        "unique_domain_count": len(set(domains)),
        "missing_name_count": len(missing_names),
        "missing_website_count": len(missing_websites),
        "invalid_website_count": len(invalid_websites),
        "board_host_error_count": len(board_host_errors),
        "duplicate_id_groups": duplicate_ids,
        "duplicate_name_groups": duplicate_names,
        "duplicate_domain_groups": duplicate_domains,
        "snapshot_comparison": snapshot_comparison,
        "independent_audit": audit,
        "errors": errors,
        "notes": [
            "Company names and websites are normalized from the timestamped Getro company directory records.",
            "Website validation here covers presence, syntax, uniqueness, and source mapping; it is not a legal ownership audit.",
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
                "sha256": sha256(path.read_bytes()),
            }
    (OUT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )

    print("VALIDATION_SUMMARY_START")
    print(json.dumps({
        "status": validation["status"],
        "displayed_company_count": validation["displayed_company_count"],
        "api_reported_total": validation["api_reported_total"],
        "raw_record_count": validation["raw_record_count"],
        "unique_source_id_count": validation["unique_source_id_count"],
        "page_sizes": validation["page_sizes"],
        "output_line_count": validation["output_line_count"],
        "missing_website_count": validation["missing_website_count"],
        "duplicate_name_groups": validation["duplicate_name_groups"],
        "duplicate_domain_groups": validation["duplicate_domain_groups"],
        "snapshot_comparison": snapshot_comparison["status"],
        "independent_audit": audit["status"],
        "errors": errors,
    }, ensure_ascii=False, indent=2, sort_keys=True))
    print("VALIDATION_SUMMARY_END")
    print("COMPANY_LIST_START")
    print(txt_path.read_text(encoding="utf-8"), end="")
    print("COMPANY_LIST_END")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        OUT.mkdir(parents=True, exist_ok=True)
        error = {"status": "FATAL", "error": repr(exc), "traceback": traceback.format_exc()}
        (OUT / "fatal-error.json").write_text(
            json.dumps(error, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )
        print(error["traceback"])
        raise
