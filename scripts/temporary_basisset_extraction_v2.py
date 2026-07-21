from __future__ import annotations

import json
import re
import sys
import traceback
from pathlib import Path

import temporary_basisset_extraction as base


def fixed_independent_audit(
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
    page_name_pattern = re.compile(r"^page-\d{6}\.json$")

    for kind, raw_dir, record_key, destination in (
        ("jobs", selected_jobs_dir, "jobs", audit_jobs),
        ("companies", selected_companies_dir, "companies", audit_companies),
    ):
        for raw_path in sorted(raw_dir.iterdir()):
            if not raw_path.is_file() or not page_name_pattern.fullmatch(raw_path.name):
                continue
            meta_path = raw_path.with_name(raw_path.stem + ".meta.json")
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            actual_hash = base.digest(raw_path.read_bytes())
            if actual_hash != meta.get("sha256"):
                raw_hash_errors.append(f"{kind}/{raw_path.name}")
            payload = json.loads(raw_path.read_bytes())
            destination.extend(((payload.get("results") or {}).get(record_key) or []))

    reconstructed = base.audit_reconstruct(audit_jobs, audit_companies)
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


base.independent_audit = fixed_independent_audit

if __name__ == "__main__":
    try:
        base.main()
    except Exception as exc:
        base.OUT.mkdir(parents=True, exist_ok=True)
        error = {"status": "FATAL", "error": repr(exc), "traceback": traceback.format_exc()}
        (base.OUT / "fatal-error.json").write_text(
            json.dumps(error, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )
        print(error["traceback"], file=sys.stderr)
        sys.exit(0)
