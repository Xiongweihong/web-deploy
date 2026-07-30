from __future__ import annotations

import importlib.util
import json
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


module.external_candidates = fixed_external_candidates
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
