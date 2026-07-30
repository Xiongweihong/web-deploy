from __future__ import annotations

import collections
import concurrent.futures
import gzip
import hashlib
import html as html_lib
import json
import math
import re
import shutil
import time
import zipfile
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

LISTING_URL = "https://sequoiacap.com/our-companies/"
REST_URL = "https://sequoiacap.com/wp-json/wp/v2/company"
OUT = Path("artifact")
RAW = OUT / "raw"
UA = "Mozilla/5.0 (compatible; SequoiaPortfolioAudit/2.0; public-source-validation)"
USER_CLAIMED_COUNT = 6107
MAX_WORKERS = 16

SOCIAL_OR_NON_COMPANY_HOSTS = {
    "twitter.com",
    "www.twitter.com",
    "x.com",
    "www.x.com",
    "linkedin.com",
    "www.linkedin.com",
    "instagram.com",
    "www.instagram.com",
    "facebook.com",
    "www.facebook.com",
    "youtube.com",
    "www.youtube.com",
    "tiktok.com",
    "www.tiktok.com",
    "threads.net",
    "www.threads.net",
    "join.a8c.com",
    "wpvip.com",
    "www.wpvip.com",
}


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", html_lib.unescape(value or "")).strip()


def canonical_host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".")
    except Exception:
        return ""


def is_sequoia_host(host: str) -> bool:
    return host == "sequoiacap.com" or host.endswith(".sequoiacap.com")


def normalize_website(url: str) -> str:
    value = html_lib.unescape((url or "").strip())
    if not value:
        return ""
    if value.startswith("//"):
        value = "https:" + value
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return ""
    path = parts.path or ""
    if path == "/":
        path = ""
    return urlunsplit((parts.scheme.lower(), parts.netloc, path, parts.query, ""))


def request_get(url: str, *, params: dict | None = None, accept: str = "text/html,application/json;q=0.9,*/*;q=0.8", timeout: int = 120) -> requests.Response:
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            response = requests.get(
                url,
                params=params,
                headers={"User-Agent": UA, "Accept": accept},
                timeout=timeout,
            )
            if response.status_code >= 500 and attempt < 3:
                time.sleep(attempt * 0.8)
                continue
            return response
        except Exception as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(attempt * 0.8)
    assert last_error is not None
    raise last_error


def save_raw(path: Path, raw: bytes, meta: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    meta = dict(meta)
    meta.update({"bytes": len(raw), "sha256": sha256_bytes(raw)})
    write_json(path.with_suffix(path.suffix + ".meta.json"), meta)


def parse_facetwp(html_text: str) -> dict:
    match = re.search(r"window\.FWP_JSON\s*=\s*({.*?});\s*window\.FWP_HTTP", html_text, re.DOTALL)
    if not match:
        raise RuntimeError("Could not locate window.FWP_JSON on Sequoia listing page")
    payload = json.loads(match.group(1))
    pager = payload.get("preload_data", {}).get("settings", {}).get("pager", {})
    return {
        "page": pager.get("page"),
        "per_page": pager.get("per_page"),
        "total_rows": pager.get("total_rows"),
        "total_pages": pager.get("total_pages"),
        "num_choices": payload.get("preload_data", {}).get("settings", {}).get("num_choices", {}),
        "facets": payload.get("preload_data", {}).get("facets", {}),
    }


def fetch_listing_snapshots() -> tuple[list[dict], list[dict]]:
    snapshots: list[dict] = []
    parsed: list[dict] = []
    for index in (1, 2):
        response = request_get(LISTING_URL)
        response.raise_for_status()
        raw = response.content
        path = RAW / "listing" / f"snapshot-{index}.html"
        save_raw(
            path,
            raw,
            {
                "requested_url": LISTING_URL,
                "final_url": response.url,
                "status": response.status_code,
                "headers": dict(response.headers),
                "fetched_at_epoch": time.time(),
            },
        )
        facet = parse_facetwp(response.text)
        soup = BeautifulSoup(raw, "lxml")
        initial_rows = len(soup.select(".company-listing__list"))
        snapshots.append({
            "index": index,
            "status": response.status_code,
            "final_url": response.url,
            "bytes": len(raw),
            "sha256": sha256_bytes(raw),
            "facetwp": facet,
            "initial_server_rendered_rows": initial_rows,
        })
        parsed.append(facet)
        time.sleep(2)
    return snapshots, parsed


def fetch_rest_snapshot(snapshot_index: int) -> dict:
    snapshot_dir = RAW / "rest" / f"snapshot-{snapshot_index}"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    combined: list[dict] = []
    page_meta: list[dict] = []
    page = 1
    total = None
    total_pages = None

    while total_pages is None or page <= total_pages:
        params = {
            "per_page": 100,
            "page": page,
            "orderby": "title",
            "order": "asc",
            "_fields": "id,slug,link,title,modified,categories",
        }
        response = request_get(REST_URL, params=params, accept="application/json")
        response.raise_for_status()
        raw = response.content
        path = snapshot_dir / f"page-{page:02d}.json"
        save_raw(
            path,
            raw,
            {
                "requested_url": response.request.url,
                "final_url": response.url,
                "status": response.status_code,
                "headers": dict(response.headers),
                "fetched_at_epoch": time.time(),
            },
        )
        rows = response.json()
        if not isinstance(rows, list):
            raise RuntimeError(f"REST page {page} did not return a list")
        if total is None:
            total = int(response.headers.get("X-WP-Total", len(rows)))
            total_pages = int(response.headers.get("X-WP-TotalPages", 1))
        combined.extend(rows)
        page_meta.append({
            "page": page,
            "count": len(rows),
            "sha256": sha256_bytes(raw),
            "x_wp_total": response.headers.get("X-WP-Total"),
            "x_wp_total_pages": response.headers.get("X-WP-TotalPages"),
        })
        page += 1

    normalized = []
    for row in combined:
        normalized.append({
            "id": int(row["id"]),
            "slug": clean_text(row.get("slug")),
            "name": clean_text((row.get("title") or {}).get("rendered")),
            "detail_url": clean_text(row.get("link")),
            "modified": clean_text(row.get("modified")),
            "categories": row.get("categories") or [],
        })
    normalized.sort(key=lambda row: (row["name"].casefold(), row["id"]))
    write_json(snapshot_dir / "combined-normalized.json", normalized)
    return {
        "snapshot_index": snapshot_index,
        "reported_total": total,
        "reported_total_pages": total_pages,
        "page_meta": page_meta,
        "records": normalized,
        "record_count": len(normalized),
        "unique_ids": len({row["id"] for row in normalized}),
        "unique_slugs": len({row["slug"] for row in normalized}),
        "unique_detail_urls": len({row["detail_url"] for row in normalized}),
    }


def classify_external_links(page_url: str, raw: bytes) -> tuple[str, list[dict]]:
    soup = BeautifulSoup(raw, "lxml")
    scope = soup.find("main") or soup.find(attrs={"role": "main"}) or soup.body or soup
    candidates: list[dict] = []
    seen: set[str] = set()
    for position, anchor in enumerate(scope.find_all("a", href=True), start=1):
        href_raw = clean_text(anchor.get("href"))
        if not href_raw or href_raw.startswith(("mailto:", "tel:", "javascript:")):
            continue
        absolute = normalize_website(urljoin(page_url, href_raw))
        if not absolute or absolute in seen:
            continue
        seen.add(absolute)
        host = canonical_host(absolute)
        text = clean_text(anchor.get_text(" ", strip=True))
        excluded_reason = ""
        if not host:
            excluded_reason = "invalid_host"
        elif is_sequoia_host(host):
            excluded_reason = "sequoia_internal"
        elif host in SOCIAL_OR_NON_COMPANY_HOSTS:
            excluded_reason = "social_or_non_company"
        elif any(host.endswith("." + domain) for domain in SOCIAL_OR_NON_COMPANY_HOSTS):
            excluded_reason = "social_or_non_company"

        score = 0
        if not excluded_reason:
            score += 1000 - min(position, 999)
            if "." in text and len(text) <= 100:
                score += 300
            if host.replace("www.", "") in text.lower().replace("www.", ""):
                score += 300
            classes = " ".join(anchor.get("class", []))
            parent_classes = " ".join(anchor.parent.get("class", [])) if anchor.parent else ""
            if re.search(r"website|url|external|company", classes + " " + parent_classes, re.I):
                score += 200
        candidates.append({
            "position": position,
            "href": absolute,
            "host": host,
            "text": text,
            "anchor_classes": anchor.get("class", []),
            "parent_classes": anchor.parent.get("class", []) if anchor.parent else [],
            "excluded_reason": excluded_reason,
            "score": score,
        })

    eligible = [candidate for candidate in candidates if not candidate["excluded_reason"]]
    eligible.sort(key=lambda item: (-item["score"], item["position"]))
    chosen = eligible[0]["href"] if eligible else ""
    return chosen, candidates


def fetch_detail(record: dict) -> dict:
    url = record["detail_url"]
    started = time.time()
    try:
        response = request_get(url)
        raw = response.content
        status = response.status_code
        chosen, candidates = classify_external_links(response.url, raw) if status == 200 else ("", [])
        raw_path = RAW / "details" / f"{record['id']}-{record['slug']}.html.gz"
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(raw_path, "wb", compresslevel=9) as handle:
            handle.write(raw)
        return {
            "id": record["id"],
            "slug": record["slug"],
            "name": record["name"],
            "requested_url": url,
            "final_url": response.url,
            "status": status,
            "content_type": response.headers.get("content-type"),
            "bytes": len(raw),
            "sha256": sha256_bytes(raw),
            "website": chosen,
            "website_host": canonical_host(chosen),
            "candidate_external_links": candidates,
            "elapsed_seconds": round(time.time() - started, 4),
            "error": "",
            "raw_gzip_path": str(raw_path.relative_to(OUT)),
        }
    except Exception as exc:
        return {
            "id": record["id"],
            "slug": record["slug"],
            "name": record["name"],
            "requested_url": url,
            "final_url": "",
            "status": None,
            "content_type": "",
            "bytes": 0,
            "sha256": "",
            "website": "",
            "website_host": "",
            "candidate_external_links": [],
            "elapsed_seconds": round(time.time() - started, 4),
            "error": repr(exc),
            "raw_gzip_path": "",
        }


def build_output(records: list[dict], details: list[dict]) -> tuple[list[dict], str]:
    detail_by_id = {row["id"]: row for row in details}
    final_rows = []
    lines = []
    for record in records:
        detail = detail_by_id.get(record["id"], {})
        website = normalize_website(detail.get("website", ""))
        display_website = website or "未公开（Sequoia未提供公司网站）"
        final_rows.append({
            **record,
            "website": website,
            "website_display": display_website,
            "website_host": canonical_host(website),
            "detail_status": detail.get("status"),
            "detail_final_url": detail.get("final_url", ""),
            "website_source": "Sequoia company detail page" if website else "Sequoia source missing",
        })
        lines.append(f"{record['name']} + {display_website}")
    return final_rows, "\n".join(lines) + "\n"


def make_evidence_zip(zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(OUT.rglob("*")):
            if not path.is_file() or path == zip_path:
                continue
            archive.write(path, path.relative_to(OUT))


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    RAW.mkdir(parents=True, exist_ok=True)

    listing_snapshots, facet_snapshots = fetch_listing_snapshots()
    rest_1 = fetch_rest_snapshot(1)
    time.sleep(2)
    rest_2 = fetch_rest_snapshot(2)

    records_1 = rest_1["records"]
    records_2 = rest_2["records"]
    map_1 = [(r["id"], r["slug"], r["name"], r["detail_url"], r["modified"]) for r in records_1]
    map_2 = [(r["id"], r["slug"], r["name"], r["detail_url"], r["modified"]) for r in records_2]
    snapshots_equal = map_1 == map_2

    details: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(fetch_detail, record): record for record in records_2}
        for future in concurrent.futures.as_completed(futures):
            details.append(future.result())
    details.sort(key=lambda row: (row["name"].casefold(), row["id"]))
    write_json(OUT / "sequoia_company_detail_resolution.json", details)

    final_rows, txt_content = build_output(records_2, details)
    actual_count = len(final_rows)
    prefix = f"sequoia_{actual_count}_companies"

    txt_path = OUT / f"{prefix}_verified.txt"
    md_path = OUT / f"{prefix}_verified.md"
    json_path = OUT / f"{prefix}_verified.json"
    validation_path = OUT / f"{prefix}_validation.json"
    comparison_path = OUT / f"{prefix}_snapshot_comparison.json"
    audit_path = OUT / f"{prefix}_independent_audit.json"
    missing_path = OUT / f"{prefix}_missing_websites.json"
    duplicate_names_path = OUT / f"{prefix}_duplicate_names.json"
    duplicate_websites_path = OUT / f"{prefix}_duplicate_websites.json"
    source_contract_path = OUT / f"{prefix}_source_contract.json"
    manifest_path = OUT / f"{prefix}_manifest.json"
    evidence_zip = OUT / f"{prefix}_evidence.zip"

    txt_path.write_text(txt_content, encoding="utf-8")
    md_lines = [
        "# Sequoia Capital Companies",
        "",
        f"- Source: {LISTING_URL}",
        f"- Verified company records: {actual_count}",
        "- Format: Company Name + Company Website",
        "",
    ] + [f"{row['name']} + {row['website_display']}" for row in final_rows]
    md_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    write_json(json_path, final_rows)

    missing = [row for row in final_rows if not row["website"]]
    successful_details = [row for row in details if row["status"] == 200 and not row["error"]]
    detail_failures = [row for row in details if row["status"] != 200 or row["error"]]

    name_groups: dict[str, list[dict]] = collections.defaultdict(list)
    website_groups: dict[str, list[dict]] = collections.defaultdict(list)
    for row in final_rows:
        name_groups[row["name"].casefold()].append(row)
        if row["website"]:
            website_groups[row["website"]].append(row)
    duplicate_names = {key: rows for key, rows in name_groups.items() if len(rows) > 1}
    duplicate_websites = {key: rows for key, rows in website_groups.items() if len(rows) > 1}
    write_json(missing_path, missing)
    write_json(duplicate_names_path, duplicate_names)
    write_json(duplicate_websites_path, duplicate_websites)

    fwp_total_1 = facet_snapshots[0].get("total_rows")
    fwp_total_2 = facet_snapshots[1].get("total_rows")
    hard_checks = {
        "fwp_snapshots_both_417": fwp_total_1 == 417 and fwp_total_2 == 417,
        "rest_snapshots_report_same_total": rest_1["reported_total"] == rest_2["reported_total"],
        "facetwp_equals_rest": fwp_total_2 == rest_2["reported_total"],
        "rest_record_count_equals_reported": rest_2["record_count"] == rest_2["reported_total"],
        "unique_ids_equals_count": rest_2["unique_ids"] == actual_count,
        "unique_slugs_equals_count": rest_2["unique_slugs"] == actual_count,
        "unique_detail_urls_equals_count": rest_2["unique_detail_urls"] == actual_count,
        "two_rest_snapshots_equal": snapshots_equal,
        "output_lines_equal_count": len(txt_content.rstrip("\n").splitlines()) == actual_count,
        "detail_results_equal_count": len(details) == actual_count,
        "all_detail_pages_http_200": len(detail_failures) == 0,
    }

    if not all(hard_checks.values()):
        status = "FAIL_VALIDATION"
    elif missing:
        status = "PASS_WITH_SOURCE_MISSING_WEBSITES"
    else:
        status = "PASS"

    comparison = {
        "listing_snapshots": listing_snapshots,
        "listing_html_hashes_equal": listing_snapshots[0]["sha256"] == listing_snapshots[1]["sha256"],
        "facetwp_snapshots": facet_snapshots,
        "rest_snapshot_1": {k: v for k, v in rest_1.items() if k != "records"},
        "rest_snapshot_2": {k: v for k, v in rest_2.items() if k != "records"},
        "record_maps_equal": snapshots_equal,
        "record_map_sha256_1": sha256_bytes(json.dumps(map_1, ensure_ascii=False, separators=(",", ":")).encode()),
        "record_map_sha256_2": sha256_bytes(json.dumps(map_2, ensure_ascii=False, separators=(",", ":")).encode()),
    }
    write_json(comparison_path, comparison)

    validation = {
        "status": status,
        "source_url": LISTING_URL,
        "user_claimed_count": USER_CLAIMED_COUNT,
        "official_facetwp_total": fwp_total_2,
        "official_rest_total": rest_2["reported_total"],
        "actual_output_count": actual_count,
        "difference_actual_minus_user_claim": actual_count - USER_CLAIMED_COUNT,
        "facetwp_per_page": facet_snapshots[1].get("per_page"),
        "facetwp_total_pages": facet_snapshots[1].get("total_pages"),
        "rest_page_counts": [page["count"] for page in rest_2["page_meta"]],
        "unique_ids": rest_2["unique_ids"],
        "unique_slugs": rest_2["unique_slugs"],
        "unique_names": len(name_groups),
        "duplicate_name_groups": len(duplicate_names),
        "website_count": actual_count - len(missing),
        "missing_website_count": len(missing),
        "unique_websites": len(website_groups),
        "duplicate_website_groups": len(duplicate_websites),
        "successful_detail_pages": len(successful_details),
        "detail_page_failures": len(detail_failures),
        "hard_checks": hard_checks,
        "txt_sha256": sha256_file(txt_path),
    }
    write_json(validation_path, validation)

    # Independent reconstruction using only saved snapshot-2 normalized records and detail-resolution ledger.
    independent_records = json.loads((RAW / "rest" / "snapshot-2" / "combined-normalized.json").read_text(encoding="utf-8"))
    independent_details = json.loads((OUT / "sequoia_company_detail_resolution.json").read_text(encoding="utf-8"))
    _, independent_txt = build_output(independent_records, independent_details)
    independent_hash = sha256_bytes(independent_txt.encode("utf-8"))
    audit = {
        "status": "PASS" if independent_hash == sha256_file(txt_path) else "FAIL",
        "reconstructed_line_count": len(independent_txt.rstrip("\n").splitlines()),
        "published_line_count": actual_count,
        "reconstructed_sha256": independent_hash,
        "published_sha256": sha256_file(txt_path),
        "exact_text_equal": independent_txt == txt_content,
    }
    write_json(audit_path, audit)

    source_contract = {
        "listing_source": LISTING_URL,
        "all_panel_counter_source": "window.FWP_JSON.preload_data.settings.pager",
        "rest_source": REST_URL,
        "rest_identity_key": "numeric WordPress company post id",
        "rest_pagination": {"per_page": 100, "orderby": "title", "order": "asc"},
        "website_source": "first eligible external non-social link within the company detail page main content, scored by DOM position and domain-like anchor text",
        "website_missing_policy": "Do not guess a domain; emit an explicit Sequoia-source-missing marker",
        "deduplication_policy": "Never deduplicate by displayed name or website; retain every unique WordPress company id",
        "user_claimed_count_policy": "Treat the user-supplied 6107 as a claim to verify, not as a target to force",
    }
    write_json(source_contract_path, source_contract)

    manifest: dict[str, dict] = {}
    for path in sorted(OUT.rglob("*")):
        if path.is_file() and path not in {manifest_path, evidence_zip}:
            manifest[str(path.relative_to(OUT))] = {"bytes": path.stat().st_size, "sha256": sha256_file(path)}
    write_json(manifest_path, manifest)
    make_evidence_zip(evidence_zip)

    result = {
        "status": status,
        "user_claimed_count": USER_CLAIMED_COUNT,
        "official_facetwp_total": fwp_total_2,
        "official_rest_total": rest_2["reported_total"],
        "actual_output_count": actual_count,
        "difference_actual_minus_user_claim": actual_count - USER_CLAIMED_COUNT,
        "unique_ids": rest_2["unique_ids"],
        "unique_slugs": rest_2["unique_slugs"],
        "unique_names": len(name_groups),
        "duplicate_name_groups": len(duplicate_names),
        "website_count": actual_count - len(missing),
        "missing_website_count": len(missing),
        "detail_page_failures": len(detail_failures),
        "txt_path": str(txt_path),
        "txt_sha256": sha256_file(txt_path),
        "md_path": str(md_path),
        "json_path": str(json_path),
        "validation_path": str(validation_path),
        "comparison_path": str(comparison_path),
        "audit_path": str(audit_path),
        "missing_path": str(missing_path),
        "duplicate_names_path": str(duplicate_names_path),
        "duplicate_websites_path": str(duplicate_websites_path),
        "source_contract_path": str(source_contract_path),
        "manifest_path": str(manifest_path),
        "evidence_zip": str(evidence_zip),
        "evidence_zip_sha256": sha256_file(evidence_zip),
        "first_10": [{"name": row["name"], "website": row["website_display"]} for row in final_rows[:10]],
        "last_10": [{"name": row["name"], "website": row["website_display"]} for row in final_rows[-10:]],
    }
    print("SEQUOIA_EXTRACTION_RESULT_START")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("SEQUOIA_EXTRACTION_RESULT_END")

    if status == "FAIL_VALIDATION":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
