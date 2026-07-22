from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
import time
import traceback
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlparse

import requests

COLLECTION_ID = "1508"
ORIGIN = "https://jobs.variant.fund"
JOBS_PAGE = ORIGIN + "/jobs"
COMPANIES_PAGE = ORIGIN + "/companies"
API_BASE = f"https://api.getro.com/api/v2/collections/{COLLECTION_ID}"
JOBS_ENDPOINT = API_BASE + "/search/jobs"
COMPANIES_ENDPOINT = API_BASE + "/search/companies"
META_ENDPOINT = API_BASE
OUT = Path("artifact")

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 (compatible; variant-evidence-crawler/1.0)"})


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
    return clean(first(company, "id", "objectID", "object_id", "slug", "name"))


def job_id(job: dict) -> str:
    return clean(first(job, "id", "objectID", "object_id", "slug", "url"))


def job_organization(job: dict) -> dict:
    value = job.get("organization") or job.get("company") or {}
    return value if isinstance(value, dict) else {"name": value}


def aliases(record: dict) -> list[str]:
    values: list[str] = []
    for key in ("id", "objectID", "object_id", "slug"):
        value = record.get(key)
        if value not in (None, ""):
            values.append("raw:" + clean(value))
    name = normalized_name(first(record, "name", "title"))
    if name:
        values.append("name:" + name)
    return list(dict.fromkeys(values))


def request_json(method: str, url: str, **kwargs) -> tuple[dict, bytes, dict]:
    last: Exception | None = None
    for attempt in range(1, 6):
        try:
            response = SESSION.request(method, url, timeout=90, **kwargs)
            if response.status_code >= 500 or response.status_code in {408, 429}:
                raise RuntimeError(f"transient HTTP {response.status_code}")
            response.raise_for_status()
            raw = response.content
            return (
                json.loads(raw),
                raw,
                {
                    "status": response.status_code,
                    "date": response.headers.get("date"),
                    "etag": response.headers.get("etag"),
                    "content_type": response.headers.get("content-type"),
                    "bytes": len(raw),
                    "sha256": digest(raw),
                    "fetched_at_epoch": time.time(),
                },
            )
        except Exception as exc:
            last = exc
            if attempt == 5:
                break
            time.sleep(min(16, 2**attempt))
    raise RuntimeError(f"request failed for {url}: {last}")


def payload_for(kind: str, page: int) -> dict:
    if kind == "jobs":
        return {"hits_per_page": 100, "page": page}
    return {"hitsPerPage": 100, "page": page, "query": "", "filters": ""}


def record_id(kind: str, record: dict) -> str:
    return job_id(record) if kind == "jobs" else canonical_company_id(record)


def fetch_capture(kind: str, capture_number: int) -> dict:
    endpoint = JOBS_ENDPOINT if kind == "jobs" else COMPANIES_ENDPOINT
    referer = JOBS_PAGE if kind == "jobs" else COMPANIES_PAGE
    record_key = "jobs" if kind == "jobs" else "companies"
    raw_dir = OUT / "raw" / f"capture-{capture_number:02d}" / kind
    raw_dir.mkdir(parents=True, exist_ok=True)

    all_records: list[dict] = []
    page_summaries: list[dict] = []
    reported_total: int | None = None
    first_page_ids: list[str] = []
    seen_nonempty_hashes: set[str] = set()
    terminal: dict | None = None

    for page in range(100):
        payload = payload_for(kind, page)
        data, raw, meta = request_json(
            "POST",
            endpoint,
            json=payload,
            headers={
                "accept": "application/json",
                "content-type": "application/json",
                "origin": ORIGIN,
                "referer": referer,
            },
        )
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
            raise RuntimeError(
                f"{kind}: reported total changed inside capture {capture_number}: "
                f"{reported_total} -> {current_total}"
            )

        raw_path = raw_dir / f"page-{page:06d}.json"
        meta_path = raw_dir / f"page-{page:06d}.meta.json"
        raw_path.write_bytes(raw)
        meta.update(
            {
                "kind": kind,
                "capture": capture_number,
                "page": page,
                "request_payload": payload,
                "record_count": len(records),
                "reported_total": reported_total,
                "raw_file": raw_path.name,
            }
        )
        meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")

        if records and meta["sha256"] in seen_nonempty_hashes:
            raise RuntimeError(f"{kind}: repeated non-empty page body at page {page}")
        seen_nonempty_hashes.add(meta["sha256"])
        ids = [record_id(kind, record) for record in records]
        if page == 0:
            first_page_ids = ids
        all_records.extend(records)
        page_summaries.append(
            {"page": page, "record_count": len(records), "sha256": meta["sha256"]}
        )
        print(
            f"{kind} capture={capture_number} page={page} records={len(records)} "
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
        raise RuntimeError(f"{kind}: max page guard reached")

    recheck_data, recheck_raw, recheck_meta = request_json(
        "POST",
        endpoint,
        json=payload_for(kind, 0),
        headers={
            "accept": "application/json",
            "content-type": "application/json",
            "origin": ORIGIN,
            "referer": referer,
        },
    )
    recheck_path = raw_dir / "page-zero-recheck.json"
    recheck_path.write_bytes(recheck_raw)
    recheck_results = recheck_data.get("results") or {}
    recheck_records = recheck_results.get(record_key) or []
    total_after = int(recheck_results.get("count") or 0)
    recheck_meta.update(
        {
            "kind": kind,
            "capture": capture_number,
            "purpose": "page_zero_stability_recheck",
            "reported_total": total_after,
            "record_count": len(recheck_records),
        }
    )
    recheck_path.with_suffix(".json.meta.json").write_text(
        json.dumps(recheck_meta, indent=2, sort_keys=True), encoding="utf-8"
    )

    ids = [record_id(kind, record) for record in all_records]
    duplicate_ids = sorted(key for key, count in Counter(ids).items() if key and count > 1)
    empty_id_indexes = [index for index, value in enumerate(ids) if not value]
    first_page_ids_after = [record_id(kind, record) for record in recheck_records]
    summary = {
        "kind": kind,
        "capture": capture_number,
        "reported_total": reported_total,
        "reported_total_after": total_after,
        "raw_count": len(all_records),
        "unique_id_count": len(set(ids)),
        "duplicate_ids": duplicate_ids,
        "empty_id_indexes": empty_id_indexes,
        "page_count": len(page_summaries),
        "page_sizes": [item["record_count"] for item in page_summaries],
        "first_page_stable": first_page_ids == first_page_ids_after,
        "terminal": terminal,
        "page_summaries": page_summaries,
    }
    summary["count_complete"] = (
        reported_total is not None and len(all_records) == reported_total
    )
    summary["stable"] = (
        summary["count_complete"]
        and reported_total == total_after
        and not empty_id_indexes
        and summary["first_page_stable"]
    )
    (raw_dir / "capture-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    return {"records": all_records, "summary": summary, "raw_dir": raw_dir}


def build_company_index(companies: list[dict]) -> tuple[dict[str, dict], dict[str, set[str]]]:
    company_by_id: dict[str, dict] = {}
    alias_index: dict[str, set[str]] = defaultdict(set)
    for company in companies:
        company_id_value = canonical_company_id(company)
        if not company_id_value:
            raise RuntimeError("company record without a stable identity")
        if company_id_value in company_by_id:
            raise RuntimeError(f"duplicate company identity: {company_id_value}")
        company_by_id[company_id_value] = company
        for alias in aliases(company):
            alias_index[alias].add(company_id_value)
    return company_by_id, alias_index


def resolve_hiring_companies(jobs: list[dict], companies: list[dict]) -> dict:
    company_by_id, alias_index = build_company_index(companies)
    organization_groups: dict[str, dict] = {}
    unusable_jobs: list[dict] = []

    for job in jobs:
        organization = job_organization(job)
        name = clean(first(organization, "name", "title"))
        identity = clean(first(organization, "id", "objectID", "object_id", "slug"))
        if not identity:
            identity = "name:" + normalized_name(name)
        if not name or identity == "name:":
            unusable_jobs.append({"job_id": job_id(job), "organization": organization})
            continue
        group = organization_groups.setdefault(
            identity, {"organization": organization, "jobs_by_id": {}}
        )
        jid = job_id(job)
        if jid:
            group["jobs_by_id"].setdefault(jid, job)

    resolved_by_company: dict[str, dict] = {}
    unresolved: list[dict] = []
    for organization_identity, group in organization_groups.items():
        organization = group["organization"]
        candidate_ids: set[str] = set()
        matched_aliases: dict[str, list[str]] = {}
        for alias in aliases(organization):
            matches = alias_index.get(alias, set())
            if matches:
                matched_aliases[alias] = sorted(matches)
                candidate_ids.update(matches)
        if len(candidate_ids) != 1:
            unresolved.append(
                {
                    "organization_identity": organization_identity,
                    "name": clean(first(organization, "name", "title")),
                    "unique_job_count": len(group["jobs_by_id"]),
                    "candidate_company_ids": sorted(candidate_ids),
                    "matched_aliases": matched_aliases,
                    "raw_organization": organization,
                }
            )
            continue

        company_id_value = next(iter(candidate_ids))
        company = company_by_id[company_id_value]
        raw_website = first(company, "domain", "website", "website_url")
        website = normalize_url(raw_website)
        row = resolved_by_company.get(company_id_value)
        if row is None:
            row = {
                "source_id": company_id_value,
                "source_slug": clean(first(company, "slug")),
                "name": clean(first(company, "name", "title")),
                "website": website,
                "raw_website": clean(raw_website),
                "website_provenance": "getro_company_domain" if website else "missing",
                "active_job_count_from_unique_jobs": 0,
                "directory_active_jobs_count": first(
                    company, "active_jobs_count", "jobs_count", "job_count"
                ),
                "organization_identities": [],
            }
            resolved_by_company[company_id_value] = row
        row["active_job_count_from_unique_jobs"] += len(group["jobs_by_id"])
        row["organization_identities"].append(organization_identity)

    rows = sorted(
        resolved_by_company.values(), key=lambda item: (item["name"].casefold(), item["source_id"])
    )
    return {
        "rows": rows,
        "unresolved": unresolved,
        "unusable_jobs": unusable_jobs,
        "organization_group_count": len(organization_groups),
    }


# Independent reconstruction intentionally does not invoke resolve_hiring_companies.
def independent_reconstruct(jobs: list[dict], companies: list[dict]) -> dict:
    def aid(record: dict) -> str:
        return clean(
            record.get("id")
            or record.get("objectID")
            or record.get("object_id")
            or record.get("slug")
            or record.get("name")
        )

    def aname(record: dict) -> str:
        return normalized_name(record.get("name") or record.get("title"))

    company_map: dict[str, dict] = {}
    raw_alias_map: dict[str, set[str]] = defaultdict(set)
    name_map: dict[str, set[str]] = defaultdict(set)
    for company in companies:
        cid = aid(company)
        company_map[cid] = company
        for key in ("id", "objectID", "object_id", "slug"):
            if company.get(key) not in (None, ""):
                raw_alias_map[clean(company[key])].add(cid)
        if aname(company):
            name_map[aname(company)].add(cid)

    grouped: dict[str, dict] = {}
    for job in jobs:
        organization = job.get("organization") or job.get("company") or {}
        if not isinstance(organization, dict):
            organization = {"name": organization}
        oid = clean(
            organization.get("id")
            or organization.get("objectID")
            or organization.get("object_id")
            or organization.get("slug")
        )
        oname = aname(organization)
        key = oid or "name:" + oname
        group = grouped.setdefault(key, {"organization": organization, "job_ids": set()})
        jid = clean(job.get("id") or job.get("objectID") or job.get("object_id") or job.get("slug") or job.get("url"))
        if jid:
            group["job_ids"].add(jid)

    rows: list[dict] = []
    unresolved: list[dict] = []
    for key, group in grouped.items():
        organization = group["organization"]
        candidate_ids: set[str] = set()
        for raw_key in ("id", "objectID", "object_id", "slug"):
            raw_value = clean(organization.get(raw_key))
            if raw_value:
                candidate_ids.update(raw_alias_map.get(raw_value, set()))
        organization_name = aname(organization)
        if organization_name:
            candidate_ids.update(name_map.get(organization_name, set()))
        if len(candidate_ids) != 1:
            unresolved.append({"organization": key, "candidate_ids": sorted(candidate_ids)})
            continue
        cid = next(iter(candidate_ids))
        company = company_map[cid]
        raw_website = company.get("domain") or company.get("website") or company.get("website_url")
        rows.append(
            {
                "source_id": cid,
                "name": clean(company.get("name") or company.get("title")),
                "website": normalize_url(raw_website),
                "active_job_count_from_unique_jobs": len(group["job_ids"]),
            }
        )
    rows.sort(key=lambda item: (item["name"].casefold(), item["source_id"]))
    return {"rows": rows, "unresolved": unresolved}


def read_raw_records(raw_dir: Path, record_key: str) -> tuple[list[dict], list[str]]:
    records: list[dict] = []
    hash_errors: list[str] = []
    for raw_path in sorted(raw_dir.glob("page-*.json")):
        if raw_path.name == "page-zero-recheck.json":
            continue
        meta_path = raw_path.with_name(raw_path.stem + ".meta.json")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        actual_hash = digest(raw_path.read_bytes())
        if actual_hash != meta.get("sha256"):
            hash_errors.append(str(raw_path))
        payload = json.loads(raw_path.read_bytes())
        records.extend(((payload.get("results") or {}).get(record_key) or []))
    return records, hash_errors


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    meta_data, meta_raw, meta_info = request_json(
        "GET", META_ENDPOINT, headers={"accept": "application/json"}
    )
    meta_dir = OUT / "raw" / "collection"
    meta_dir.mkdir(parents=True)
    (meta_dir / "collection.json").write_bytes(meta_raw)
    (meta_dir / "collection.json.meta.json").write_text(
        json.dumps(meta_info, indent=2, sort_keys=True), encoding="utf-8"
    )

    capture_1_jobs = fetch_capture("jobs", 1)
    capture_1_companies = fetch_capture("companies", 1)
    time.sleep(1)
    capture_2_jobs = fetch_capture("jobs", 2)
    capture_2_companies = fetch_capture("companies", 2)

    resolution_1 = resolve_hiring_companies(
        capture_1_jobs["records"], capture_1_companies["records"]
    )
    resolution_2 = resolve_hiring_companies(
        capture_2_jobs["records"], capture_2_companies["records"]
    )
    rows = resolution_1["rows"]

    normalized_dir = OUT / "normalized"
    normalized_dir.mkdir()
    txt_path = normalized_dir / "variant-hiring-companies.txt"
    json_path = normalized_dir / "variant-hiring-companies.json"
    directory_path = normalized_dir / "variant-directory-companies-all.json"
    txt_path.write_text(
        "\n".join(f'{row["name"]} + {row["website"]}' for row in rows)
        + ("\n" if rows else ""),
        encoding="utf-8",
    )
    json_path.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    directory_path.write_text(
        json.dumps(capture_1_companies["records"], ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    missing_websites = [row for row in rows if not row["website"]]
    invalid_websites: list[dict] = []
    name_groups: dict[str, list[dict]] = defaultdict(list)
    domain_groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        name_groups[row["name"].casefold()].append(row)
        if not row["website"]:
            continue
        parsed = urlparse(row["website"])
        host = parsed.netloc.casefold().removeprefix("www.")
        domain_groups[host].append(row)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or host in {"jobs.variant.fund", "api.getro.com", "getro.com"}
        ):
            invalid_websites.append(row)
    duplicate_names = {key: value for key, value in name_groups.items() if len(value) > 1}
    duplicate_domains = {key: value for key, value in domain_groups.items() if len(value) > 1}

    jobs_by_id: dict[str, list[dict]] = defaultdict(list)
    for job in capture_1_jobs["records"]:
        jobs_by_id[job_id(job)].append(job)
    duplicate_job_groups = {
        key: value for key, value in jobs_by_id.items() if key and len(value) > 1
    }
    nonidentical_duplicate_job_ids = []
    for key, values in duplicate_job_groups.items():
        canonical = {
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            for value in values
        }
        if len(canonical) > 1:
            nonidentical_duplicate_job_ids.append(key)

    directory_positive_ids: set[str] = set()
    directory_positive_job_sum = 0
    for company in capture_1_companies["records"]:
        hint = first(company, "active_jobs_count", "jobs_count", "job_count")
        try:
            count = int(hint)
        except (TypeError, ValueError):
            continue
        if count > 0:
            directory_positive_ids.add(canonical_company_id(company))
            directory_positive_job_sum += count

    raw_jobs, raw_job_hash_errors = read_raw_records(
        capture_1_jobs["raw_dir"], "jobs"
    )
    raw_companies, raw_company_hash_errors = read_raw_records(
        capture_1_companies["raw_dir"], "companies"
    )
    independent = independent_reconstruct(raw_jobs, raw_companies)

    def row_map(values: list[dict]) -> dict[str, tuple]:
        return {
            row["source_id"]: (
                row["name"],
                row["website"],
                row["active_job_count_from_unique_jobs"],
            )
            for row in values
        }

    primary_map = row_map(rows)
    second_map = row_map(resolution_2["rows"])
    audit_map = row_map(independent["rows"])
    txt_lines = [line for line in txt_path.read_text(encoding="utf-8").splitlines() if line]
    expected_lines = [f'{row["name"]} + {row["website"]}' for row in rows]

    independent_audit = {
        "raw_hash_errors": raw_job_hash_errors + raw_company_hash_errors,
        "raw_job_count": len(raw_jobs),
        "raw_company_count": len(raw_companies),
        "reconstructed_company_count": len(independent["rows"]),
        "unresolved_count": len(independent["unresolved"]),
        "source_id_maps_equal": primary_map == audit_map,
        "txt_lines_equal": txt_lines == expected_lines,
        "txt_line_count": len(txt_lines),
    }
    independent_audit["status"] = (
        "PASS"
        if not independent_audit["raw_hash_errors"]
        and independent_audit["unresolved_count"] == 0
        and independent_audit["source_id_maps_equal"]
        and independent_audit["txt_lines_equal"]
        else "FAIL"
    )
    (OUT / "independent-audit.json").write_text(
        json.dumps(independent_audit, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    snapshot_comparison = {
        "jobs_reported_totals_equal": (
            capture_1_jobs["summary"]["reported_total"]
            == capture_2_jobs["summary"]["reported_total"]
        ),
        "job_id_sets_equal": (
            {job_id(job) for job in capture_1_jobs["records"]}
            == {job_id(job) for job in capture_2_jobs["records"]}
        ),
        "directory_id_sets_equal": (
            {canonical_company_id(company) for company in capture_1_companies["records"]}
            == {canonical_company_id(company) for company in capture_2_companies["records"]}
        ),
        "hiring_company_maps_equal": primary_map == second_map,
        "capture_1_company_count": len(rows),
        "capture_2_company_count": len(resolution_2["rows"]),
    }
    snapshot_comparison["status"] = (
        "PASS"
        if snapshot_comparison["job_id_sets_equal"]
        and snapshot_comparison["directory_id_sets_equal"]
        and snapshot_comparison["hiring_company_maps_equal"]
        else "FAIL"
    )
    (OUT / "snapshot-comparison.json").write_text(
        json.dumps(snapshot_comparison, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    validation = {
        "source": JOBS_PAGE,
        "collection_id": COLLECTION_ID,
        "collection_label": (((meta_data.get("data") or {}).get("attributes") or {}).get("label")),
        "collection_metadata_sha256": meta_info["sha256"],
        "jobs_capture_1": capture_1_jobs["summary"],
        "jobs_capture_2": capture_2_jobs["summary"],
        "companies_capture_1": capture_1_companies["summary"],
        "companies_capture_2": capture_2_companies["summary"],
        "directory_company_count": len(capture_1_companies["records"]),
        "job_organization_group_count": resolution_1["organization_group_count"],
        "resolved_hiring_company_count": len(rows),
        "output_line_count": len(txt_lines),
        "unique_output_id_count": len({row["source_id"] for row in rows}),
        "unique_output_name_count": len({row["name"].casefold() for row in rows}),
        "unique_output_domain_count": len(domain_groups),
        "directory_positive_company_count": len(directory_positive_ids),
        "directory_positive_ids_equal_resolved": directory_positive_ids
        == {row["source_id"] for row in rows},
        "directory_positive_job_sum": directory_positive_job_sum,
        "jobs_unique_id_count": len({job_id(job) for job in capture_1_jobs["records"] if job_id(job)}),
        "duplicate_job_id_count": len(duplicate_job_groups),
        "duplicate_job_ids": sorted(duplicate_job_groups),
        "nonidentical_duplicate_job_ids": sorted(nonidentical_duplicate_job_ids),
        "unresolved_organization_count": len(resolution_1["unresolved"]),
        "jobs_without_usable_organization_count": len(resolution_1["unusable_jobs"]),
        "missing_website_count": len(missing_websites),
        "invalid_website_count": len(invalid_websites),
        "duplicate_name_group_count": len(duplicate_names),
        "duplicate_domain_group_count": len(duplicate_domains),
        "duplicate_names": duplicate_names,
        "duplicate_domains": duplicate_domains,
        "snapshot_comparison": snapshot_comparison,
        "independent_audit": independent_audit,
    }

    hard_errors: list[str] = []
    if not capture_1_jobs["summary"]["stable"]:
        hard_errors.append("first jobs capture was not stable")
    if not capture_1_companies["summary"]["stable"]:
        hard_errors.append("first companies capture was not stable")
    if resolution_1["unresolved"]:
        hard_errors.append("unresolved hiring organizations")
    if resolution_1["unusable_jobs"]:
        hard_errors.append("jobs without usable organization identity")
    if missing_websites:
        hard_errors.append("missing company websites")
    if invalid_websites:
        hard_errors.append("invalid company websites")
    if len(rows) != len({row["source_id"] for row in rows}):
        hard_errors.append("duplicate normalized company IDs")
    if nonidentical_duplicate_job_ids:
        hard_errors.append("non-identical duplicate job IDs")
    if snapshot_comparison["status"] != "PASS":
        hard_errors.append("two-capture comparison failed")
    if independent_audit["status"] != "PASS":
        hard_errors.append("independent audit failed")
    validation["hard_errors"] = hard_errors
    validation["status"] = "PASS" if not hard_errors else "NEEDS_REVIEW"

    (OUT / "validation.json").write_text(
        json.dumps(validation, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (OUT / "unresolved-organizations.json").write_text(
        json.dumps(resolution_1["unresolved"], ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (OUT / "missing-websites.json").write_text(
        json.dumps(missing_websites, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    manifest = {
        "generated_at_epoch": time.time(),
        "status": validation["status"],
        "files": {},
    }
    for path in sorted(OUT.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            manifest["files"][str(path.relative_to(OUT))] = {
                "bytes": path.stat().st_size,
                "sha256": digest(path.read_bytes()),
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
                "jobs_reported_total": capture_1_jobs["summary"]["reported_total"],
                "jobs_raw_count": capture_1_jobs["summary"]["raw_count"],
                "jobs_unique_id_count": validation["jobs_unique_id_count"],
                "directory_reported_total": capture_1_companies["summary"]["reported_total"],
                "directory_raw_count": capture_1_companies["summary"]["raw_count"],
                "resolved_hiring_company_count": len(rows),
                "output_line_count": len(txt_lines),
                "missing_website_count": len(missing_websites),
                "unresolved_organization_count": len(resolution_1["unresolved"]),
                "duplicate_name_group_count": len(duplicate_names),
                "duplicate_domain_group_count": len(duplicate_domains),
                "snapshot_comparison": snapshot_comparison["status"],
                "independent_audit": independent_audit["status"],
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
            json.dumps(error, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        print(error["traceback"], file=sys.stderr)
        sys.exit(1)
