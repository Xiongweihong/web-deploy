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
RAW = OUT / "raw"
NORMALIZED = OUT / "normalized"
USER_AGENT = "Mozilla/5.0 (compatible; 8vc-evidence-crawler/3.0)"

# Reviewed correction used only when the official Getro company record has no website.
REVIEWED_WEBSITE_OVERRIDES = {
    "mainshares": {
        "website": "https://mainshares.com",
        "evidence_url": "https://jobs.8vc.com/companies/mainshares-2-cb16c0ea-98ad-44b4-9724-502702d55d21",
        "note": "The official 8VC/Getro company profile identifies mainshares.com.",
    }
}


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


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize_url(value: object) -> str:
    if isinstance(value, dict):
        value = value.get("url") or value.get("href") or value.get("value") or value.get("label")
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


def canonical_company_id(company: dict) -> str:
    value = first(company, "id", "objectID", "object_id", "slug")
    if value not in (None, ""):
        return clean(value)
    return "name:" + normalized_name(first(company, "name", "title"))


def job_id(job: dict) -> str:
    return clean(first(job, "id", "objectID", "object_id", "slug", "url", "apply_url"))


def job_organization(job: dict) -> dict:
    value = job.get("organization") or job.get("company") or {}
    return value if isinstance(value, dict) else {"name": value}


def raw_identifier_aliases(record: dict) -> list[str]:
    values: list[str] = []
    for key in ("id", "objectID", "object_id"):
        value = record.get(key)
        if value not in (None, ""):
            values.append("id:" + clean(value))
    return list(dict.fromkeys(values))


def slug_alias(record: dict) -> str:
    value = record.get("slug")
    return "slug:" + clean(value).casefold() if value not in (None, "") else ""


def name_alias(record: dict) -> str:
    value = normalized_name(first(record, "name", "title"))
    return "name:" + value if value else ""


def post_json(url: str, payload: dict, referer: str, attempts: int = 6) -> tuple[dict, bytes, dict]:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "accept": "application/json",
                "content-type": "application/json",
                "origin": ORIGIN,
                "referer": referer,
                "user-agent": USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                raw = response.read()
                data = json.loads(raw)
                return (
                    data,
                    raw,
                    {
                        "status": response.status,
                        "content_type": response.headers.get("content-type"),
                        "date": response.headers.get("date"),
                        "etag": response.headers.get("etag"),
                        "last_modified": response.headers.get("last-modified"),
                        "sha256": digest(raw),
                        "request_payload": payload,
                        "fetched_at_epoch": time.time(),
                    },
                )
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            if isinstance(exc, urllib.error.HTTPError) and exc.code < 500 and exc.code not in {408, 429}:
                detail = exc.read().decode("utf-8", "replace")
                raise RuntimeError(f"HTTP {exc.code}: {detail[:2000]}") from exc
            if attempt < attempts:
                time.sleep(min(20, 2**attempt))
    raise RuntimeError(f"request failed after {attempts} attempts: {url}: {last_error}")


def payload_for(kind: str, page: int) -> dict:
    if kind == "jobs":
        return {"hits_per_page": 100, "page": page}
    return {"hitsPerPage": 100, "page": page, "query": "", "filters": ""}


def records_from_response(kind: str, data: dict) -> tuple[list[dict], int | None]:
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, dict):
        raise RuntimeError(f"{kind}: missing results object")
    key = "jobs" if kind == "jobs" else "companies"
    records = results.get(key) or []
    if not isinstance(records, list):
        raise RuntimeError(f"{kind}: {key} is not a list")
    count = results.get("count")
    return records, int(count) if count is not None else None


def fetch_kind(kind: str, snapshot: int) -> dict:
    endpoint = JOBS_ENDPOINT if kind == "jobs" else COMPANIES_ENDPOINT
    referer = JOBS_PAGE if kind == "jobs" else COMPANIES_PAGE
    raw_dir = RAW / f"snapshot-{snapshot:02d}" / kind
    raw_dir.mkdir(parents=True, exist_ok=True)

    all_records: list[dict] = []
    page_summaries: list[dict] = []
    reported_total: int | None = None
    first_page_ids: list[str] = []
    seen_hashes: set[str] = set()
    terminal: dict | None = None

    for page in range(1000):
        data, raw, meta = post_json(endpoint, payload_for(kind, page), referer)
        records, current_total = records_from_response(kind, data)
        if reported_total is None:
            reported_total = current_total
        elif current_total is not None and current_total != reported_total:
            raise RuntimeError(f"{kind}: total changed in snapshot {snapshot}: {reported_total} -> {current_total}")

        raw_path = raw_dir / f"page-{page:06d}.json"
        meta_path = raw_dir / f"page-{page:06d}.meta.json"
        raw_path.write_bytes(raw)
        meta.update(
            {
                "kind": kind,
                "snapshot": snapshot,
                "page": page,
                "record_count": len(records),
                "reported_total": reported_total,
                "raw_file": raw_path.name,
            }
        )
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

        if records and meta["sha256"] in seen_hashes:
            raise RuntimeError(f"{kind}: repeated non-empty page body at page {page}")
        seen_hashes.add(meta["sha256"])

        ids = [job_id(row) if kind == "jobs" else canonical_company_id(row) for row in records]
        if page == 0:
            first_page_ids = ids
        all_records.extend(records)
        page_summaries.append({"page": page, "records": len(records), "sha256": meta["sha256"]})
        print(
            f"{kind} snapshot={snapshot} page={page} records={len(records)} "
            f"accumulated={len(all_records)} total={reported_total}"
        )

        if not records:
            terminal = {"type": "empty_page", "page": page, "count": len(all_records)}
            break
        if reported_total is not None and len(all_records) >= reported_total:
            terminal = {"type": "reported_total_reached", "page": page, "count": len(all_records)}
            break
        time.sleep(0.03)
    else:
        raise RuntimeError(f"{kind}: max page guard reached")

    # Recheck page zero after the complete pagination walk to detect an unstable source snapshot.
    recheck, recheck_raw, recheck_meta = post_json(endpoint, payload_for(kind, 0), referer)
    recheck_records, recheck_total = records_from_response(kind, recheck)
    recheck_path = raw_dir / "page-zero-recheck.json"
    recheck_path.write_bytes(recheck_raw)
    recheck_meta.update(
        {
            "kind": kind,
            "snapshot": snapshot,
            "page": 0,
            "recheck": True,
            "record_count": len(recheck_records),
            "reported_total": recheck_total,
        }
    )
    recheck_path.with_suffix(".json.meta.json").write_text(
        json.dumps(recheck_meta, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )

    ids = [job_id(row) if kind == "jobs" else canonical_company_id(row) for row in all_records]
    groups: dict[str, list[dict]] = defaultdict(list)
    for key, record in zip(ids, all_records):
        groups[key].append(record)
    duplicate_ids = sorted(key for key, group in groups.items() if key and len(group) > 1)
    conflicting_duplicate_ids = sorted(
        key
        for key, group in groups.items()
        if key and len(group) > 1 and len({canonical_json(record) for record in group}) > 1
    )
    empty_id_indexes = [index for index, key in enumerate(ids) if not key]
    first_page_ids_after = [
        job_id(row) if kind == "jobs" else canonical_company_id(row) for row in recheck_records
    ]
    unique_id_count = len(set(ids))
    base_stable = (
        reported_total is not None
        and reported_total == recheck_total
        and len(all_records) == reported_total
        and not empty_id_indexes
        and not conflicting_duplicate_ids
        and first_page_ids == first_page_ids_after
    )
    stable = base_stable and (kind == "jobs" or unique_id_count == reported_total)

    summary = {
        "kind": kind,
        "snapshot": snapshot,
        "reported_total": reported_total,
        "reported_total_after": recheck_total,
        "raw_count": len(all_records),
        "unique_id_count": unique_id_count,
        "duplicate_ids": duplicate_ids,
        "conflicting_duplicate_ids": conflicting_duplicate_ids,
        "empty_id_indexes": empty_id_indexes,
        "first_page_stable": first_page_ids == first_page_ids_after,
        "page_count": len(page_summaries),
        "page_sizes": [page["records"] for page in page_summaries],
        "terminal": terminal,
        "stable": stable,
    }
    (raw_dir / "capture-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    return {"records": all_records, "summary": summary, "raw_dir": raw_dir}


def deduplicate_jobs(jobs: list[dict]) -> tuple[list[dict], list[str], list[str]]:
    by_id: dict[str, dict] = {}
    exact_duplicate_ids: list[str] = []
    conflicting_duplicate_ids: list[str] = []
    for job in jobs:
        key = job_id(job)
        if not key:
            continue
        if key not in by_id:
            by_id[key] = job
            continue
        if canonical_json(by_id[key]) == canonical_json(job):
            exact_duplicate_ids.append(key)
        else:
            conflicting_duplicate_ids.append(key)
    return list(by_id.values()), sorted(set(exact_duplicate_ids)), sorted(set(conflicting_duplicate_ids))


def build_company_indexes(companies: list[dict]) -> dict:
    company_by_id: dict[str, dict] = {}
    id_index: dict[str, set[str]] = defaultdict(set)
    slug_index: dict[str, set[str]] = defaultdict(set)
    name_index: dict[str, set[str]] = defaultdict(set)

    for company in companies:
        key = canonical_company_id(company)
        if not key:
            raise RuntimeError("company without canonical identity")
        if key in company_by_id and canonical_json(company_by_id[key]) != canonical_json(company):
            raise RuntimeError(f"conflicting duplicate company identity: {key}")
        company_by_id[key] = company
        for alias in raw_identifier_aliases(company):
            id_index[alias].add(key)
        slug = slug_alias(company)
        if slug:
            slug_index[slug].add(key)
        name = name_alias(company)
        if name:
            name_index[name].add(key)
    return {
        "company_by_id": company_by_id,
        "id_index": id_index,
        "slug_index": slug_index,
        "name_index": name_index,
    }


def resolve_org(org: dict, indexes: dict) -> tuple[str | None, str, dict]:
    id_candidates: set[str] = set()
    id_matches: dict[str, list[str]] = {}
    for alias in raw_identifier_aliases(org):
        matches = indexes["id_index"].get(alias, set())
        if matches:
            id_matches[alias] = sorted(matches)
            id_candidates.update(matches)
    if len(id_candidates) == 1:
        return next(iter(id_candidates)), "stable_id", {"id_matches": id_matches}
    if len(id_candidates) > 1:
        return None, "ambiguous_stable_id", {"id_matches": id_matches}

    slug = slug_alias(org)
    slug_candidates = indexes["slug_index"].get(slug, set()) if slug else set()
    if len(slug_candidates) == 1:
        return next(iter(slug_candidates)), "slug", {"slug": slug, "matches": sorted(slug_candidates)}
    if len(slug_candidates) > 1:
        return None, "ambiguous_slug", {"slug": slug, "matches": sorted(slug_candidates)}

    name = name_alias(org)
    name_candidates = indexes["name_index"].get(name, set()) if name else set()
    if len(name_candidates) == 1:
        return next(iter(name_candidates)), "name", {"name": name, "matches": sorted(name_candidates)}
    return None, "unresolved_or_ambiguous_name", {"name": name, "matches": sorted(name_candidates)}


def resolve_hiring_companies(jobs: list[dict], companies: list[dict]) -> dict:
    unique_jobs, exact_duplicate_job_ids, conflicting_duplicate_job_ids = deduplicate_jobs(jobs)
    indexes = build_company_indexes(companies)
    org_groups: dict[str, dict] = {}
    unusable_jobs: list[dict] = []

    for job in unique_jobs:
        org = job_organization(job)
        org_name = clean(first(org, "name", "title"))
        identity = clean(first(org, "id", "objectID", "object_id", "slug"))
        if not identity:
            identity = "name:" + normalized_name(org_name)
        if not org_name or identity == "name:":
            unusable_jobs.append({"job_id": job_id(job), "organization": org})
            continue
        group = org_groups.setdefault(identity, {"organization": org, "job_ids": []})
        group["job_ids"].append(job_id(job))

    resolved_by_company: dict[str, dict] = {}
    unresolved: list[dict] = []
    applied_overrides: list[dict] = []

    for identity, group in org_groups.items():
        org = group["organization"]
        company_key, method, evidence = resolve_org(org, indexes)
        if not company_key:
            unresolved.append(
                {
                    "organization_identity": identity,
                    "name": clean(first(org, "name", "title")),
                    "job_count": len(group["job_ids"]),
                    "job_ids": group["job_ids"],
                    "resolution_method": method,
                    "resolution_evidence": evidence,
                    "raw_organization": org,
                }
            )
            continue

        company = indexes["company_by_id"][company_key]
        raw_website = first(company, "domain", "website", "website_url")
        website = normalize_url(raw_website)
        provenance = "getro_company_domain" if website else "missing"
        company_name = clean(first(company, "name", "title"))
        override = REVIEWED_WEBSITE_OVERRIDES.get(company_name.casefold())
        if not website and override:
            website = normalize_url(override["website"])
            provenance = "reviewed_official_profile_override"
            applied_overrides.append(
                {
                    "source_id": company_key,
                    "company_name": company_name,
                    "old_value": clean(raw_website),
                    "new_website": website,
                    **override,
                }
            )

        row = resolved_by_company.get(company_key)
        if row is None:
            row = {
                "source_id": company_key,
                "source_slug": clean(first(company, "slug")),
                "name": company_name,
                "website": website,
                "raw_domain": clean(raw_website),
                "website_provenance": provenance,
                "active_job_count_from_unique_jobs": 0,
                "active_job_count_from_directory": first(
                    company, "active_jobs_count", "jobs_count", "job_count"
                ),
                "organization_identities": [],
                "resolution_methods": [],
            }
            resolved_by_company[company_key] = row
        row["active_job_count_from_unique_jobs"] += len(group["job_ids"])
        row["organization_identities"].append(identity)
        row["resolution_methods"].append(method)

    rows = sorted(resolved_by_company.values(), key=lambda row: (row["name"].casefold(), row["source_id"]))
    return {
        "rows": rows,
        "unresolved": unresolved,
        "unusable_jobs": unusable_jobs,
        "organization_group_count": len(org_groups),
        "unique_job_count": len(unique_jobs),
        "exact_duplicate_job_ids": exact_duplicate_job_ids,
        "conflicting_duplicate_job_ids": conflicting_duplicate_job_ids,
        "applied_overrides": applied_overrides,
    }


def capture_snapshot(snapshot: int) -> dict:
    jobs_capture = fetch_kind("jobs", snapshot)
    companies_capture = fetch_kind("companies", snapshot)
    resolution = resolve_hiring_companies(jobs_capture["records"], companies_capture["records"])
    snapshot_dir = RAW / f"snapshot-{snapshot:02d}"
    (snapshot_dir / "resolved-company-rows.json").write_text(
        json.dumps(resolution["rows"], ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    (snapshot_dir / "resolution-summary.json").write_text(
        json.dumps(
            {
                "organization_group_count": resolution["organization_group_count"],
                "unique_job_count": resolution["unique_job_count"],
                "resolved_company_count": len(resolution["rows"]),
                "unresolved": resolution["unresolved"],
                "unusable_jobs": resolution["unusable_jobs"],
                "exact_duplicate_job_ids": resolution["exact_duplicate_job_ids"],
                "conflicting_duplicate_job_ids": resolution["conflicting_duplicate_job_ids"],
                "applied_overrides": resolution["applied_overrides"],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return {
        "snapshot": snapshot,
        "jobs_capture": jobs_capture,
        "companies_capture": companies_capture,
        "resolution": resolution,
        "dir": snapshot_dir,
    }


# Independent audit implementation. It does not call resolve_hiring_companies or its index builders.
def audit_reconstruct(jobs: list[dict], companies: list[dict]) -> dict:
    company_by_key: dict[str, dict] = {}
    raw_id_to_keys: dict[str, set[str]] = defaultdict(set)
    slug_to_keys: dict[str, set[str]] = defaultdict(set)
    name_to_keys: dict[str, set[str]] = defaultdict(set)
    for company in companies:
        key = clean(company.get("id") or company.get("objectID") or company.get("object_id") or company.get("slug"))
        if not key:
            key = "name:" + clean(company.get("name") or company.get("title")).casefold()
        company_by_key[key] = company
        for field in ("id", "objectID", "object_id"):
            value = company.get(field)
            if value not in (None, ""):
                raw_id_to_keys[clean(value)].add(key)
        if company.get("slug") not in (None, ""):
            slug_to_keys[clean(company.get("slug")).casefold()].add(key)
        company_name = clean(company.get("name") or company.get("title")).casefold()
        if company_name:
            name_to_keys[company_name].add(key)

    unique_jobs: dict[str, dict] = {}
    conflicts: list[str] = []
    for job in jobs:
        key = clean(job.get("id") or job.get("objectID") or job.get("object_id") or job.get("slug") or job.get("url"))
        if not key:
            continue
        if key in unique_jobs and canonical_json(unique_jobs[key]) != canonical_json(job):
            conflicts.append(key)
        else:
            unique_jobs.setdefault(key, job)

    grouped: dict[str, dict] = {}
    unusable: list[str] = []
    for key, job in unique_jobs.items():
        org_value = job.get("organization") or job.get("company") or {}
        org = org_value if isinstance(org_value, dict) else {"name": org_value}
        name = clean(org.get("name") or org.get("title"))
        identity = clean(org.get("id") or org.get("objectID") or org.get("object_id") or org.get("slug"))
        if not identity:
            identity = "name:" + name.casefold()
        if not name:
            unusable.append(key)
            continue
        grouped.setdefault(identity, {"org": org, "job_ids": []})["job_ids"].append(key)

    output: dict[str, dict] = {}
    unresolved: list[dict] = []
    applied_overrides: list[dict] = []
    for identity, group in grouped.items():
        org = group["org"]
        candidates: set[str] = set()
        for field in ("id", "objectID", "object_id"):
            value = org.get(field)
            if value not in (None, ""):
                candidates.update(raw_id_to_keys.get(clean(value), set()))
        if not candidates and org.get("slug") not in (None, ""):
            candidates.update(slug_to_keys.get(clean(org.get("slug")).casefold(), set()))
        if not candidates:
            candidates.update(name_to_keys.get(clean(org.get("name") or org.get("title")).casefold(), set()))
        if len(candidates) != 1:
            unresolved.append({"identity": identity, "candidates": sorted(candidates), "organization": org})
            continue
        company_key = next(iter(candidates))
        company = company_by_key[company_key]
        name = clean(company.get("name") or company.get("title"))
        raw_website = company.get("domain") or company.get("website") or company.get("website_url")
        website = normalize_url(raw_website)
        if not website and name.casefold() in REVIEWED_WEBSITE_OVERRIDES:
            website = normalize_url(REVIEWED_WEBSITE_OVERRIDES[name.casefold()]["website"])
            applied_overrides.append({"source_id": company_key, "name": name, "website": website})
        row = output.setdefault(
            company_key,
            {
                "source_id": company_key,
                "name": name,
                "website": website,
                "active_job_count_from_unique_jobs": 0,
            },
        )
        row["active_job_count_from_unique_jobs"] += len(group["job_ids"])

    rows = sorted(output.values(), key=lambda row: (row["name"].casefold(), row["source_id"]))
    return {
        "rows": rows,
        "unresolved": unresolved,
        "unusable_jobs": unusable,
        "conflicting_duplicate_job_ids": sorted(set(conflicts)),
        "unique_job_count": len(unique_jobs),
        "applied_overrides": applied_overrides,
    }


def independent_audit(snapshot_dir: Path, normalized_rows: list[dict], txt_path: Path) -> dict:
    raw_hash_errors: list[str] = []
    audit_jobs: list[dict] = []
    audit_companies: list[dict] = []
    for kind, destination in (("jobs", audit_jobs), ("companies", audit_companies)):
        raw_dir = snapshot_dir / kind
        record_key = "jobs" if kind == "jobs" else "companies"
        for raw_path in sorted(raw_dir.glob("page-*.json")):
            if raw_path.name == "page-zero-recheck.json":
                continue
            meta_path = raw_path.with_name(raw_path.stem + ".meta.json")
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            actual_hash = digest(raw_path.read_bytes())
            if actual_hash != meta.get("sha256"):
                raw_hash_errors.append(f"{kind}/{raw_path.name}")
            payload = json.loads(raw_path.read_bytes())
            destination.extend(((payload.get("results") or {}).get(record_key) or []))

    reconstructed = audit_reconstruct(audit_jobs, audit_companies)
    audit_rows = reconstructed["rows"]
    primary_map = {
        row["source_id"]: (
            row["name"],
            row["website"],
            row["active_job_count_from_unique_jobs"],
        )
        for row in normalized_rows
    }
    audit_map = {
        row["source_id"]: (
            row["name"],
            row["website"],
            row["active_job_count_from_unique_jobs"],
        )
        for row in audit_rows
    }
    txt_lines = [line for line in txt_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    expected_lines = [f'{row["name"]} + {row["website"]}' for row in normalized_rows]
    status = (
        "PASS"
        if not raw_hash_errors
        and not reconstructed["unresolved"]
        and not reconstructed["unusable_jobs"]
        and not reconstructed["conflicting_duplicate_job_ids"]
        and primary_map == audit_map
        and txt_lines == expected_lines
        else "FAIL"
    )
    return {
        "status": status,
        "raw_hash_errors": raw_hash_errors,
        "raw_job_count": len(audit_jobs),
        "raw_company_count": len(audit_companies),
        "unique_job_count": reconstructed["unique_job_count"],
        "reconstructed_company_count": len(audit_rows),
        "reconstructed_unresolved_count": len(reconstructed["unresolved"]),
        "reconstructed_unusable_job_count": len(reconstructed["unusable_jobs"]),
        "source_id_maps_equal": primary_map == audit_map,
        "txt_lines_equal": txt_lines == expected_lines,
        "txt_line_count": len(txt_lines),
        "applied_overrides": reconstructed["applied_overrides"],
    }


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    RAW.mkdir(parents=True)
    NORMALIZED.mkdir(parents=True)

    snapshots = [capture_snapshot(1), capture_snapshot(2)]
    selected = snapshots[-1]
    rows = selected["resolution"]["rows"]

    txt_path = NORMALIZED / "8vc-hiring-companies.txt"
    json_path = NORMALIZED / "8vc-hiring-companies.json"
    txt_path.write_text(
        "\n".join(f'{row["name"]} + {row["website"]}' for row in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )
    json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    company_maps = [
        {row["source_id"]: (row["name"], row["website"]) for row in snapshot["resolution"]["rows"]}
        for snapshot in snapshots
    ]
    company_count_maps = [
        {
            row["source_id"]: row["active_job_count_from_unique_jobs"]
            for row in snapshot["resolution"]["rows"]
        }
        for snapshot in snapshots
    ]
    snapshot_comparison = {
        "status": "PASS" if company_maps[0] == company_maps[1] else "FAIL",
        "company_identity_maps_equal": company_maps[0] == company_maps[1],
        "company_job_count_maps_equal": company_count_maps[0] == company_count_maps[1],
        "snapshot_1_company_count": len(company_maps[0]),
        "snapshot_2_company_count": len(company_maps[1]),
        "added_company_ids": sorted(set(company_maps[1]) - set(company_maps[0])),
        "removed_company_ids": sorted(set(company_maps[0]) - set(company_maps[1])),
        "changed_company_ids": sorted(
            key for key in set(company_maps[0]) & set(company_maps[1]) if company_maps[0][key] != company_maps[1][key]
        ),
        "snapshot_1_jobs_reported_total": snapshots[0]["jobs_capture"]["summary"]["reported_total"],
        "snapshot_2_jobs_reported_total": snapshots[1]["jobs_capture"]["summary"]["reported_total"],
        "snapshot_1_unique_job_count": snapshots[0]["resolution"]["unique_job_count"],
        "snapshot_2_unique_job_count": snapshots[1]["resolution"]["unique_job_count"],
    }
    (OUT / "snapshot-comparison.json").write_text(
        json.dumps(snapshot_comparison, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )

    (OUT / "override-ledger.json").write_text(
        json.dumps(selected["resolution"]["applied_overrides"], ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (OUT / "unresolved-organizations.json").write_text(
        json.dumps(selected["resolution"]["unresolved"], ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    names = [row["name"].casefold() for row in rows]
    ids = [row["source_id"] for row in rows]
    domains = [urlparse(row["website"]).netloc.casefold().removeprefix("www.") for row in rows if row["website"]]
    duplicate_names = {
        name: [row for row in rows if row["name"].casefold() == name]
        for name, count in Counter(names).items()
        if count > 1
    }
    duplicate_ids = {key: count for key, count in Counter(ids).items() if count > 1}
    duplicate_domains = {
        domain: [row for row in rows if urlparse(row["website"]).netloc.casefold().removeprefix("www.") == domain]
        for domain, count in Counter(domains).items()
        if count > 1
    }
    missing_names = [row for row in rows if not row["name"]]
    missing_websites = [row for row in rows if not row["website"]]
    invalid_websites = []
    for row in rows:
        website = row["website"]
        if not website:
            continue
        parsed = urlparse(website)
        if parsed.scheme != "https" or not parsed.netloc:
            invalid_websites.append(row)
        if parsed.netloc.casefold().removeprefix("www.") in {
            "jobs.8vc.com",
            "api.getro.com",
            "getro.com",
        }:
            invalid_websites.append(row)

    audit = independent_audit(selected["dir"], rows, txt_path)
    (OUT / "independent-audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )

    hard_errors: list[str] = []
    for snapshot in snapshots:
        if not snapshot["jobs_capture"]["summary"]["stable"]:
            hard_errors.append(f'jobs snapshot {snapshot["snapshot"]} was not stable')
        if not snapshot["companies_capture"]["summary"]["stable"]:
            hard_errors.append(f'companies snapshot {snapshot["snapshot"]} was not stable')
        if snapshot["resolution"]["unresolved"]:
            hard_errors.append(f'unresolved organizations in snapshot {snapshot["snapshot"]}')
        if snapshot["resolution"]["unusable_jobs"]:
            hard_errors.append(f'unusable jobs in snapshot {snapshot["snapshot"]}')
        if snapshot["resolution"]["conflicting_duplicate_job_ids"]:
            hard_errors.append(f'conflicting duplicate job IDs in snapshot {snapshot["snapshot"]}')
    if missing_names:
        hard_errors.append("missing company names")
    if missing_websites:
        hard_errors.append("missing company websites")
    if invalid_websites:
        hard_errors.append("invalid company websites")
    if duplicate_ids:
        hard_errors.append("duplicate normalized source IDs")
    if snapshot_comparison["status"] != "PASS":
        hard_errors.append("two complete snapshots have different company identity maps")
    if audit["status"] != "PASS":
        hard_errors.append("independent audit failed")

    validation = {
        "source": JOBS_PAGE,
        "company_directory": COMPANIES_PAGE,
        "collection_id": COLLECTION_ID,
        "generated_at_epoch": time.time(),
        "status": "PASS" if not hard_errors else "NEEDS_REVIEW",
        "selected_snapshot": selected["snapshot"],
        "jobs_capture": selected["jobs_capture"]["summary"],
        "companies_capture": selected["companies_capture"]["summary"],
        "snapshot_summaries": [
            {
                "snapshot": snapshot["snapshot"],
                "jobs": snapshot["jobs_capture"]["summary"],
                "companies": snapshot["companies_capture"]["summary"],
                "unique_job_count": snapshot["resolution"]["unique_job_count"],
                "organization_group_count": snapshot["resolution"]["organization_group_count"],
                "resolved_hiring_company_count": len(snapshot["resolution"]["rows"]),
                "exact_duplicate_job_ids": snapshot["resolution"]["exact_duplicate_job_ids"],
                "unresolved_organization_count": len(snapshot["resolution"]["unresolved"]),
            }
            for snapshot in snapshots
        ],
        "job_organization_group_count": selected["resolution"]["organization_group_count"],
        "resolved_hiring_company_count": len(rows),
        "output_line_count": len(rows),
        "unique_source_id_count": len(set(ids)),
        "unique_name_count": len(set(names)),
        "unique_domain_count": len(set(domains)),
        "unresolved_organization_count": len(selected["resolution"]["unresolved"]),
        "jobs_without_usable_organization_count": len(selected["resolution"]["unusable_jobs"]),
        "missing_name_count": len(missing_names),
        "missing_website_count": len(missing_websites),
        "invalid_website_count": len(invalid_websites),
        "duplicate_id_groups": duplicate_ids,
        "duplicate_name_groups": duplicate_names,
        "duplicate_domain_groups": duplicate_domains,
        "exact_duplicate_job_ids": selected["resolution"]["exact_duplicate_job_ids"],
        "conflicting_duplicate_job_ids": selected["resolution"]["conflicting_duplicate_job_ids"],
        "applied_overrides": selected["resolution"]["applied_overrides"],
        "snapshot_comparison": snapshot_comparison,
        "independent_audit": audit,
        "hard_errors": hard_errors,
        "notes": [
            "Output contains companies represented by at least one unique job record in the complete /jobs API snapshot.",
            "Exact duplicate job records are reported but deduplicated when deriving company job counts.",
            "Company websites come from Getro company records, except reviewed official-profile overrides recorded in override-ledger.json.",
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
                "jobs_reported_total": selected["jobs_capture"]["summary"]["reported_total"],
                "jobs_raw_count": selected["jobs_capture"]["summary"]["raw_count"],
                "jobs_unique_id_count": selected["jobs_capture"]["summary"]["unique_id_count"],
                "companies_reported_total": selected["companies_capture"]["summary"]["reported_total"],
                "companies_raw_count": selected["companies_capture"]["summary"]["raw_count"],
                "resolved_hiring_company_count": len(rows),
                "output_line_count": len(rows),
                "missing_website_count": len(missing_websites),
                "duplicate_name_group_count": len(duplicate_names),
                "duplicate_domain_group_count": len(duplicate_domains),
                "snapshot_comparison": snapshot_comparison["status"],
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
        # Preserve the diagnostic artifact even when extraction fails.
        sys.exit(0)
