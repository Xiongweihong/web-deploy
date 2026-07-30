from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

import requests

TARGET = "https://www.ycombinator.com/companies"
INDEX_NAME = "YCCompany_By_Launch_Date_production"
APPLICATION_ID = "45BWZJ1SGC"
ALGOLIA_URL = f"https://{APPLICATION_ID.lower()}-dsn.algolia.net/1/indexes/*/queries"
OUT = Path("artifact")
RAW = OUT / "raw"
USER_AGENT = "Mozilla/5.0 (compatible; yc-directory-evidence-audit/1.0)"
EXPECTED_USER_COUNT = 6107


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def save_bytes(path: Path, raw: bytes, metadata: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    write_json(
        path.with_suffix(path.suffix + ".meta.json"),
        {**metadata, "bytes": len(raw), "sha256": sha256_bytes(raw)},
    )


def request(method: str, url: str, **kwargs) -> requests.Response:
    last_error: Exception | None = None
    for attempt in range(1, 6):
        try:
            response = requests.request(
                method,
                url,
                timeout=120,
                headers={"User-Agent": USER_AGENT, **kwargs.pop("headers", {})},
                **kwargs,
            )
            response.raise_for_status()
            return response
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < 5:
                time.sleep(min(16, 2**attempt))
    raise RuntimeError(f"Request failed: {method} {url}: {last_error}")


def get_algolia_key(html: str) -> str:
    match = re.search(r"window\.AlgoliaOpts\s*=\s*({[^<]+})", html)
    if not match:
        raise RuntimeError("Could not locate window.AlgoliaOpts in YC companies page")
    options = json.loads(match.group(1))
    if options.get("app") != APPLICATION_ID or not options.get("key"):
        raise RuntimeError(f"Unexpected YC Algolia options: {options}")
    return str(options["key"])


def algolia_request(api_key: str, params_string: str) -> tuple[dict, bytes]:
    query = {
        "x-algolia-agent": "Algolia for JavaScript (3.35.1); Browser; JS Helper (3.16.1)",
        "x-algolia-application-id": APPLICATION_ID,
        "x-algolia-api-key": api_key,
    }
    response = request(
        "POST",
        ALGOLIA_URL,
        params=query,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        data=json.dumps(
            {"requests": [{"indexName": INDEX_NAME, "params": params_string}]},
            separators=(",", ":"),
        ).encode("utf-8"),
    )
    return response.json(), response.content


def normalize_website(value: object) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return ""
    if text.startswith("//"):
        text = "https:" + text
    elif "://" not in text:
        text = "https://" + text
    elif text.startswith("http://"):
        text = "https://" + text[7:]
    parsed = urlparse(text)
    host = parsed.netloc.casefold().removeprefix("www.")
    if not host or "." not in host or " " in host:
        return ""
    return f"https://{host}"


def company_identity_map(companies: list[dict]) -> list[tuple]:
    return sorted(
        (
            int(company["id"]),
            str(company.get("name") or ""),
            str(company.get("slug") or ""),
            str(company.get("website") or ""),
            str(company.get("batch") or ""),
            str(company.get("status") or ""),
        )
        for company in companies
    )


def fetch_snapshot(snapshot: int) -> dict:
    snapshot_dir = RAW / f"snapshot-{snapshot:02d}"
    page_response = request("GET", TARGET, headers={"Accept": "text/html,application/xhtml+xml"})
    page_raw = page_response.content
    save_bytes(
        snapshot_dir / "companies-page.html",
        page_raw,
        {
            "requested_url": TARGET,
            "final_url": page_response.url,
            "status": page_response.status_code,
            "content_type": page_response.headers.get("content-type"),
            "date": page_response.headers.get("date"),
            "etag": page_response.headers.get("etag"),
            "fetched_at_epoch": time.time(),
        },
    )
    api_key = get_algolia_key(page_response.text)

    facet_params = (
        "facets=%5B%22batch%22%5D&hitsPerPage=0&maxValuesPerFacet=1000"
        "&query=&tagFilters="
    )
    facet_payload, facet_raw = algolia_request(api_key, facet_params)
    save_bytes(
        snapshot_dir / "algolia-facets.json",
        facet_raw,
        {
            "index_name": INDEX_NAME,
            "request_params": facet_params,
            "fetched_at_epoch": time.time(),
        },
    )
    facet_result = (facet_payload.get("results") or [{}])[0]
    reported_total = int(facet_result.get("nbHits") or 0)
    batches = (facet_result.get("facets") or {}).get("batch") or {}
    facet_sum = sum(int(count) for count in batches.values())
    if not batches:
        raise RuntimeError("Algolia facets response has no batch facet")
    if facet_sum != reported_total:
        raise RuntimeError(
            f"Batch facet sum {facet_sum} does not equal Algolia nbHits {reported_total}"
        )

    all_companies: list[dict] = []
    page_summaries: list[dict] = []
    for batch, expected_count_raw in sorted(batches.items(), key=lambda item: item[0]):
        expected_count = int(expected_count_raw)
        fetched = 0
        page = 0
        while fetched < expected_count:
            params_string = (
                "facets=%5B%22batch%22%5D&hitsPerPage=1000&maxValuesPerFacet=1000"
                f"&query=&tagFilters=&facetFilters=batch%3A{quote(str(batch), safe='')}"
                f"&page={page}"
            )
            payload, raw = algolia_request(api_key, params_string)
            result = (payload.get("results") or [{}])[0]
            hits = result.get("hits") or []
            batch_total = int(result.get("nbHits") or 0)
            path = snapshot_dir / "batches" / f"{batch.replace('/', '_')}-page-{page:03d}.json"
            save_bytes(
                path,
                raw,
                {
                    "batch": batch,
                    "page": page,
                    "expected_batch_count": expected_count,
                    "reported_batch_count": batch_total,
                    "request_params": params_string,
                    "fetched_at_epoch": time.time(),
                },
            )
            if batch_total != expected_count:
                raise RuntimeError(
                    f"Batch {batch} expected {expected_count}, response reports {batch_total}"
                )
            if not hits:
                raise RuntimeError(
                    f"Batch {batch} returned an empty page before expected count was reached"
                )
            all_companies.extend(hits)
            fetched += len(hits)
            page_summaries.append(
                {
                    "batch": batch,
                    "page": page,
                    "record_count": len(hits),
                    "expected_batch_count": expected_count,
                    "running_batch_count": fetched,
                    "sha256": sha256_bytes(raw),
                }
            )
            page += 1
        if fetched != expected_count:
            raise RuntimeError(
                f"Batch {batch} fetched {fetched}, expected {expected_count}"
            )

    ids = [int(company["id"]) for company in all_companies]
    if len(all_companies) != reported_total:
        raise RuntimeError(
            f"Fetched {len(all_companies)} companies, Algolia reports {reported_total}"
        )
    duplicate_ids = {key: value for key, value in Counter(ids).items() if value > 1}
    if duplicate_ids:
        raise RuntimeError(f"Duplicate company IDs in source: {duplicate_ids}")

    all_companies.sort(key=lambda company: int(company["id"]))
    write_json(snapshot_dir / "companies-all-raw.json", all_companies)
    write_json(
        snapshot_dir / "snapshot-summary.json",
        {
            "snapshot": snapshot,
            "reported_total": reported_total,
            "facet_sum": facet_sum,
            "batch_count": len(batches),
            "batch_facets": dict(sorted(batches.items())),
            "fetched_count": len(all_companies),
            "unique_id_count": len(set(ids)),
            "page_summaries": page_summaries,
            "identity_map_sha256": sha256_bytes(
                json.dumps(company_identity_map(all_companies), separators=(",", ":")).encode()
            ),
        },
    )
    return {
        "companies": all_companies,
        "reported_total": reported_total,
        "facet_sum": facet_sum,
        "batches": dict(sorted(batches.items())),
        "page_summaries": page_summaries,
        "page_html_sha256": sha256_bytes(page_raw),
    }


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    RAW.mkdir(parents=True)

    snapshot_1 = fetch_snapshot(1)
    time.sleep(4)
    snapshot_2 = fetch_snapshot(2)

    map_1 = company_identity_map(snapshot_1["companies"])
    map_2 = company_identity_map(snapshot_2["companies"])
    stable = map_1 == map_2
    if not stable:
        ids_1 = {row[0] for row in map_1}
        ids_2 = {row[0] for row in map_2}
        write_json(
            OUT / "unstable-snapshot-diff.json",
            {
                "added_ids": sorted(ids_2 - ids_1),
                "removed_ids": sorted(ids_1 - ids_2),
            },
        )
        raise RuntimeError("YC company source changed between the two complete snapshots")

    companies = snapshot_2["companies"]
    rows: list[dict] = []
    for position, company in enumerate(companies, start=1):
        source_website = str(company.get("website") or "").strip()
        website = normalize_website(source_website)
        rows.append(
            {
                "source_position": position,
                "source_id": int(company["id"]),
                "name": str(company.get("name") or "").strip(),
                "slug": str(company.get("slug") or "").strip(),
                "yc_profile": f"https://www.ycombinator.com/companies/{company.get('slug')}",
                "source_website": source_website,
                "website": website,
                "display_website": website or "未公开（YC未提供有效公司官网）",
                "website_domain": urlparse(website).netloc if website else "",
                "batch": str(company.get("batch") or ""),
                "status": str(company.get("status") or ""),
                "industry": str(company.get("industry") or ""),
                "subindustry": str(company.get("subindustry") or ""),
                "launched_at": company.get("launched_at"),
                "team_size": company.get("team_size"),
                "is_hiring": bool(company.get("isHiring")),
            }
        )

    ids = [row["source_id"] for row in rows]
    slugs = [row["slug"] for row in rows]
    names = [row["name"].casefold() for row in rows]
    domains = [row["website_domain"] for row in rows if row["website_domain"]]
    duplicate_names = {
        key: [
            {
                "source_id": row["source_id"],
                "name": row["name"],
                "slug": row["slug"],
                "website": row["website"],
            }
            for row in rows
            if row["name"].casefold() == key
        ]
        for key, count in Counter(names).items()
        if count > 1
    }
    duplicate_domains = {
        key: value for key, value in Counter(domains).items() if value > 1
    }
    missing_websites = [
        {
            "source_id": row["source_id"],
            "name": row["name"],
            "slug": row["slug"],
            "source_website": row["source_website"],
            "yc_profile": row["yc_profile"],
        }
        for row in rows
        if not row["website"]
    ]

    txt_lines = [f"{row['name']} + {row['display_website']}" for row in rows]
    txt_path = OUT / f"ycombinator_{len(rows)}_companies_verified.txt"
    md_path = OUT / f"ycombinator_{len(rows)}_companies_verified.md"
    json_path = OUT / f"ycombinator_{len(rows)}_companies_verified.json"
    validation_path = OUT / f"ycombinator_{len(rows)}_companies_validation.json"
    audit_path = OUT / f"ycombinator_{len(rows)}_companies_independent_audit.json"
    comparison_path = OUT / f"ycombinator_{len(rows)}_companies_snapshot_comparison.json"
    duplicates_path = OUT / f"ycombinator_{len(rows)}_companies_duplicate_names.json"
    missing_path = OUT / f"ycombinator_{len(rows)}_companies_missing_websites.json"
    contract_path = OUT / f"ycombinator_{len(rows)}_companies_source_contract.json"
    manifest_path = OUT / f"ycombinator_{len(rows)}_companies_manifest.json"

    txt_path.write_text("\n".join(txt_lines) + "\n", encoding="utf-8")
    md_path.write_text(
        f"# Y Combinator Startup Directory — {len(rows):,} companies\n\n"
        + "\n".join(txt_lines)
        + "\n",
        encoding="utf-8",
    )
    write_json(
        json_path,
        {
            "status": "PASS_WITH_SOURCE_UNDISCLOSED_WEBSITES"
            if missing_websites
            else "PASS",
            "source_page": TARGET,
            "source_index": INDEX_NAME,
            "application_id": APPLICATION_ID,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "source_record_count": len(rows),
            "companies": rows,
        },
    )
    write_json(duplicates_path, {"count": len(duplicate_names), "groups": duplicate_names})
    write_json(
        missing_path,
        {
            "count": len(missing_websites),
            "policy": "Retain every YC company record; do not guess a domain when the official YC index has no valid website.",
            "companies": missing_websites,
        },
    )

    comparison = {
        "status": "PASS",
        "snapshot_1_count": snapshot_1["reported_total"],
        "snapshot_2_count": snapshot_2["reported_total"],
        "snapshot_identity_maps_equal": stable,
        "batch_facets_equal": snapshot_1["batches"] == snapshot_2["batches"],
        "snapshot_1_page_html_sha256": snapshot_1["page_html_sha256"],
        "snapshot_2_page_html_sha256": snapshot_2["page_html_sha256"],
        "raw_page_html_hashes_equal": snapshot_1["page_html_sha256"]
        == snapshot_2["page_html_sha256"],
        "note": "Raw page HTML may differ due to dynamic page serialization; the full stable-ID company maps are equal.",
    }
    write_json(comparison_path, comparison)

    hard_checks = {
        "algolia_reported_equals_fetched": snapshot_2["reported_total"] == len(rows),
        "batch_facet_sum_equals_reported": snapshot_2["facet_sum"] == len(rows),
        "two_complete_snapshots_equal": stable,
        "unique_source_ids_equal_count": len(set(ids)) == len(rows),
        "unique_slugs_equal_count": len(set(slugs)) == len(rows),
        "all_names_nonempty": all(row["name"] for row in rows),
        "all_slugs_nonempty": all(row["slug"] for row in rows),
        "all_valid_resolved_websites_https": all(
            not row["website"]
            or (
                urlparse(row["website"]).scheme == "https"
                and bool(urlparse(row["website"]).netloc)
            )
            for row in rows
        ),
        "output_line_count_equals_source": len(txt_lines) == len(rows),
    }
    status = "PASS_WITH_SOURCE_UNDISCLOSED_WEBSITES" if missing_websites else "PASS"
    if not all(hard_checks.values()):
        status = "FAIL"
    validation = {
        "status": status,
        "source_page": TARGET,
        "source_index": INDEX_NAME,
        "user_expected_count": EXPECTED_USER_COUNT,
        "live_source_count": len(rows),
        "difference_from_user_expected_count": len(rows) - EXPECTED_USER_COUNT,
        "batch_count": len(snapshot_2["batches"]),
        "batch_facet_sum": snapshot_2["facet_sum"],
        "unique_source_id_count": len(set(ids)),
        "unique_slug_count": len(set(slugs)),
        "unique_display_name_count": len(set(names)),
        "duplicate_display_name_group_count": len(duplicate_names),
        "source_valid_website_count": len(rows) - len(missing_websites),
        "source_missing_or_invalid_website_count": len(missing_websites),
        "unique_resolved_domain_count": len(set(domains)),
        "duplicate_resolved_domain_group_count": len(duplicate_domains),
        "output_line_count": len(txt_lines),
        "snapshot_comparison": comparison,
        "hard_checks": hard_checks,
        "notes": [
            "The authoritative identity key is the YC numeric company ID, not the display name or website domain.",
            "Companies with no valid website in the official YC Algolia index are retained with an explicit undisclosed marker.",
            "Website reachability and domain ownership were not re-audited for every company; this validates the official YC index mapping at the timestamped snapshot.",
        ],
    }
    write_json(validation_path, validation)
    if status == "FAIL":
        raise RuntimeError(f"Validation failed: {hard_checks}")

    write_json(
        contract_path,
        {
            "source_page": TARGET,
            "source_provider": "Y Combinator Algolia search index",
            "algolia_application_id": APPLICATION_ID,
            "algolia_index_name": INDEX_NAME,
            "api_key_discovery": "window.AlgoliaOpts on the official YC companies page",
            "completeness_strategy": "Fetch the complete batch facet, then exhaust every batch independently at 1,000 hits per page.",
            "identity_key": "company.id",
            "display_name": "company.name",
            "company_website": "company.website",
            "missing_website_policy": "Retain the record and mark the company website as undisclosed; never synthesize a domain from the name.",
        },
    )

    # Independent reconstruction from exact raw snapshot-2 records.
    audit_source = json.loads((RAW / "snapshot-02" / "companies-all-raw.json").read_text(encoding="utf-8"))
    audit_source.sort(key=lambda company: int(company["id"]))
    audit_lines = []
    audit_ids = []
    for company in audit_source:
        name = str(company.get("name") or "").strip()
        website = normalize_website(company.get("website"))
        audit_lines.append(f"{name} + {website or '未公开（YC未提供有效公司官网）'}")
        audit_ids.append(int(company["id"]))
    actual_lines = [line for line in txt_path.read_text(encoding="utf-8").splitlines() if line]
    audit_checks = {
        "raw_snapshot_2_count_equals_live_count": len(audit_source) == len(rows),
        "raw_snapshot_2_ids_unique": len(set(audit_ids)) == len(audit_ids),
        "independent_output_equals_final_txt": audit_lines == actual_lines,
        "independent_line_count_equals_source": len(audit_lines) == len(rows),
    }
    audit = {
        "status": "PASS" if all(audit_checks.values()) else "FAIL",
        "method": "Re-read the exact raw snapshot-2 company array, sort by stable numeric ID, normalize only the official website field, and regenerate every output line without using the normalized JSON.",
        "checks": audit_checks,
        "reconstructed_count": len(audit_source),
        "txt_line_count": len(actual_lines),
    }
    write_json(audit_path, audit)
    if audit["status"] != "PASS":
        raise RuntimeError(f"Independent audit failed: {audit_checks}")

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "source_page": TARGET,
        "files": {},
    }
    for path in sorted(OUT.iterdir()):
        if path.is_file() and path != manifest_path:
            manifest["files"][path.name] = {
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
    write_json(manifest_path, manifest)

    # Add the raw source and final outputs to one replayable ZIP inside artifact/.
    evidence_root = OUT / f"ycombinator_{len(rows)}_companies_evidence"
    evidence_root.mkdir(parents=True, exist_ok=True)
    for path in sorted(OUT.iterdir()):
        if path.is_file():
            shutil.copy2(path, evidence_root / path.name)
    shutil.copytree(RAW, evidence_root / "raw", dirs_exist_ok=True)
    (evidence_root / "README.txt").write_text(
        f"Y Combinator Startup Directory evidence\nLive source count: {len(rows)}\n"
        f"User expected count: {EXPECTED_USER_COUNT}\n"
        "Source: official YC Algolia index, exhausted by batch facets.\n",
        encoding="utf-8",
    )
    evidence_zip = OUT / f"ycombinator_{len(rows)}_companies_evidence.zip"
    with zipfile.ZipFile(evidence_zip, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(evidence_root.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(evidence_root.parent))

    result = {
        "status": status,
        "live_source_count": len(rows),
        "user_expected_count": EXPECTED_USER_COUNT,
        "difference": len(rows) - EXPECTED_USER_COUNT,
        "unique_source_ids": len(set(ids)),
        "unique_slugs": len(set(slugs)),
        "unique_names": len(set(names)),
        "duplicate_name_groups": len(duplicate_names),
        "valid_website_count": len(rows) - len(missing_websites),
        "missing_website_count": len(missing_websites),
        "txt_path": str(txt_path),
        "txt_sha256": sha256_file(txt_path),
        "md_path": str(md_path),
        "json_path": str(json_path),
        "validation_path": str(validation_path),
        "audit_path": str(audit_path),
        "comparison_path": str(comparison_path),
        "duplicates_path": str(duplicates_path),
        "missing_path": str(missing_path),
        "contract_path": str(contract_path),
        "manifest_path": str(manifest_path),
        "evidence_zip": str(evidence_zip),
        "evidence_zip_sha256": sha256_file(evidence_zip),
    }
    print("YC_EXTRACTION_RESULT_START")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("YC_EXTRACTION_RESULT_END")


if __name__ == "__main__":
    main()
