from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

V3 = Path(__file__).with_name("tmp_multi_vc_final_runner_v3_20260731.py")
source = V3.read_text(encoding="utf-8")
source = source.replace(
    'namespace: dict[str, object] = {}',
    'namespace: dict[str, object] = {"__file__": str(BASE_RUNNER), "__name__": "__runner_prefix__"}',
    1,
)
if "module.main()" not in source:
    raise RuntimeError("V3 runner main call not found")
prefix, gate_suffix = source.split("module.main()", 1)
namespace: dict[str, object] = {"__file__": str(V3), "__name__": "__runner_v5__"}
exec(compile(prefix, str(V3), "exec"), namespace)
module = namespace["module"]
original_non_index_enrich = namespace["original_enrich_details"]

index_dir = Path(os.environ.get("INDEX_COMPLETE_DIR", "index-complete"))
index_records_path = index_dir / "index-complete.json"
index_validation_path = index_dir / "index-aggregate-validation.json"
if not index_records_path.exists() or not index_validation_path.exists():
    raise RuntimeError(f"Validated Index aggregate not found in {index_dir}")
index_records = json.loads(index_records_path.read_text(encoding="utf-8"))
index_validation = json.loads(index_validation_path.read_text(encoding="utf-8"))
if index_validation.get("status") != "PASS":
    raise RuntimeError("Index aggregate validation did not pass")
index_map = {str(row["source_id"]): row for row in index_records}


def enrich_with_validated_index(session, kind, rows):
    if kind != "index-ventures":
        return original_non_index_enrich(session, kind, rows)
    output = []
    for row in rows:
        key = str(row["source_id"])
        source = index_map.get(key)
        if source is None:
            raise RuntimeError(f"Index aggregate is missing source id {key}")
        name = str(source.get("name") or row["name"]).strip()
        website = module.clean_url(source.get("website") or "")
        enriched = dict(row)
        enriched.update(
            {
                "name": name,
                "name_key": module.normalize_name(name),
                "website": website,
                "domain": module.domain(website),
                "detail_status": source.get("detail_status"),
                "detail_final_url": source.get("detail_final_url"),
                "detail_title": source.get("detail_title"),
                "detail_snapshot_verified": True,
            }
        )
        output.append(enriched)
    if len(output) != 321 or len({item["name_key"] for item in output}) != 321:
        raise RuntimeError("Validated Index aggregate did not produce 321 unique companies")
    module.dump(
        module.RAW / "index-ventures-detail-manifest.json",
        [{k: v for k, v in item.items() if k != "source_raw"} for item in output],
    )
    return output


module.enrich_details = enrich_with_validated_index
module.main()

Path("artifact/index-aggregate-validation.json").write_text(
    index_validation_path.read_text(encoding="utf-8"), encoding="utf-8"
)
shutil.copy2(index_records_path, Path("artifact/raw/index-complete.json"))
exec(compile(gate_suffix, str(V3), "exec"), namespace)
