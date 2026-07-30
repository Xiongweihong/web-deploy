from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

MODULE_PATH = Path(__file__).with_name("tmp_multi_vc_final_20260731.py")
spec = importlib.util.spec_from_file_location("multi_vc_final", MODULE_PATH)
if spec is None or spec.loader is None:
    raise RuntimeError("Could not load final extractor")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def fixed_external_candidates(soup, internal_hosts):
    """Accept only explicit absolute external URLs from official detail pages."""
    out = []
    for anchor in soup.find_all("a", href=True):
        raw = str(anchor.get("href") or "").strip()
        if not raw.lower().startswith(("http://", "https://")):
            continue
        url = module.clean_url(raw)
        if not url:
            continue
        host = module.domain(url)
        if (
            not host
            or host in internal_hosts
            or host in module.GENERIC_DOMAINS
            or (urlparse(url).hostname or "").endswith("placeholder.invalid")
        ):
            continue
        if url not in out:
            out.append(url)
    return out


def fixed_fetch_consider(session, source, board_id, host, snapshot):
    """Fetch a complete Consider company directory with bounded cursor pagination."""
    page_url = f"https://{host}/companies"
    response = module.get_retry(session, page_url)
    response.raise_for_status()
    (module.RAW / f"{source}-page-{snapshot}.html").write_bytes(response.content)

    match = re.search(r"window\.serverInitialData\s*=\s*(\{.*?\});", response.text, re.S)
    if not match:
        raise RuntimeError(f"serverInitialData missing for {source}")
    initial = json.loads(match.group(1))
    board = initial.get("board") or {"id": board_id, "isParent": True}
    declared = None
    try:
        declared = int(initial["parents"]["items"][board_id]["numCompanies"])
    except Exception:
        pass

    headers = {
        "X-CSRF-Token": initial.get("csrfToken", ""),
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Referer": page_url,
    }

    selected_size = None
    first_body = None
    failures = []
    for size in [1000, 500, 200, 100, 45]:
        api = session.post(
            f"https://{host}/api-boards/search-companies",
            headers=headers,
            json={"query": {}, "meta": {"size": size}, "board": board},
            timeout=180,
        )
        if api.ok:
            selected_size = size
            first_body = api.json()
            break
        failures.append({"size": size, "status": api.status_code, "body": api.text[:1000]})
    if selected_size is None or first_body is None:
        raise RuntimeError(f"All Consider API page-size attempts failed for {source}: {failures}")

    pages = [first_body]
    all_companies = list(first_body.get("companies") or [])
    total = int(first_body.get("total", -1))
    sequence = (first_body.get("meta") or {}).get("sequence")

    for page_index in range(1, 100):
        if total >= 0 and len(all_companies) >= total:
            break
        if not sequence:
            break
        api = session.post(
            f"https://{host}/api-boards/search-companies",
            headers=headers,
            json={
                "query": {},
                "meta": {"size": selected_size, "sequence": sequence},
                "board": board,
            },
            timeout=180,
        )
        api.raise_for_status()
        body = api.json()
        pages.append(body)
        batch = list(body.get("companies") or [])
        if not batch:
            break
        all_companies.extend(batch)
        next_sequence = (body.get("meta") or {}).get("sequence")
        if next_sequence == sequence:
            break
        sequence = next_sequence

    unique = {}
    for company in all_companies:
        key = str(company.get("slug") or company.get("id") or company.get("name") or "")
        if key:
            unique.setdefault(key, company)
    companies = list(unique.values())

    module.dump(
        module.RAW / f"{source}-api-{snapshot}.json",
        {
            "selected_page_size": selected_size,
            "failed_page_sizes": failures,
            "total": total,
            "pages": pages,
            "companies": companies,
        },
    )

    rows = []
    for company in companies:
        website_field = company.get("website") or {}
        website = website_field.get("url") if isinstance(website_field, dict) else website_field
        if not website and company.get("domain"):
            website = str(company["domain"])
        slug = str(company.get("slug") or company.get("id") or company.get("name") or "")
        rows.append(
            module.record(
                source,
                slug,
                str(company.get("name") or company.get("id") or "").strip(),
                website,
                detail_url=f"https://{host}/companies/{slug}",
                raw=company,
            )
        )

    return rows, {
        "declared": declared,
        "api_total": total,
        "returned": len(rows),
        "selected_page_size": selected_size,
        "api_page_count": len(pages),
        "failed_page_sizes": failures,
    }


module.external_candidates = fixed_external_candidates
module.fetch_consider = fixed_fetch_consider
module.main()

validation_path = Path("artifact/validation.json")
validation = json.loads(validation_path.read_text(encoding="utf-8"))

expected_counts = {
    "iconiq": 172,
    "general-catalyst": 596,
    "index-ventures": 321,
    "capitalg-portfolio": 100,
    "bcv-portfolio": 274,
    "cvs-health-ventures": 28,
    "hanabi": 21,
    "astar": 60,
    "acapital": 110,
}
actual_counts = {
    source: int(validation["per_source"][source]["record_count"])
    for source in expected_counts
}
source_count_matches = {
    source: actual_counts[source] == expected
    for source, expected in expected_counts.items()
}

jobs_checks = {}
for key in ["capitalg_jobs_meta", "bcv_jobs_meta"]:
    snapshots = validation[key]
    jobs_checks[key] = []
    for snapshot_name in ["snapshot_1", "snapshot_2"]:
        item = snapshots[snapshot_name]
        declared = item.get("declared")
        api_total = item.get("api_total")
        returned = item.get("returned")
        jobs_checks[key].append(
            {
                "snapshot": snapshot_name,
                "declared": declared,
                "api_total": api_total,
                "returned": returned,
                "selected_page_size": item.get("selected_page_size"),
                "api_page_count": item.get("api_page_count"),
                "closed": (
                    api_total == returned
                    and (declared is None or declared == api_total)
                ),
            }
        )

khosla_checks = {
    "snapshot_1_unique": validation["khosla_meta"]["snapshot_1"]["unique"],
    "snapshot_2_unique": validation["khosla_meta"]["snapshot_2"]["unique"],
    "unique_counts_equal": (
        validation["khosla_meta"]["snapshot_1"]["unique"]
        == validation["khosla_meta"]["snapshot_2"]["unique"]
    ),
    "category_counts_equal": (
        validation["khosla_meta"]["snapshot_1"]["category_counts"]
        == validation["khosla_meta"]["snapshot_2"]["category_counts"]
    ),
}

all_source_counts_pass = all(source_count_matches.values())
all_jobs_pass = all(
    row["closed"]
    for rows in jobs_checks.values()
    for row in rows
)
all_snapshot_checks_pass = all(validation["snapshot_checks"].values())
all_khosla_pass = all(
    [khosla_checks["unique_counts_equal"], khosla_checks["category_counts_equal"]]
)
independent_audit = json.loads(
    Path("artifact/independent_audit.json").read_text(encoding="utf-8")
)
reconstruction_pass = bool(
    independent_audit.get("json_to_txt_reconstruction_matches")
)

strict_pass = all(
    [
        all_source_counts_pass,
        all_jobs_pass,
        all_snapshot_checks_pass,
        all_khosla_pass,
        reconstruction_pass,
        validation["txt_line_count"] == validation["deduplicated_company_count"],
    ]
)

gate = {
    "status": "PASS" if strict_pass else "FAIL",
    "expected_source_counts": expected_counts,
    "actual_source_counts": actual_counts,
    "source_count_matches": source_count_matches,
    "jobs_count_closure": jobs_checks,
    "khosla_snapshot_closure": khosla_checks,
    "all_source_counts_pass": all_source_counts_pass,
    "all_jobs_pass": all_jobs_pass,
    "all_snapshot_checks_pass": all_snapshot_checks_pass,
    "all_khosla_pass": all_khosla_pass,
    "json_to_txt_reconstruction_pass": reconstruction_pass,
    "txt_line_count": validation["txt_line_count"],
    "deduplicated_company_count": validation["deduplicated_company_count"],
}
Path("artifact/final_gate.json").write_text(
    json.dumps(gate, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
validation["strict_final_gate"] = gate
validation["status"] = "PASS" if strict_pass else "FAIL"
validation_path.write_text(
    json.dumps(validation, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)

print("MULTI_VC_STRICT_GATE_START")
print(json.dumps(gate, ensure_ascii=False, indent=2))
print("MULTI_VC_STRICT_GATE_END")

if not strict_pass:
    raise SystemExit(2)
