from __future__ import annotations

import concurrent.futures
import hashlib
import json
import math
import shutil
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

import requests

COLLECTION_ID = 220
BASE = f"https://api.getro.com/api/v2/collections/{COLLECTION_ID}/search"
JOBS_ENDPOINT = f"{BASE}/jobs"
COMPANIES_ENDPOINT = f"{BASE}/companies"
COUNTS_ENDPOINT = f"{BASE}/network_counts"
TARGET_JOBS = "https://jobs.cantos.vc/jobs"
TARGET_COMPANIES = "https://jobs.cantos.vc/companies"
OUT = Path("artifact-full")
RAW = OUT / "raw"
UA = "Mozilla/5.0 (compatible; evidence-first-public-page-audit/1.0)"
HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://jobs.cantos.vc",
    "Referer": "https://jobs.cantos.vc/",
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def save_bytes(path: Path, raw: bytes, meta: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    write_json(path.with_suffix(path.suffix + ".meta.json"), {**meta, "bytes": len(raw), "sha256": sha256(raw)})


def post_json(session: requests.Session, endpoint: str, payload: dict, *, attempts: int = 5) -> requests.Response:
    last = None
    for attempt in range(1, attempts + 1):
        try:
            response = session.post(endpoint, json=payload, headers=HEADERS, timeout=120)
            response.raise_for_status()
            return response
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < attempts:
                time.sleep(min(16, 2 ** attempt))
    raise RuntimeError(f"POST failed for {endpoint}: {last}")


def get_json(session: requests.Session, endpoint: str, *, attempts: int = 5) -> requests.Response:
    last = None
    for attempt in range(1, attempts + 1):
        try:
            response = session.get(endpoint, headers=HEADERS, timeout=120)
            response.raise_for_status()
            return response
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < attempts:
                time.sleep(min(16, 2 ** attempt))
    raise RuntimeError(f"GET failed for {endpoint}: {last}")


def fetch_collection(session: requests.Session, snapshot: int, kind: str, *, page_size: int = 100) -> dict:
    endpoint = JOBS_ENDPOINT if kind == "jobs" else COMPANIES_ENDPOINT
    key = "jobs" if kind == "jobs" else "companies"
    all_records: list[dict] = []
    page = 1
    reported_total = None
    page_summaries: list[dict] = []
    seen_page_hashes: set[str] = set()
    seen_ids: set[str] = set()

    while page <= 200:
        payload = {
            "hitsPerPage": page_size,
            "page": page,
            "filters": {"page": page},
            "query": "",
        }
        response = post_json(session, endpoint, payload)
        raw = response.content
        body_hash = sha256(raw)
        path = RAW / f"snapshot-{snapshot:02d}" / kind / f"page-{page:04d}.json"
        save_bytes(path, raw, {
            "endpoint": endpoint,
            "method": "POST",
            "request_payload": payload,
            "status": response.status_code,
            "content_type": response.headers.get("content-type"),
            "date": response.headers.get("date"),
            "etag": response.headers.get("etag"),
            "fetched_at_epoch": time.time(),
            "page": page,
        })

        data = response.json()
        results = data.get("results") or {}
        records = results.get(key) or []
        count = int(results.get("count") or 0)
        if reported_total is None:
            reported_total = count
        elif count != reported_total:
            raise RuntimeError(f"{kind} reported total changed from {reported_total} to {count} on page {page}")

        if body_hash in seen_page_hashes and records:
            raise RuntimeError(f"Repeated non-empty {kind} page body at page {page}")
        seen_page_hashes.add(body_hash)

        duplicate_ids_on_page = []
        for record in records:
            record_id = str(record.get("id"))
            if record_id in seen_ids:
                duplicate_ids_on_page.append(record_id)
            seen_ids.add(record_id)
        page_summaries.append({
            "page": page,
            "record_count": len(records),
            "reported_total": count,
            "sha256": body_hash,
            "duplicate_ids_on_page": duplicate_ids_on_page,
        })

        if not records:
            break
        all_records.extend(records)
        if len(all_records) >= reported_total:
            # Fetch one explicit terminal page to prove exhaustion.
            page += 1
            terminal_payload = {
                "hitsPerPage": page_size,
                "page": page,
                "filters": {"page": page},
                "query": "",
            }
            terminal_response = post_json(session, endpoint, terminal_payload)
            terminal_raw = terminal_response.content
            terminal_path = RAW / f"snapshot-{snapshot:02d}" / kind / f"page-{page:04d}.json"
            save_bytes(terminal_path, terminal_raw, {
                "endpoint": endpoint,
                "method": "POST",
                "request_payload": terminal_payload,
                "status": terminal_response.status_code,
                "content_type": terminal_response.headers.get("content-type"),
                "date": terminal_response.headers.get("date"),
                "etag": terminal_response.headers.get("etag"),
                "fetched_at_epoch": time.time(),
                "page": page,
                "terminal_probe": True,
            })
            terminal_results = (terminal_response.json().get("results") or {})
            terminal_records = terminal_results.get(key) or []
            terminal_count = int(terminal_results.get("count") or 0)
            page_summaries.append({
                "page": page,
                "record_count": len(terminal_records),
                "reported_total": terminal_count,
                "sha256": sha256(terminal_raw),
                "terminal_probe": True,
            })
            if terminal_count != reported_total:
                raise RuntimeError(f"{kind} total changed during terminal probe")
            if terminal_records:
                # A server may ignore requested page size. Continue if more records truly exist.
                all_records.extend(terminal_records)
                page += 1
                continue
            break
        page += 1

    if reported_total is None:
        raise RuntimeError(f"No {kind} total was reported")
    if len(all_records) != reported_total:
        raise RuntimeError(f"{kind} raw record count {len(all_records)} != reported total {reported_total}")

    ids = [str(record.get("id")) for record in all_records]
    if len(set(ids)) != len(ids):
        groups = {key: value for key, value in Counter(ids).items() if value > 1}
        raise RuntimeError(f"Duplicate {kind} IDs: {groups}")

    # Recheck page 1 at the end of each collection fetch.
    first_payload = {"hitsPerPage": page_size, "page": 1, "filters": {"page": 1}, "query": ""}
    recheck = post_json(session, endpoint, first_payload)
    recheck_raw = recheck.content
    recheck_path = RAW / f"snapshot-{snapshot:02d}" / kind / "page-0001-recheck.json"
    save_bytes(recheck_path, recheck_raw, {
        "endpoint": endpoint,
        "method": "POST",
        "request_payload": first_payload,
        "status": recheck.status_code,
        "content_type": recheck.headers.get("content-type"),
        "date": recheck.headers.get("date"),
        "etag": recheck.headers.get("etag"),
        "fetched_at_epoch": time.time(),
        "page": 1,
        "recheck": True,
    })
    initial_first_hash = page_summaries[0]["sha256"]
    if sha256(recheck_raw) != initial_first_hash:
        raise RuntimeError(f"{kind} page 1 changed during snapshot {snapshot}")

    return {
        "reported_total": reported_total,
        "records": all_records,
        "page_summaries": page_summaries,
        "page_1_recheck_sha256": sha256(recheck_raw),
    }


def normalize_website(domain: object) -> str:
    text = str(domain or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "https://" + text
    parsed = urlparse(text)
    host = parsed.netloc.casefold().removeprefix("www.")
    return f"https://{host}" if host and "." in host else ""


def website_check(row: dict) -> dict:
    url = row["website"]
    if not url:
        return {"id": row["id"], "name": row["name"], "source_url": "", "status": None, "final_url": "", "final_root": "", "history": [], "error": "source domain missing"}
    try:
        response = requests.get(url, headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}, timeout=30, allow_redirects=True, stream=True)
        parsed = urlparse(response.url)
        host = parsed.netloc.casefold().removeprefix("www.")
        result = {
            "id": row["id"],
            "name": row["name"],
            "source_url": url,
            "status": response.status_code,
            "final_url": response.url,
            "final_root": f"https://{host}" if host else "",
            "history": [{"status": prior.status_code, "url": prior.url, "location": prior.headers.get("location")} for prior in response.history],
            "content_type": response.headers.get("content-type"),
            "error": "",
        }
        response.close()
        return result
    except Exception as exc:  # noqa: BLE001
        return {"id": row["id"], "name": row["name"], "source_url": url, "status": None, "final_url": "", "final_root": "", "history": [], "error": repr(exc)}


def make_snapshot(snapshot: int) -> dict:
    session = requests.Session()
    counts_response = get_json(session, COUNTS_ENDPOINT)
    counts_raw = counts_response.content
    save_bytes(RAW / f"snapshot-{snapshot:02d}" / "network-counts.json", counts_raw, {
        "endpoint": COUNTS_ENDPOINT,
        "method": "GET",
        "status": counts_response.status_code,
        "content_type": counts_response.headers.get("content-type"),
        "date": counts_response.headers.get("date"),
        "etag": counts_response.headers.get("etag"),
        "fetched_at_epoch": time.time(),
    })
    network_counts = (counts_response.json().get("results") or {})

    jobs = fetch_collection(session, snapshot, "jobs", page_size=100)
    companies = fetch_collection(session, snapshot, "companies", page_size=100)

    job_org_counts = Counter(str(job.get("organization", {}).get("id")) for job in jobs["records"])
    job_org_counts.pop("None", None)
    directory_by_id = {str(company.get("id")): company for company in companies["records"]}
    unresolved_org_ids = sorted(set(job_org_counts) - set(directory_by_id))
    if unresolved_org_ids:
        raise RuntimeError(f"Hiring organizations absent from directory: {unresolved_org_ids}")

    hiring_rows = []
    for company_id in sorted(job_org_counts, key=lambda cid: (str(directory_by_id[cid].get("name") or "").casefold(), int(cid))):
        company = directory_by_id[company_id]
        hiring_rows.append({
            "id": int(company_id),
            "name": str(company.get("name") or "").strip(),
            "slug": str(company.get("slug") or "").strip(),
            "domain": str(company.get("domain") or "").strip(),
            "website": normalize_website(company.get("domain")),
            "actual_job_count": int(job_org_counts[company_id]),
            "directory_active_jobs_count": int(company.get("active_jobs_count") or 0),
            "stage": company.get("stage"),
            "description": company.get("description"),
        })

    return {
        "network_counts": network_counts,
        "jobs": jobs,
        "companies": companies,
        "hiring_rows": hiring_rows,
        "job_org_counts": dict(job_org_counts),
        "unresolved_org_ids": unresolved_org_ids,
    }


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    RAW.mkdir(parents=True)

    snapshot_1 = make_snapshot(1)
    time.sleep(3)
    snapshot_2 = make_snapshot(2)

    def job_map(snapshot: dict) -> list[tuple]:
        return sorted((int(job["id"]), int(job.get("organization", {}).get("id")), str(job.get("title") or ""), str(job.get("url") or "")) for job in snapshot["jobs"]["records"])

    def company_map(snapshot: dict) -> list[tuple]:
        return sorted((int(row["id"]), row["name"], row["slug"], row["domain"], row["actual_job_count"]) for row in snapshot["hiring_rows"])

    job_map_1 = job_map(snapshot_1)
    job_map_2 = job_map(snapshot_2)
    company_map_1 = company_map(snapshot_1)
    company_map_2 = company_map(snapshot_2)
    if job_map_1 != job_map_2:
        raise RuntimeError("Two complete job snapshots differ")
    if company_map_1 != company_map_2:
        raise RuntimeError("Two complete hiring-company snapshots differ")

    hiring_rows = snapshot_2["hiring_rows"]
    names = [row["name"].casefold() for row in hiring_rows]
    domains = [urlparse(row["website"]).netloc.casefold().removeprefix("www.") for row in hiring_rows if row["website"]]

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        website_checks = list(executor.map(website_check, hiring_rows))
    website_checks.sort(key=lambda row: (row["name"].casefold(), row["id"]))

    write_json(OUT / "hiring-companies-source.json", hiring_rows)
    write_json(OUT / "website-checks.json", website_checks)
    write_json(OUT / "snapshot-comparison.json", {
        "status": "PASS",
        "job_snapshot_1_count": len(job_map_1),
        "job_snapshot_2_count": len(job_map_2),
        "job_maps_equal": True,
        "hiring_company_snapshot_1_count": len(company_map_1),
        "hiring_company_snapshot_2_count": len(company_map_2),
        "hiring_company_maps_equal": True,
    })

    directory_hint_sum = sum(row["directory_active_jobs_count"] for row in hiring_rows)
    actual_job_sum = sum(row["actual_job_count"] for row in hiring_rows)
    hint_differences = [
        {
            "id": row["id"],
            "name": row["name"],
            "actual_job_count": row["actual_job_count"],
            "directory_active_jobs_count": row["directory_active_jobs_count"],
            "difference": row["directory_active_jobs_count"] - row["actual_job_count"],
        }
        for row in hiring_rows
        if row["actual_job_count"] != row["directory_active_jobs_count"]
    ]

    summary = {
        "status": "PASS_SOURCE_EXTRACTION",
        "collection_id": COLLECTION_ID,
        "target_jobs": TARGET_JOBS,
        "target_companies": TARGET_COMPANIES,
        "network_counts_snapshot_2": snapshot_2["network_counts"],
        "jobs_reported_total": snapshot_2["jobs"]["reported_total"],
        "jobs_raw_count": len(snapshot_2["jobs"]["records"]),
        "unique_job_id_count": len({str(job.get("id")) for job in snapshot_2["jobs"]["records"]}),
        "directory_reported_total": snapshot_2["companies"]["reported_total"],
        "directory_raw_count": len(snapshot_2["companies"]["records"]),
        "unique_directory_company_id_count": len({str(company.get("id")) for company in snapshot_2["companies"]["records"]}),
        "hiring_company_count": len(hiring_rows),
        "unique_hiring_company_id_count": len({row["id"] for row in hiring_rows}),
        "unique_hiring_company_name_count": len(set(names)),
        "source_website_count": sum(bool(row["website"]) for row in hiring_rows),
        "unique_source_domain_count": len(set(domains)),
        "missing_source_website_count": sum(not row["website"] for row in hiring_rows),
        "duplicate_name_groups": {key: value for key, value in Counter(names).items() if value > 1},
        "duplicate_domain_groups": {key: value for key, value in Counter(domains).items() if value > 1},
        "actual_job_sum": actual_job_sum,
        "directory_active_jobs_hint_sum": directory_hint_sum,
        "job_hint_differences": hint_differences,
        "two_snapshot_job_maps_equal": True,
        "two_snapshot_hiring_company_maps_equal": True,
        "jobs_page_summaries": snapshot_2["jobs"]["page_summaries"],
        "companies_page_summaries": snapshot_2["companies"]["page_summaries"],
    }
    write_json(OUT / "source-validation-summary.json", summary)
    print("SOURCE_VALIDATION_SUMMARY_START")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("SOURCE_VALIDATION_SUMMARY_END")


if __name__ == "__main__":
    main()
