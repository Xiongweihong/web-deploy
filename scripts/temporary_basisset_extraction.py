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

ORIGIN = "https://jobs.basisset.com"
JOBS_PAGE = ORIGIN + "/jobs"
COMPANIES_PAGE = ORIGIN + "/companies"
OUT = Path("artifact")
USER_AGENT = "Mozilla/5.0 (compatible; basisset-evidence-crawler/1.0)"
REQUEST_INTERVAL_SECONDS = 2.1


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


def canonical_json_digest(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return digest(payload.encode("utf-8"))


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


def identifier_aliases(record: dict) -> list[str]:
    values: list[str] = []
    for key in ("id", "objectID", "object_id", "slug"):
        value = record.get(key)
        if value not in (None, ""):
            values.append("raw:" + str(value).strip())
    return list(dict.fromkeys(values))


def name_alias(record: dict) -> str:
    value = normalized_name(first(record, "name", "title"))
    return "name:" + value if value else ""


def organization_identity(org: dict) -> str:
    aliases = identifier_aliases(org)
    if aliases:
        return aliases[0]
    return name_alias(org)


def request_bytes(
    url: str,
    *,
    method: str = "GET",
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    attempts: int = 5,
) -> tuple[bytes, dict]:
    merged_headers = {"user-agent": USER_AGENT, "accept": "*/*"}
    if headers:
        merged_headers.update(headers)
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(url, data=data, method=method, headers=merged_headers)
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                raw = response.read()
                return raw, {
                    "status": response.status,
                    "content_type": response.headers.get("content-type"),
                    "date": response.headers.get("date"),
                    "etag": response.headers.get("etag"),
                    "sha256": digest(raw),
                    "fetched_at_epoch": time.time(),
                    "url": response.geturl(),
                    "method": method,
                }
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if isinstance(exc, urllib.error.HTTPError) and exc.code < 500 and exc.code not in (408, 429):
                detail = exc.read().decode("utf-8", "replace")
                raise RuntimeError(f"HTTP {exc.code} for {url}: {detail[:2000]}") from exc
            if attempt < attempts:
                time.sleep(min(30, 2**attempt))
    raise RuntimeError(f"request failed after {attempts} attempts for {url}: {last_error}")


def get_text(url: str) -> tuple[str, bytes, dict]:
    raw, meta = request_bytes(url, headers={"accept": "text/html,application/xhtml+xml"})
    return raw.decode("utf-8", "replace"), raw, meta


def post_json(url: str, payload: dict, referer: str) -> tuple[dict, bytes, dict]:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    raw, meta = request_bytes(
        url,
        method="POST",
        data=body,
        headers={
            "accept": "application/json",
            "content-type": "application/json",
            "origin": ORIGIN,
            "referer": referer,
        },
    )
    meta["request_payload"] = payload
    try:
        return json.loads(raw), raw, meta
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid JSON from {url}: {raw[:1000]!r}") from exc


def recursive_collection_candidates(node: object, path: str = "$") -> list[dict]:
    found: list[dict] = []
    if isinstance(node, dict):
        label_parts = []
        for key in ("label", "slug", "subdomain", "name", "title", "key"):
            value = node.get(key)
            if isinstance(value, (str, int)):
                label_parts.append(str(value))
        label_text = " ".join(label_parts).casefold()
        if "basisset" in label_text or "basis set" in label_text:
            for key in ("id", "collectionId", "collection_id", "networkId", "network_id"):
                value = node.get(key)
                if str(value or "").isdigit():
                    found.append({"id": str(value), "source": "next_data_object", "path": path, "label": label_text})
        for key, value in node.items():
            found.extend(recursive_collection_candidates(value, f"{path}.{key}"))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(recursive_collection_candidates(value, f"{path}[{index}]"))
    return found


def discover_collection_id() -> tuple[str, dict]:
    discovery_dir = OUT / "discovery"
    discovery_dir.mkdir(parents=True, exist_ok=True)
    candidates: list[dict] = []

    for page_name, page_url in (("jobs", JOBS_PAGE), ("companies", COMPANIES_PAGE)):
        html, raw, meta = get_text(page_url)
        (discovery_dir / f"{page_name}.html").write_bytes(raw)
        (discovery_dir / f"{page_name}.meta.json").write_text(
            json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8"
        )

        for pattern_name, pattern in (
            ("api_url", r"api\.getro\.com/api/v2/collections/(\d+)"),
            ("collection_id", r'"collection(?:Id|_id)"\s*:\s*"?(\d+)"?'),
            ("id_before_label", r'"id"\s*:\s*"?(\d+)"?.{0,600}?"label"\s*:\s*"([^"]+)"'),
            ("label_before_id", r'"label"\s*:\s*"([^"]+)".{0,600}?"id"\s*:\s*"?(\d+)"?'),
        ):
            for match in re.finditer(pattern, html, re.IGNORECASE | re.DOTALL):
                groups = match.groups()
                if pattern_name == "id_before_label":
                    candidate_id, label = groups
                    if "basisset" not in label.casefold() and "basis set" not in label.casefold():
                        continue
                elif pattern_name == "label_before_id":
                    label, candidate_id = groups
                    if "basisset" not in label.casefold() and "basis set" not in label.casefold():
                        continue
                else:
                    candidate_id = groups[0]
                    label = ""
                candidates.append(
                    {
                        "id": candidate_id,
                        "source": f"{page_name}:{pattern_name}",
                        "label": label,
                    }
                )

        next_match = re.search(
            r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
            html,
            re.IGNORECASE | re.DOTALL,
        )
        if next_match:
            try:
                next_data = json.loads(next_match.group(1))
                (discovery_dir / f"{page_name}.__NEXT_DATA__.json").write_text(
                    json.dumps(next_data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
                )
                candidates.extend(recursive_collection_candidates(next_data))
            except json.JSONDecodeError:
                pass

    # Fallback only for discovery: a current, pinned open-source Getro source catalog.
    if not candidates:
        catalog_url = (
            "https://raw.githubusercontent.com/wyattowalsh/openopps/"
            "02b3fa78b9d5ac26a66c4fc215a7809337477f14/"
            "src/openopps/providers/sources/getro.py"
        )
        catalog_text, catalog_raw, catalog_meta = get_text(catalog_url)
        (discovery_dir / "openopps-getro-catalog.py").write_bytes(catalog_raw)
        (discovery_dir / "openopps-getro-catalog.meta.json").write_text(
            json.dumps(catalog_meta, indent=2, sort_keys=True), encoding="utf-8"
        )
        match = re.search(
            r'"basisset"\s*:\s*SourceRecord\(.*?raw_metadata=\{"collectionId"\s*:\s*"(\d+)"\}',
            catalog_text,
            re.DOTALL,
        )
        if not match:
            match = re.search(
                r'jobs\.basisset\.com/companies.*?collectionId"\s*:\s*"(\d+)"',
                catalog_text,
                re.DOTALL,
            )
        if match:
            candidates.append({"id": match.group(1), "source": "pinned_openopps_catalog", "label": "basisset"})

    unique_candidates: dict[str, list[dict]] = defaultdict(list)
    for candidate in candidates:
        if candidate.get("id", "").isdigit():
            unique_candidates[candidate["id"]].append(candidate)

    validations: list[dict] = []
    for candidate_id, evidence in sorted(unique_candidates.items()):
        meta_url = f"https://api.getro.com/api/v2/collections/{candidate_id}"
        try:
            raw, response_meta = request_bytes(meta_url, headers={"accept": "application/json"})
            payload = json.loads(raw)
            attributes = ((payload.get("data") or {}).get("attributes") or {}) if isinstance(payload, dict) else {}
            label = clean(first(attributes, "label", "name", "slug", "subdomain"))
            valid = "basisset" in label.casefold() or "basis set" in label.casefold()
            validations.append(
                {
                    "id": candidate_id,
                    "evidence": evidence,
                    "metadata_label": label,
                    "metadata_sha256": digest(raw),
                    "valid": valid,
                }
            )
            (discovery_dir / f"collection-{candidate_id}.json").write_bytes(raw)
            (discovery_dir / f"collection-{candidate_id}.meta.json").write_text(
                json.dumps(response_meta, indent=2, sort_keys=True), encoding="utf-8"
            )
        except Exception as exc:
            validations.append(
                {"id": candidate_id, "evidence": evidence, "metadata_error": repr(exc), "valid": False}
            )

    valid_ids = sorted({item["id"] for item in validations if item.get("valid")})
    if len(valid_ids) == 1:
        selected = valid_ids[0]
    elif len(unique_candidates) == 1:
        # A single page-derived/pinned candidate is accepted if the metadata route is unavailable.
        selected = next(iter(unique_candidates))
    else:
        raise RuntimeError(
            f"could not uniquely discover Basis Set collection id; candidates={dict(unique_candidates)} validations={validations}"
        )

    report = {
        "selected_collection_id": selected,
        "candidate_evidence": dict(unique_candidates),
        "candidate_validations": validations,
    }
    (discovery_dir / "collection-discovery.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    return selected, report


def payload_for(kind: str, page: int) -> dict:
    if kind == "jobs":
        return {"hits_per_page": 100, "page": page}
    return {"hitsPerPage": 100, "page": page, "query": "", "filters": ""}


def page_ids(kind: str, records: list[dict]) -> list[str]:
    if kind == "jobs":
        return [job_id(record) for record in records]
    return [canonical_company_id(record) for record in records]


def duplicate_id_analysis(kind: str, records: list[dict]) -> dict:
    groups: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        key = job_id(record) if kind == "jobs" else canonical_company_id(record)
        if key:
            groups[key].append(record)
    duplicate_ids: dict[str, dict] = {}
    conflicting_ids: list[str] = []
    exact_duplicate_extra_records = 0
    for key, group in groups.items():
        if len(group) <= 1:
            continue
        hashes = sorted({canonical_json_digest(record) for record in group})
        duplicate_ids[key] = {"record_count": len(group), "unique_payload_hashes": hashes}
        if len(hashes) > 1:
            conflicting_ids.append(key)
        else:
            exact_duplicate_extra_records += len(group) - 1
    return {
        "duplicate_ids": duplicate_ids,
        "duplicate_id_count": len(duplicate_ids),
        "conflicting_duplicate_ids": sorted(conflicting_ids),
        "exact_duplicate_extra_record_count": exact_duplicate_extra_records,
    }


def fetch_attempt(kind: str, attempt: int, collection_id: str) -> dict:
    endpoint = f"https://api.getro.com/api/v2/collections/{collection_id}/search/{kind}"
    referer = JOBS_PAGE if kind == "jobs" else COMPANIES_PAGE
    record_key = kind
    raw_dir = OUT / "raw" / kind / f"attempt-{attempt:02d}"
    raw_dir.mkdir(parents=True, exist_ok=True)

    all_records: list[dict] = []
    page_summaries: list[dict] = []
    reported_total: int | None = None
    first_page_ids: list[str] = []
    seen_hashes: set[str] = set()
    terminal: dict | None = None

    for page in range(1000):
        if page:
            time.sleep(REQUEST_INTERVAL_SECONDS)
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
            raise RuntimeError(
                f"{kind}: total changed inside attempt {attempt}: {reported_total} -> {current_total}"
            )

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
        print(
            f"{kind} attempt={attempt} page={page} records={len(records)} "
            f"accumulated={len(all_records)} total={reported_total}"
        )

        if not records:
            terminal = {"type": "empty_page", "page": page}
            break
        if reported_total is not None and len(all_records) >= reported_total:
            terminal = {"type": "reported_total_reached", "page": page, "count": len(all_records)}
            break
    else:
        raise RuntimeError(f"{kind}: max page guard reached")

    time.sleep(REQUEST_INTERVAL_SECONDS)
    check, check_raw, check_meta = post_json(endpoint, payload_for(kind, 0), referer)
    (raw_dir / "page-zero-recheck.json").write_bytes(check_raw)
    (raw_dir / "page-zero-recheck.meta.json").write_text(
        json.dumps(check_meta, indent=2, sort_keys=True), encoding="utf-8"
    )
    check_results = check.get("results") or {}
    check_records = check_results.get(record_key) or []
    total_after = int(check_results.get("count") or 0)
    ids = page_ids(kind, all_records)
    empty_ids = [index for index, key in enumerate(ids) if not key]
    first_page_ids_after = page_ids(kind, check_records)
    duplicate_analysis = duplicate_id_analysis(kind, all_records)

    count_complete = reported_total is not None and len(all_records) == reported_total
    unique_ok = True
    if kind == "companies":
        unique_ok = len(set(ids)) == len(ids)
    stable = (
        count_complete
        and reported_total == total_after
        and not empty_ids
        and not duplicate_analysis["conflicting_duplicate_ids"]
        and unique_ok
        and first_page_ids == first_page_ids_after
    )
    summary = {
        "kind": kind,
        "attempt": attempt,
        "reported_total": reported_total,
        "reported_total_after": total_after,
        "raw_count": len(all_records),
        "unique_id_count": len(set(ids)),
        "empty_id_indexes": empty_ids,
        "first_page_stable": first_page_ids == first_page_ids_after,
        "page_count": len(page_summaries),
        "page_sizes": [page["records"] for page in page_summaries],
        "terminal": terminal,
        "stable": stable,
        **duplicate_analysis,
    }
    (raw_dir / "attempt-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    return {"records": all_records, "summary": summary, "raw_dir": raw_dir}


def stable_capture(kind: str, collection_id: str, max_attempts: int = 2) -> dict:
    attempts: list[dict] = []
    for attempt in range(1, max_attempts + 1):
        result = fetch_attempt(kind, attempt, collection_id)
        attempts.append(result)
        if result["summary"]["stable"]:
            result["all_attempt_summaries"] = [item["summary"] for item in attempts]
            return result
        time.sleep(3)
    result = attempts[-1]
    result["all_attempt_summaries"] = [item["summary"] for item in attempts]
    return result


def unique_jobs(records: list[dict]) -> tuple[list[dict], dict]:
    by_id: dict[str, dict] = {}
    hashes_by_id: dict[str, set[str]] = defaultdict(set)
    empty: list[dict] = []
    for record in records:
        key = job_id(record)
        if not key:
            empty.append(record)
            continue
        hashes_by_id[key].add(canonical_json_digest(record))
        by_id.setdefault(key, record)
    conflicts = sorted(key for key, hashes in hashes_by_id.items() if len(hashes) > 1)
    return list(by_id.values()), {"empty_job_records": empty, "conflicting_job_ids": conflicts}


def build_company_index(companies: list[dict]):
    company_by_id: dict[str, dict] = {}
    stable_alias_index: dict[str, set[str]] = defaultdict(set)
    name_index: dict[str, set[str]] = defaultdict(set)
    for company in companies:
        company_id_value = canonical_company_id(company)
        if not company_id_value or company_id_value == "name:":
            raise RuntimeError("company without canonical identity")
        if company_id_value in company_by_id:
            raise RuntimeError(f"duplicate company canonical identity: {company_id_value}")
        company_by_id[company_id_value] = company
        for alias in identifier_aliases(company):
            stable_alias_index[alias].add(company_id_value)
        alias = name_alias(company)
        if alias:
            name_index[alias].add(company_id_value)
    return company_by_id, stable_alias_index, name_index


def resolve_hiring_companies(jobs: list[dict], companies: list[dict]) -> dict:
    company_by_id, stable_alias_index, name_index = build_company_index(companies)
    deduped_jobs, dedupe_info = unique_jobs(jobs)
    org_groups: dict[str, dict] = {}
    unusable_jobs: list[dict] = []
    for job in deduped_jobs:
        org = job_organization(job)
        org_name = clean(first(org, "name", "title"))
        identity = organization_identity(org)
        if not org_name or not identity:
            unusable_jobs.append({"job_id": job_id(job), "organization": org})
            continue
        group = org_groups.setdefault(identity, {"organization": org, "job_ids": []})
        group["job_ids"].append(job_id(job))

    resolved_by_company: dict[str, dict] = {}
    unresolved: list[dict] = []
    for identity, group in org_groups.items():
        org = group["organization"]
        stable_candidates: set[str] = set()
        matched_stable_aliases: dict[str, list[str]] = {}
        for alias in identifier_aliases(org):
            matches = stable_alias_index.get(alias, set())
            if matches:
                matched_stable_aliases[alias] = sorted(matches)
                stable_candidates.update(matches)

        selected_candidates = stable_candidates
        match_method = "stable_identifier"
        matched_name_alias = ""
        if not stable_candidates:
            matched_name_alias = name_alias(org)
            selected_candidates = name_index.get(matched_name_alias, set()) if matched_name_alias else set()
            match_method = "exact_normalized_name"

        if len(selected_candidates) != 1:
            unresolved.append(
                {
                    "organization_identity": identity,
                    "name": clean(first(org, "name", "title")),
                    "job_count": len(group["job_ids"]),
                    "job_ids": group["job_ids"],
                    "matched_stable_aliases": matched_stable_aliases,
                    "matched_name_alias": matched_name_alias,
                    "candidate_company_ids": sorted(selected_candidates),
                    "raw_organization": org,
                }
            )
            continue

        company_id_value = next(iter(selected_candidates))
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
                "active_job_count_from_unique_jobs": 0,
                "active_job_count_from_directory": first(
                    company, "active_jobs_count", "jobs_count", "job_count"
                ),
                "organization_identities": [],
                "resolution_methods": [],
            }
            resolved_by_company[company_id_value] = row
        row["active_job_count_from_unique_jobs"] += len(group["job_ids"])
        row["organization_identities"].append(identity)
        row["resolution_methods"].append(match_method)

    rows = sorted(resolved_by_company.values(), key=lambda row: (row["name"].casefold(), row["source_id"]))
    return {
        "rows": rows,
        "unresolved": unresolved,
        "unusable_jobs": unusable_jobs,
        "organization_group_count": len(org_groups),
        "company_by_id": company_by_id,
        "dedupe_info": dedupe_info,
        "unique_job_count": len(deduped_jobs),
    }


def audit_reconstruct(jobs: list[dict], companies: list[dict]) -> dict:
    # Independent implementation: builds its own indexes and does not call the primary resolver.
    company_records: dict[str, dict] = {}
    stable: dict[str, set[str]] = defaultdict(set)
    names: dict[str, set[str]] = defaultdict(set)
    for company in companies:
        cid = clean(first(company, "id", "objectID", "object_id", "slug"))
        if not cid:
            cid = "name:" + normalized_name(first(company, "name", "title"))
        if cid in company_records:
            raise RuntimeError(f"audit duplicate company id {cid}")
        company_records[cid] = company
        for key in ("id", "objectID", "object_id", "slug"):
            value = clean(company.get(key))
            if value:
                stable["raw:" + value].add(cid)
        n = normalized_name(first(company, "name", "title"))
        if n:
            names[n].add(cid)

    unique_job_records: dict[str, dict] = {}
    conflicting_job_ids: list[str] = []
    job_hashes: dict[str, set[str]] = defaultdict(set)
    for job in jobs:
        jid = clean(first(job, "id", "objectID", "object_id", "slug", "url"))
        if not jid:
            continue
        job_hashes[jid].add(canonical_json_digest(job))
        unique_job_records.setdefault(jid, job)
    conflicting_job_ids = sorted(key for key, hashes in job_hashes.items() if len(hashes) > 1)

    orgs: dict[str, dict] = {}
    for jid, job in unique_job_records.items():
        org = job.get("organization") or job.get("company") or {}
        if not isinstance(org, dict):
            org = {"name": org}
        stable_keys = []
        for key in ("id", "objectID", "object_id", "slug"):
            value = clean(org.get(key))
            if value:
                stable_keys.append("raw:" + value)
        n = normalized_name(first(org, "name", "title"))
        identity = stable_keys[0] if stable_keys else ("name:" + n if n else "")
        if not identity or not n:
            continue
        group = orgs.setdefault(identity, {"org": org, "job_ids": [], "stable_keys": stable_keys, "name": n})
        group["job_ids"].append(jid)

    rows: dict[str, dict] = {}
    unresolved: list[dict] = []
    for identity, group in orgs.items():
        candidates: set[str] = set()
        for alias in group["stable_keys"]:
            candidates.update(stable.get(alias, set()))
        if not candidates:
            candidates = set(names.get(group["name"], set()))
        if len(candidates) != 1:
            unresolved.append({"identity": identity, "candidates": sorted(candidates), "name": group["name"]})
            continue
        cid = next(iter(candidates))
        company = company_records[cid]
        website = normalize_url(first(company, "domain", "website", "website_url"))
        row = rows.setdefault(
            cid,
            {
                "source_id": cid,
                "name": clean(first(company, "name", "title")),
                "website": website,
                "active_job_count_from_unique_jobs": 0,
            },
        )
        row["active_job_count_from_unique_jobs"] += len(group["job_ids"])

    return {
        "rows": sorted(rows.values(), key=lambda row: (row["name"].casefold(), row["source_id"])),
        "unresolved": unresolved,
        "unique_job_count": len(unique_job_records),
        "conflicting_job_ids": conflicting_job_ids,
        "organization_group_count": len(orgs),
    }


def independent_audit(
    selected_jobs_dir: Path,
    selected_companies_dir: Path,
    normalized_rows: list[dict],
    txt_path: Path,
    json_path: Path,
    jobs_summary: dict,
    companies_summary: dict,
) -> dict:
    raw_hash_errors: list[str] = []
    audit_jobs: list[dict] = []
    audit_companies: list[dict] = []
    for kind, raw_dir, record_key, destination in (
        ("jobs", selected_jobs_dir, "jobs", audit_jobs),
        ("companies", selected_companies_dir, "companies", audit_companies),
    ):
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
    json_rows = json.loads(json_path.read_text(encoding="utf-8"))
    result = {
        "raw_hash_errors": raw_hash_errors,
        "raw_job_count": len(audit_jobs),
        "raw_company_count": len(audit_companies),
        "jobs_reported_total_matches_raw": jobs_summary.get("reported_total") == len(audit_jobs),
        "companies_reported_total_matches_raw": companies_summary.get("reported_total") == len(audit_companies),
        "reconstructed_unique_job_count": reconstructed["unique_job_count"],
        "reconstructed_company_count": len(audit_rows),
        "reconstructed_organization_group_count": reconstructed["organization_group_count"],
        "reconstructed_unresolved_count": len(reconstructed["unresolved"]),
        "reconstructed_conflicting_job_ids": reconstructed["conflicting_job_ids"],
        "source_id_maps_equal": primary_map == audit_map,
        "txt_lines_equal": txt_lines == expected_lines,
        "json_rows_equal": json_rows == normalized_rows,
        "txt_line_count": len(txt_lines),
    }
    result["status"] = (
        "PASS"
        if not raw_hash_errors
        and result["jobs_reported_total_matches_raw"]
        and result["companies_reported_total_matches_raw"]
        and not reconstructed["unresolved"]
        and not reconstructed["conflicting_job_ids"]
        and primary_map == audit_map
        and txt_lines == expected_lines
        and json_rows == normalized_rows
        else "FAIL"
    )
    return result


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    collection_id, discovery_report = discover_collection_id()
    jobs_capture = stable_capture("jobs", collection_id)
    companies_capture = stable_capture("companies", collection_id)
    jobs = jobs_capture["records"]
    companies = companies_capture["records"]

    resolution = resolve_hiring_companies(jobs, companies)
    rows = resolution["rows"]
    unresolved = resolution["unresolved"]
    unusable_jobs = resolution["unusable_jobs"]

    missing_websites = [row for row in rows if not row["website"]]
    invalid_websites: list[dict] = []
    blocked_hosts = {"jobs.basisset.com", "api.getro.com", "getro.com"}
    for row in rows:
        website = row["website"]
        if not website:
            continue
        parsed = urlparse(website)
        host = parsed.netloc.casefold().removeprefix("www.")
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or host in blocked_hosts:
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

    directory_positive = []
    for company in companies:
        hint = first(company, "active_jobs_count", "jobs_count", "job_count")
        try:
            hint_int = int(hint)
        except (TypeError, ValueError):
            continue
        if hint_int > 0:
            directory_positive.append(
                {
                    "source_id": canonical_company_id(company),
                    "name": clean(first(company, "name", "title")),
                    "active_jobs_count": hint_int,
                }
            )
    positive_ids = {row["source_id"] for row in directory_positive}
    resolved_ids = {row["source_id"] for row in rows}
    directory_positive_not_in_job_results = sorted(positive_ids - resolved_ids)
    job_results_not_directory_positive = sorted(resolved_ids - positive_ids)

    normalized_dir = OUT / "normalized"
    normalized_dir.mkdir()
    txt_path = normalized_dir / "basisset-hiring-companies.txt"
    json_path = normalized_dir / "basisset-hiring-companies.json"
    txt_path.write_text(
        "\n".join(f'{row["name"]} + {row["website"]}' for row in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )
    json_path.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )

    for filename, payload in (
        ("unresolved-organizations.json", unresolved),
        ("unusable-jobs.json", unusable_jobs),
        ("missing-websites.json", missing_websites),
        ("duplicate-names.json", duplicate_names),
        ("duplicate-domains.json", duplicate_domains),
        ("directory-positive-companies.json", directory_positive),
        ("directory-positive-not-in-job-results.json", directory_positive_not_in_job_results),
        ("job-results-not-directory-positive.json", job_results_not_directory_positive),
    ):
        (OUT / filename).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )

    audit = independent_audit(
        jobs_capture["raw_dir"],
        companies_capture["raw_dir"],
        rows,
        txt_path,
        json_path,
        jobs_capture["summary"],
        companies_capture["summary"],
    )
    (OUT / "independent-audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )

    hard_errors: list[str] = []
    review_items: list[str] = []
    if not jobs_capture["summary"]["stable"]:
        hard_errors.append("jobs snapshot was not stable after two attempts")
    if not companies_capture["summary"]["stable"]:
        hard_errors.append("companies snapshot was not stable after two attempts")
    if unresolved:
        hard_errors.append("unresolved hiring organizations")
    if unusable_jobs:
        hard_errors.append("jobs without usable organization identity")
    if missing_websites:
        hard_errors.append("missing company websites")
    if invalid_websites:
        hard_errors.append("invalid company websites")
    if resolution["dedupe_info"]["conflicting_job_ids"]:
        hard_errors.append("conflicting duplicate job IDs")
    if len(rows) != len({row["source_id"] for row in rows}):
        hard_errors.append("duplicate normalized source IDs")
    if audit["status"] != "PASS":
        hard_errors.append("independent audit failed")
    if duplicate_names:
        review_items.append("duplicate normalized company names")
    if duplicate_domains:
        review_items.append("multiple company IDs share a website domain")

    status = "PASS"
    if hard_errors:
        status = "FAIL"
    elif review_items:
        status = "NEEDS_REVIEW"

    validation = {
        "status": status,
        "source": JOBS_PAGE,
        "collection_id": collection_id,
        "collection_discovery": discovery_report,
        "jobs_capture": jobs_capture["summary"],
        "jobs_attempts": jobs_capture["all_attempt_summaries"],
        "companies_capture": companies_capture["summary"],
        "companies_attempts": companies_capture["all_attempt_summaries"],
        "raw_job_record_count": len(jobs),
        "unique_job_count": resolution["unique_job_count"],
        "job_organization_group_count": resolution["organization_group_count"],
        "resolved_hiring_company_count": len(rows),
        "output_line_count": len(rows),
        "company_directory_count": len(companies),
        "unresolved_organization_count": len(unresolved),
        "jobs_without_usable_organization_count": len(unusable_jobs),
        "missing_website_count": len(missing_websites),
        "invalid_website_count": len(invalid_websites),
        "duplicate_name_group_count": len(duplicate_names),
        "duplicate_domain_group_count": len(duplicate_domains),
        "directory_positive_company_count": len(directory_positive),
        "directory_positive_not_in_job_results_count": len(directory_positive_not_in_job_results),
        "job_results_not_directory_positive_count": len(job_results_not_directory_positive),
        "duplicate_names": duplicate_names,
        "duplicate_domains": duplicate_domains,
        "invalid_websites": invalid_websites,
        "hard_errors": hard_errors,
        "review_items": review_items,
        "independent_audit": audit,
    }
    (OUT / "validation.json").write_text(
        json.dumps(validation, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )

    manifest = {
        "generated_at_epoch": time.time(),
        "status": status,
        "source": JOBS_PAGE,
        "collection_id": collection_id,
        "files": {},
    }
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
                "status": status,
                "collection_id": collection_id,
                "jobs_reported_total": jobs_capture["summary"]["reported_total"],
                "jobs_raw_count": jobs_capture["summary"]["raw_count"],
                "jobs_unique_id_count": jobs_capture["summary"]["unique_id_count"],
                "jobs_exact_duplicate_extra_records": jobs_capture["summary"][
                    "exact_duplicate_extra_record_count"
                ],
                "companies_reported_total": companies_capture["summary"]["reported_total"],
                "companies_raw_count": companies_capture["summary"]["raw_count"],
                "hiring_organization_count": resolution["organization_group_count"],
                "resolved_hiring_company_count": len(rows),
                "output_line_count": len(rows),
                "unresolved_organization_count": len(unresolved),
                "missing_website_count": len(missing_websites),
                "duplicate_name_group_count": len(duplicate_names),
                "duplicate_domain_group_count": len(duplicate_domains),
                "independent_audit": audit["status"],
                "hard_errors": hard_errors,
                "review_items": review_items,
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
        # Preserve diagnostics by allowing the artifact upload step to run.
        sys.exit(0)
