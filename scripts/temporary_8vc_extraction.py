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

COLLECTION_ID = "1005"
ORIGIN = "https://jobs.8vc.com"
JOBS_PAGE = ORIGIN + "/jobs"
COMPANIES_PAGE = ORIGIN + "/companies"
API_BASE = f"https://api.getro.com/api/v2/collections/{COLLECTION_ID}"
JOBS_ENDPOINT = API_BASE + "/search/jobs"
COMPANIES_ENDPOINT = API_BASE + "/search/companies"
OUT = Path("artifact")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalized_name(value: object) -> str:
    return clean(value).casefold()


def first(record: dict, *keys: str):
    for key in keys:
        value = record.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


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
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return text.rstrip("/")


def canonical_company_id(company: dict) -> str:
    value = first(company, "id", "objectID", "object_id", "slug")
    if value not in (None, ""):
        return str(value).strip()
    return "name:" + normalized_name(first(company, "name", "title"))


def job_id(job: dict) -> str:
    return clean(first(job, "id", "objectID", "object_id", "slug", "url"))


def job_organization(job: dict) -> dict:
    value = job.get("organization") or job.get("company") or {}
    return value if isinstance(value, dict) else {"name": value}


def organization_identity(org: dict) -> str:
    value = first(org, "id", "objectID", "object_id", "slug")
    if value not in (None, ""):
        return str(value).strip()
    return "name:" + normalized_name(first(org, "name", "title"))


def aliases(record: dict) -> list[str]:
    values: list[str] = []
    for key in ("id", "objectID", "object_id", "slug"):
        value = record.get(key)
        if value not in (None, ""):
            values.append(f"raw:{str(value).strip()}")
    name = normalized_name(first(record, "name", "title"))
    if name:
        values.append("name:" + name)
    return list(dict.fromkeys(values))


def post_json(url: str, payload: dict, referer: str, attempts: int = 5) -> tuple[dict, bytes, dict]:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "accept": "application/json",
            "content-type": "application/json",
            "origin": ORIGIN,
            "referer": referer,
            "user-agent": "Mozilla/5.0 (compatible; 8vc-evidence-crawler/2.0)",
        },
    )
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                raw = response.read()
                return (
                    json.loads(raw),
                    raw,
                    {
                        "status": response.status,
                        "content_type": response.headers.get("content-type"),
                        "date": response.headers.get("date"),
                        "etag": response.headers.get("etag"),
                        "sha256": digest(raw),
                        "request_payload": payload,
                        "fetched_at_epoch": time.time(),
                    },
                )
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if isinstance(exc, urllib.error.HTTPError) and exc.code < 500 and exc.code not in (408, 429):
                detail = exc.read().decode("utf-8", "replace")
                raise RuntimeError(f"HTTP {exc.code}: {detail[:2000]}") from exc
            if attempt < attempts:
                time.sleep(min(20, 2**attempt))
    raise RuntimeError(f"request failed after {attempts} attempts: {last_error}")


def payload_for(kind: str, page: int) -> dict:
    if kind == "jobs":
        return {"hits_per_page": 100, "page": page}
    return {"hitsPerPage": 100, "page": page, "query": "", "filters": ""}


def page_ids(kind: str, records: list[dict]) -> list[str]:
    if kind == "jobs":
        return [job_id(record) for record in records]
    return [canonical_company_id(record) for record in records]


def fetch_attempt(kind: str, attempt: int) -> dict:
    endpoint = JOBS_ENDPOINT if kind == "jobs" else COMPANIES_ENDPOINT
    referer = JOBS_PAGE if kind == "jobs" else COMPANIES_PAGE
    record_key = "jobs" if kind == "jobs" else "companies"
    raw_dir = OUT / "raw" / kind / f"attempt-{attempt:02d}"
    raw_dir.mkdir(parents=True, exist_ok=True)

    all_records: list[dict] = []
    page_summaries: list[dict] = []
    reported_total: int | None = None
    first_page_ids: list[str] = []
    seen_hashes: set[str] = set()
    terminal: dict | None = None

    for page in range(1000):
        data, raw, meta = post_json(endpoint, payload_for(kind, page), referer)
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, dict):
            raise RuntimeError(f"{kind} page {page}: missing results object")
        records = results.get(record_key) or []
        if not isinstance(records, list):
            raise RuntimeError(f"{kind} page {page}: {record_key} is not a list")
        current_total = results.get("count")
        if reported_total is None:
            reported_total = int(current_total) if current_total is not None else None
        elif current_total is not None and int(current_total) != reported_total:
            raise RuntimeError(f"{kind}: total changed inside attempt {attempt}: {reported_total} -> {current_total}")

        raw_path = raw_dir / f"page-{page:06d}.json"
        meta_path = raw_dir / f"page-{page:06d}.meta.json"
        raw_path.write_bytes(raw)
        meta.update(
            {
                "kind": kind,
                "attempt": attempt,
                "page": page,
                "record_count": len(records),
                "reported_total": reported_total,
                "raw_file": raw_path.name,
            }
        )
        meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")

        if records and meta["sha256"] in seen_hashes:
            raise RuntimeError(f"{kind}: repeated non-empty page body at page {page}")
        seen_hashes.add(meta["sha256"])
        if page == 0:
            first_page_ids = page_ids(kind, records)
        all_records.extend(records)
        page_summaries.append({"page": page, "records": len(records), "sha256": meta["sha256"]})
        print(f"{kind} attempt={attempt} page={page} records={len(records)} accumulated={len(all_records)} total={reported_total}")

        if not records:
            terminal = {"type": "empty_page", "page": page}
            break
        if reported_total is not None and len(all_records) >= reported_total:
            terminal = {"type": "reported_total_reached", "page": page, "count": len(all_records)}
            break
    else:
        raise RuntimeError(f"{kind}: max page guard reached")

    check, _, _ = post_json(endpoint, payload_for(kind, 0), referer)
    check_results = check.get("results") or {}
    check_records = check_results.get(record_key) or []
    total_after = int(check_results.get("count") or 0)
    ids = page_ids(kind, all_records)
    duplicate_ids = sorted(key for key, count in Counter(ids).items() if key and count > 1)
    empty_ids = [index for index, key in enumerate(ids) if not key]
    first_page_ids_after = page_ids(kind, check_records)
    stable = (
        reported_total is not None
        and reported_total == total_after
        and len(all_records) == reported_total
        and len(set(ids)) == reported_total
        and not empty_ids
        and first_page_ids == first_page_ids_after
    )
    summary = {
        "kind": kind,
        "attempt": attempt,
        "reported_total": reported_total,
        "reported_total_after": total_after,
        "raw_count": len(all_records),
        "unique_id_count": len(set(ids)),
        "duplicate_ids": duplicate_ids,
        "empty_id_indexes": empty_ids,
        "first_page_stable": first_page_ids == first_page_ids_after,
        "page_count": len(page_summaries),
        "page_sizes": [page["records"] for page in page_summaries],
        "terminal": terminal,
        "stable": stable,
    }
    (raw_dir / "attempt-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    return {"records": all_records, "summary": summary, "raw_dir": raw_dir}


def stable_capture(kind: str, max_attempts: int = 3) -> dict:
    attempts: list[dict] = []
    for attempt in range(1, max_attempts + 1):
        result = fetch_attempt(kind, attempt)
        attempts.append(result)
        if result["summary"]["stable"]:
            result["all_attempt_summaries"] = [item["summary"] for item in attempts]
            return result
        time.sleep(2)
    result = attempts[-1]
    result["all_attempt_summaries"] = [item["summary"] for item in attempts]
    return result


def build_company_index(companies: list[dict]):
    company_by_id: dict[str, dict] = {}
    alias_index: dict[str, set[str]] = defaultdict(set)
    for company in companies:
        company_id_value = canonical_company_id(company)
        if not company_id_value:
            raise RuntimeError("company without canonical identity")
        if company_id_value in company_by_id:
            raise RuntimeError(f"duplicate company canonical identity: {company_id_value}")
        company_by_id[company_id_value] = company
        for alias in aliases(company):
            alias_index[alias].add(company_id_value)
    return company_by_id, alias_index


def resolve_hiring_companies(jobs: list[dict], companies: list[dict]) -> dict:
    company_by_id, alias_index = build_company_index(companies)
    org_groups: dict[str, dict] = {}
    unusable_jobs: list[dict] = []
    for job in jobs:
        org = job_organization(job)
        org_name = clean(first(org, "name", "title"))
        identity = organization_identity(org)
        if not org_name or identity == "name:":
            unusable_jobs.append({"job_id": job_id(job), "organization": org})
            continue
        group = org_groups.setdefault(identity, {"organization": org, "job_ids": []})
        group["job_ids"].append(job_id(job))

    resolved_by_company: dict[str, dict] = {}
    unresolved: list[dict] = []
    for identity, group in org_groups.items():
        org = group["organization"]
        candidate_ids: set[str] = set()
        matched_aliases: dict[str, list[str]] = {}
        for alias in aliases(org):
            matches = alias_index.get(alias, set())
            if matches:
                matched_aliases[alias] = sorted(matches)
                candidate_ids.update(matches)
        if len(candidate_ids) != 1:
            unresolved.append(
                {
                    "organization_identity": identity,
                    "name": clean(first(org, "name", "title")),
                    "job_count": len(group["job_ids"]),
                    "job_ids": group["job_ids"],
                    "matched_aliases": matched_aliases,
                    "candidate_company_ids": sorted(candidate_ids),
                    "raw_organization": org,
                }
            )
            continue

        company_id_value = next(iter(candidate_ids))
        company = company_by_id[company_id_value]
        domain_value = first(company, "domain", "website", "website_url")
        website = normalize_url(domain_value)
        row = resolved_by_company.get(company_id_value)
        if row is None:
            row = {
                "source_id": company_id_value,
                "source_slug": clean(first(company, "slug")),
                "name": clean(first(company, "name", "title")),
                "website": website,
                "raw_domain": clean(domain_value),
                "website_provenance": "getro_company_domain" if website else "missing",
                "active_job_count_from_jobs": 0,
                "active_job_count_from_directory": first(
                    company, "active_jobs_count", "jobs_count", "job_count"
                ),
                "organization_identities": [],
            }
            resolved_by_company[company_id_value] = row
        row["active_job_count_from_jobs"] += len(group["job_ids"])
        row["organization_identities"].append(identity)

    rows = sorted(resolved_by_company.values(), key=lambda row: (row["name"].casefold(), row["source_id"]))
    return {
        "rows": rows,
        "unresolved": unresolved,
        "unusable_jobs": unusable_jobs,
        "organization_group_count": len(org_groups),
        "company_by_id": company_by_id,
    }


def independent_audit(
    selected_jobs_dir: Path,
    selected_companies_dir: Path,
    normalized_rows: list[dict],
    txt_path: Path,
) -> dict:
    raw_hash_errors: list[str] = []
    audit_jobs: list[dict] = []
    audit_companies: list[dict] = []
    for kind, raw_dir, record_key, destination in (
        ("jobs", selected_jobs_dir, "jobs", audit_jobs),
        ("companies", selected_companies_dir, "companies", audit_companies),
    ):
        for raw_path in sorted(raw_dir.glob("page-*.json")):
            meta_path = raw_path.with_name(raw_path.stem + ".meta.json")
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            actual_hash = digest(raw_path.read_bytes())
            if actual_hash != meta.get("sha256"):
                raw_hash_errors.append(f"{kind}/{raw_path.name}")
            payload = json.loads(raw_path.read_bytes())
            destination.extend(((payload.get("results") or {}).get(record_key) or []))

    reconstructed = resolve_hiring_companies(audit_jobs, audit_companies)
    audit_rows = reconstructed["rows"]
    primary_map = {
        row["source_id"]: (row["name"], row["website"], row["active_job_count_from_jobs"])
        for row in normalized_rows
    }
    audit_map = {
        row["source_id"]: (row["name"], row["website"], row["active_job_count_from_jobs"])
        for row in audit_rows
    }
    txt_lines = [line for line in txt_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    expected_lines = [f'{row["name"]} + {row["website"]}' for row in normalized_rows]
    return {
        "raw_hash_errors": raw_hash_errors,
        "raw_job_count": len(audit_jobs),
        "raw_company_count": len(audit_companies),
        "reconstructed_company_count": len(audit_rows),
        "reconstructed_unresolved_count": len(reconstructed["unresolved"]),
        "source_id_maps_equal": primary_map == audit_map,
        "txt_lines_equal": txt_lines == expected_lines,
        "txt_line_count": len(txt_lines),
        "status": "PASS"
        if not raw_hash_errors
        and not reconstructed["unresolved"]
        and primary_map == audit_map
        and txt_lines == expected_lines
        else "FAIL",
    }


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    jobs_capture = stable_capture("jobs")
    companies_capture = stable_capture("companies")
    jobs = jobs_capture["records"]
    companies = companies_capture["records"]

    resolution = resolve_hiring_companies(jobs, companies)
    rows = resolution["rows"]
    unresolved = resolution["unresolved"]
    unusable_jobs = resolution["unusable_jobs"]

    missing_websites = [row for row in rows if not row["website"]]
    invalid_websites = []
    for row in rows:
        website = row["website"]
        if not website:
            continue
        parsed = urlparse(website)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            invalid_websites.append(row)
        if parsed.netloc.casefold().removeprefix("www.") in {
            "jobs.8vc.com",
            "api.getro.com",
            "getro.com",
        }:
            invalid_websites.append(row)

    names = [row["name"].casefold() for row in rows]
    duplicate_names = {
        name: [row for row in rows if row["name"].casefold() == name]
        for name, count in Counter(names).items()
        if count > 1
    }
    domain_groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if row["website"]:
            host = urlparse(row["website"]).netloc.casefold().removeprefix("www.")
            domain_groups[host].append(row)
    duplicate_domains = {host: group for host, group in domain_groups.items() if len(group) > 1}

    normalized_dir = OUT / "normalized"
    normalized_dir.mkdir()
    txt_path = normalized_dir / "8vc-hiring-companies.txt"
    json_path = normalized_dir / "8vc-hiring-companies.json"
    txt_path.write_text(
        "\n".join(f'{row["name"]} + {row["website"]}' for row in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )
    json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    (OUT / "unresolved-organizations.json").write_text(
        json.dumps(unresolved, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    (OUT / "missing-websites.json").write_text(
        json.dumps(missing_websites, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    (OUT / "samples.json").write_text(
        json.dumps(
            {
                "job_keys": sorted(jobs[0].keys()) if jobs else [],
                "job_organization_keys": sorted(job_organization(jobs[0]).keys()) if jobs else [],
                "company_keys": sorted(companies[0].keys()) if companies else [],
                "first_job": jobs[0] if jobs else None,
                "first_company": companies[0] if companies else None,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    audit = independent_audit(jobs_capture["raw_dir"], companies_capture["raw_dir"], rows, txt_path)
    (OUT / "independent-audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )

    validation = {
        "source": JOBS_PAGE,
        "collection_id": COLLECTION_ID,
        "jobs_capture": jobs_capture["summary"],
        "jobs_attempts": jobs_capture["all_attempt_summaries"],
        "companies_capture": companies_capture["summary"],
        "companies_attempts": companies_capture["all_attempt_summaries"],
        "job_organization_group_count": resolution["organization_group_count"],
        "resolved_hiring_company_count": len(rows),
        "output_line_count": len(rows),
        "unresolved_organization_count": len(unresolved),
        "jobs_without_usable_organization_count": len(unusable_jobs),
        "missing_website_count": len(missing_websites),
        "invalid_website_count": len(invalid_websites),
        "duplicate_name_group_count": len(duplicate_names),
        "duplicate_domain_group_count": len(duplicate_domains),
        "duplicate_names": duplicate_names,
        "duplicate_domains": duplicate_domains,
        "independent_audit": audit,
    }
    hard_errors: list[str] = []
    if not jobs_capture["summary"]["stable"]:
        hard_errors.append("jobs snapshot was not stable after three attempts")
    if not companies_capture["summary"]["stable"]:
        hard_errors.append("companies snapshot was not stable after three attempts")
    if unresolved:
        hard_errors.append("unresolved hiring organizations")
    if unusable_jobs:
        hard_errors.append("jobs without usable organization identity")
    if missing_websites:
        hard_errors.append("missing company websites")
    if invalid_websites:
        hard_errors.append("invalid company websites")
    if len(rows) != len({row["source_id"] for row in rows}):
        hard_errors.append("duplicate normalized source IDs")
    if audit["status"] != "PASS":
        hard_errors.append("independent audit failed")
    validation["hard_errors"] = hard_errors
    validation["status"] = "PASS" if not hard_errors else "NEEDS_REVIEW"
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
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

    print("VALIDATION_SUMMARY_START")
    print(
        json.dumps(
            {
                "status": validation["status"],
                "jobs_reported_total": jobs_capture["summary"]["reported_total"],
                "jobs_raw_count": jobs_capture["summary"]["raw_count"],
                "jobs_unique_id_count": jobs_capture["summary"]["unique_id_count"],
                "companies_reported_total": companies_capture["summary"]["reported_total"],
                "companies_raw_count": companies_capture["summary"]["raw_count"],
                "resolved_hiring_company_count": len(rows),
                "output_line_count": len(rows),
                "unresolved_organization_count": len(unresolved),
                "missing_website_count": len(missing_websites),
                "duplicate_name_group_count": len(duplicate_names),
                "duplicate_domain_group_count": len(duplicate_domains),
                "independent_audit": audit["status"],
                "hard_errors": hard_errors,
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
        error = {
            "status": "FATAL",
            "error": repr(exc),
            "traceback": traceback.format_exc(),
        }
        (OUT / "fatal-error.json").write_text(
            json.dumps(error, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )
        print(error["traceback"], file=sys.stderr)
        # Exit successfully so GitHub always uploads the evidence and diagnostic artifact.
        sys.exit(0)
