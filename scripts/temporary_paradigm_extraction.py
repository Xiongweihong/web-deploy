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
from playwright.sync_api import sync_playwright

TARGET_URL = "https://www.paradigm.xyz/careers"
COMPANIES_URL = "https://jobs.paradigm.xyz/companies"
JOBS_URL = "https://jobs.paradigm.xyz/jobs"
OUT = Path("artifact")
RAW = OUT / "raw"
NORMALIZED = OUT / "normalized"
USER_AGENT = "Mozilla/5.0 (compatible; paradigm-evidence-crawler/1.0)"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def as_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def normalize_url(value: object) -> str:
    if isinstance(value, dict):
        value = value.get("url") or value.get("href") or value.get("label")
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
    return f"https://{parsed.netloc.lower().removeprefix('www.')}"


def company_id(company: dict) -> str:
    return clean(company.get("id") or company.get("slug") or company.get("domain") or company.get("name"))


def save_bytes(path: Path, data: bytes, meta: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    meta = {**meta, "bytes": len(data), "sha256": digest(data)}
    path.with_suffix(path.suffix + ".meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )


def json_company_documents(captures: list[dict], phase: str = "companies") -> list[dict]:
    docs = []
    for item in captures:
        data = item.get("json")
        if item.get("phase") == phase and isinstance(data, dict) and isinstance(data.get("companies"), list):
            docs.append(data)
    return docs


def json_job_documents(captures: list[dict]) -> list[dict]:
    docs = []
    for item in captures:
        data = item.get("json")
        if isinstance(data, dict) and isinstance(data.get("jobs"), list):
            docs.append(data)
    return docs


def combine_companies(documents: list[dict]) -> tuple[list[dict], dict]:
    by_id: dict[str, dict] = {}
    duplicate_ids: list[str] = []
    totals: list[int] = []
    sequences: list[str] = []
    page_sizes: list[int] = []
    for document in documents:
        companies = document.get("companies") or []
        page_sizes.append(len(companies))
        if document.get("total") is not None:
            totals.append(as_int(document.get("total")))
        sequence = (document.get("meta") or {}).get("sequence")
        if sequence:
            sequences.append(str(sequence))
        for company in companies:
            key = company_id(company)
            if not key:
                continue
            if key in by_id and by_id[key] != company:
                duplicate_ids.append(key)
            by_id[key] = company
    reported_total = max(totals) if totals else 0
    return list(by_id.values()), {
        "reported_totals": sorted(set(totals)),
        "reported_total": reported_total,
        "page_sizes": page_sizes,
        "response_count": len(documents),
        "sequence_count": len(sequences),
        "terminal_sequence_absent": bool(documents) and not (documents[-1].get("meta") or {}).get("sequence"),
        "duplicate_conflicting_ids": sorted(set(duplicate_ids)),
    }


def company_rows(companies: list[dict]) -> tuple[list[dict], list[dict]]:
    all_rows = []
    for company in companies:
        source_id = company_id(company)
        name = clean(company.get("name"))
        domain = clean(company.get("domain"))
        source_website = company.get("website")
        website = normalize_url(source_website) or normalize_url(domain)
        num_jobs = as_int(company.get("numJobs"))
        all_rows.append(
            {
                "source_id": source_id,
                "slug": clean(company.get("slug")),
                "name": name,
                "website": website,
                "source_domain": domain,
                "source_website": source_website,
                "num_jobs": num_jobs,
                "website_provenance": "consider_website" if normalize_url(source_website) else "consider_domain_fallback",
            }
        )
    active = [row for row in all_rows if row["num_jobs"] > 0]
    active.sort(key=lambda row: (row["name"].casefold(), row["source_id"]))
    return active, all_rows


def click_more(page) -> bool:
    patterns = ("load more", "show more", "view more", "more companies", "see more")
    for selector in ("button", "[role='button']", "a"):
        locator = page.locator(selector)
        try:
            count = min(locator.count(), 250)
        except Exception:
            continue
        for index in range(count):
            item = locator.nth(index)
            try:
                if not item.is_visible():
                    continue
                text = clean(item.inner_text()).casefold()
                aria = clean(item.get_attribute("aria-label")).casefold()
                combined = f"{text} {aria}"
                if any(pattern in combined for pattern in patterns):
                    item.scroll_into_view_if_needed()
                    item.click(timeout=5000, force=True)
                    return True
            except Exception:
                continue
    return False


def capture_snapshot(snapshot_number: int) -> dict:
    snapshot_dir = RAW / f"snapshot-{snapshot_number:02d}"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    captures: list[dict] = []
    phase = {"value": "bootstrap"}
    response_counter = {"value": 0}

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 1200},
            locale="en-US",
        )
        page = context.new_page()

        def handle_response(response) -> None:
            url = response.url
            if "/api-boards/" not in url:
                return
            response_counter["value"] += 1
            index = response_counter["value"]
            try:
                body = response.body()
            except Exception:
                return
            request = response.request
            safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "_", urlparse(url).path.strip("/") or "root")
            path = snapshot_dir / "network" / f"{index:04d}_{safe_name}.json"
            request_meta = {
                "phase": phase["value"],
                "url": url,
                "status": response.status,
                "method": request.method,
                "post_data": request.post_data,
                "content_type": response.headers.get("content-type"),
                "fetched_at_epoch": time.time(),
            }
            save_bytes(path, body, request_meta)
            parsed = None
            try:
                parsed = json.loads(body)
            except Exception:
                pass
            captures.append({**request_meta, "path": str(path), "json": parsed})

        page.on("response", handle_response)

        phase["value"] = "target"
        page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=120000)
        page.wait_for_timeout(6000)
        target_html = page.content().encode("utf-8")
        save_bytes(
            snapshot_dir / "target-page.html",
            target_html,
            {"url": page.url, "phase": phase["value"], "fetched_at_epoch": time.time()},
        )
        (snapshot_dir / "target-body.txt").write_text(page.locator("body").inner_text(), encoding="utf-8")
        page.screenshot(path=str(snapshot_dir / "target-page.png"), full_page=True)

        phase["value"] = "companies"
        page.goto(COMPANIES_URL, wait_until="domcontentloaded", timeout=120000)
        page.wait_for_timeout(5000)
        no_progress = 0
        previous_count = -1
        for _ in range(80):
            docs = json_company_documents(captures, "companies")
            companies, info = combine_companies(docs)
            current_count = len(companies)
            total = info["reported_total"]
            if total and current_count >= total:
                break
            clicked = click_more(page)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.keyboard.press("End")
            page.wait_for_timeout(1800)
            docs = json_company_documents(captures, "companies")
            current_count = len(combine_companies(docs)[0])
            if current_count <= previous_count and not clicked:
                no_progress += 1
            elif current_count <= previous_count:
                no_progress += 1
            else:
                no_progress = 0
            previous_count = current_count
            if no_progress >= 6:
                break
        page.wait_for_timeout(2000)
        save_bytes(
            snapshot_dir / "companies-page.html",
            page.content().encode("utf-8"),
            {"url": page.url, "phase": phase["value"], "fetched_at_epoch": time.time()},
        )
        (snapshot_dir / "companies-body.txt").write_text(page.locator("body").inner_text(), encoding="utf-8")
        page.screenshot(path=str(snapshot_dir / "companies-page.png"), full_page=True)

        phase["value"] = "jobs"
        page.goto(JOBS_URL, wait_until="domcontentloaded", timeout=120000)
        page.wait_for_timeout(6000)
        save_bytes(
            snapshot_dir / "jobs-page.html",
            page.content().encode("utf-8"),
            {"url": page.url, "phase": phase["value"], "fetched_at_epoch": time.time()},
        )
        (snapshot_dir / "jobs-body.txt").write_text(page.locator("body").inner_text(), encoding="utf-8")
        page.screenshot(path=str(snapshot_dir / "jobs-page.png"), full_page=True)
        browser.close()

    documents = json_company_documents(captures, "companies")
    companies, company_info = combine_companies(documents)
    active_rows, all_rows = company_rows(companies)
    job_documents = json_job_documents(captures)
    job_totals = sorted({as_int(doc.get("total")) for doc in job_documents if doc.get("total") is not None})
    target_job_totals = sorted(
        {
            as_int(item["json"].get("total"))
            for item in captures
            if item.get("phase") == "target"
            and isinstance(item.get("json"), dict)
            and isinstance(item["json"].get("jobs"), list)
            and item["json"].get("total") is not None
        }
    )
    summary = {
        "snapshot": snapshot_number,
        "captured_at_epoch": time.time(),
        "company_info": company_info,
        "raw_unique_company_count": len(companies),
        "all_company_count": len(all_rows),
        "active_company_count": len(active_rows),
        "sum_company_num_jobs": sum(row["num_jobs"] for row in all_rows),
        "job_totals": job_totals,
        "target_job_totals": target_job_totals,
        "api_response_count": len(captures),
    }
    (snapshot_dir / "snapshot-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    (snapshot_dir / "active-company-rows.json").write_text(
        json.dumps(active_rows, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    return {"rows": active_rows, "all_rows": all_rows, "summary": summary, "dir": snapshot_dir}


def independent_audit(snapshot_dir: Path, normalized_rows: list[dict], txt_path: Path) -> dict:
    raw_hash_errors = []
    company_documents = []
    job_documents = []
    for path in sorted((snapshot_dir / "network").glob("*.json")):
        meta_path = path.with_suffix(path.suffix + ".meta.json")
        if not meta_path.exists():
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        actual = digest(path.read_bytes())
        if actual != meta.get("sha256"):
            raw_hash_errors.append(str(path.relative_to(snapshot_dir)))
        try:
            document = json.loads(path.read_bytes())
        except Exception:
            continue
        if meta.get("phase") == "companies" and isinstance(document.get("companies"), list):
            company_documents.append(document)
        if isinstance(document.get("jobs"), list):
            job_documents.append(document)
    companies, info = combine_companies(company_documents)
    audit_rows, all_rows = company_rows(companies)
    primary_map = {
        row["source_id"]: (row["name"], row["website"], row["num_jobs"])
        for row in normalized_rows
    }
    audit_map = {
        row["source_id"]: (row["name"], row["website"], row["num_jobs"])
        for row in audit_rows
    }
    txt_lines = [line for line in txt_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    expected_lines = [f'{row["name"]} + {row["website"]}' for row in normalized_rows]
    job_totals = sorted({as_int(doc.get("total")) for doc in job_documents if doc.get("total") is not None})
    total_jobs_from_companies = sum(row["num_jobs"] for row in all_rows)
    status = (
        "PASS"
        if not raw_hash_errors
        and info["reported_total"] == len(companies)
        and primary_map == audit_map
        and txt_lines == expected_lines
        and (not job_totals or total_jobs_from_companies in job_totals)
        else "FAIL"
    )
    return {
        "status": status,
        "raw_hash_errors": raw_hash_errors,
        "company_response_count": len(company_documents),
        "reported_company_total": info["reported_total"],
        "reconstructed_all_company_count": len(companies),
        "reconstructed_active_company_count": len(audit_rows),
        "source_id_maps_equal": primary_map == audit_map,
        "txt_lines_equal": txt_lines == expected_lines,
        "txt_line_count": len(txt_lines),
        "job_totals": job_totals,
        "sum_company_num_jobs": total_jobs_from_companies,
    }


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    RAW.mkdir(parents=True)
    NORMALIZED.mkdir(parents=True)

    snapshots = [capture_snapshot(1), capture_snapshot(2)]
    selected = snapshots[-1]
    rows = selected["rows"]
    all_rows = selected["all_rows"]

    txt_path = NORMALIZED / "paradigm-careers-companies.txt"
    json_path = NORMALIZED / "paradigm-careers-companies.json"
    txt_path.write_text(
        "\n".join(f'{row["name"]} + {row["website"]}' for row in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )
    json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    snapshot_maps = [
        {row["source_id"]: (row["name"], row["website"], row["num_jobs"]) for row in snapshot["rows"]}
        for snapshot in snapshots
    ]
    snapshot_comparison = {
        "status": "PASS" if snapshot_maps[0] == snapshot_maps[1] else "FAIL",
        "snapshot_1_count": len(snapshot_maps[0]),
        "snapshot_2_count": len(snapshot_maps[1]),
        "added_ids": sorted(set(snapshot_maps[1]) - set(snapshot_maps[0])),
        "removed_ids": sorted(set(snapshot_maps[0]) - set(snapshot_maps[1])),
        "changed_ids": sorted(
            key for key in set(snapshot_maps[0]) & set(snapshot_maps[1]) if snapshot_maps[0][key] != snapshot_maps[1][key]
        ),
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
        row
        for row in rows
        if row["website"]
        and (urlparse(row["website"]).scheme != "https" or not urlparse(row["website"]).netloc)
    ]
    domain_mismatches = []
    for row in rows:
        if not row["source_domain"] or not row["website"]:
            continue
        source_host = row["source_domain"].casefold().removeprefix("www.")
        website_host = urlparse(row["website"]).netloc.casefold().removeprefix("www.")
        if source_host != website_host:
            domain_mismatches.append(row)

    job_totals = selected["summary"]["job_totals"]
    target_job_totals = selected["summary"]["target_job_totals"]
    sum_jobs = selected["summary"]["sum_company_num_jobs"]
    audit = independent_audit(selected["dir"], rows, txt_path)
    (OUT / "independent-audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )

    errors = []
    company_info = selected["summary"]["company_info"]
    if company_info["reported_total"] != selected["summary"]["raw_unique_company_count"]:
        errors.append("company reported total does not equal captured unique company count")
    if company_info["duplicate_conflicting_ids"]:
        errors.append("conflicting duplicate company IDs")
    if missing_names:
        errors.append("missing company names")
    if missing_websites:
        errors.append("missing company websites")
    if invalid_websites:
        errors.append("invalid company websites")
    if duplicate_ids:
        errors.append("duplicate active company IDs")
    if snapshot_comparison["status"] != "PASS":
        errors.append("two source snapshots differ")
    if audit["status"] != "PASS":
        errors.append("independent audit failed")
    if job_totals and sum_jobs not in job_totals:
        errors.append("sum of company numJobs does not match jobs API total")

    validation = {
        "source_url": TARGET_URL,
        "company_directory_url": COMPANIES_URL,
        "jobs_url": JOBS_URL,
        "status": "PASS" if not errors else "NEEDS_REVIEW",
        "company_reported_total": company_info["reported_total"],
        "company_raw_unique_count": selected["summary"]["raw_unique_company_count"],
        "company_response_page_sizes": company_info["page_sizes"],
        "company_terminal_sequence_absent": company_info["terminal_sequence_absent"],
        "all_directory_company_count": len(all_rows),
        "active_hiring_company_count": len(rows),
        "output_line_count": len(rows),
        "sum_company_num_jobs": sum_jobs,
        "jobs_api_totals": job_totals,
        "target_page_job_totals": target_job_totals,
        "unique_source_id_count": len(set(ids)),
        "unique_name_count": len(set(names)),
        "unique_domain_count": len(set(domains)),
        "missing_name_count": len(missing_names),
        "missing_website_count": len(missing_websites),
        "invalid_website_count": len(invalid_websites),
        "duplicate_id_groups": duplicate_ids,
        "duplicate_name_groups": duplicate_names,
        "duplicate_domain_groups": duplicate_domains,
        "domain_mismatch_count": len(domain_mismatches),
        "domain_mismatches": domain_mismatches,
        "snapshot_comparison": snapshot_comparison,
        "independent_audit": audit,
        "errors": errors,
        "notes": [
            "Output includes companies with numJobs > 0 in the complete Consider company directory.",
            "Companies with numJobs = 0 remain in the raw directory evidence but are excluded from the careers output.",
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
                "company_reported_total": validation["company_reported_total"],
                "company_raw_unique_count": validation["company_raw_unique_count"],
                "all_directory_company_count": validation["all_directory_company_count"],
                "active_hiring_company_count": validation["active_hiring_company_count"],
                "sum_company_num_jobs": validation["sum_company_num_jobs"],
                "jobs_api_totals": validation["jobs_api_totals"],
                "missing_website_count": validation["missing_website_count"],
                "duplicate_name_groups": validation["duplicate_name_groups"],
                "duplicate_domain_groups": validation["duplicate_domain_groups"],
                "domain_mismatch_count": validation["domain_mismatch_count"],
                "snapshot_comparison": snapshot_comparison["status"],
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
