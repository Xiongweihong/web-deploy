from __future__ import annotations

import json
import os
from pathlib import Path

INPUT = Path(os.environ.get("INDEX_SHARD_DIR", "index-shards"))
OUT = Path(os.environ.get("INDEX_AGGREGATE_DIR", "artifact"))
OUT.mkdir(parents=True, exist_ok=True)


def dump(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


files = sorted(INPUT.rglob("index-shard-*.json"))
if len(files) != 8:
    raise RuntimeError(f"Expected 8 Index shard files, found {len(files)}: {files}")

records = []
shards = []
for path in files:
    payload = json.loads(path.read_text(encoding="utf-8"))
    shards.append({
        "file": str(path),
        "shard": payload["shard"],
        "assigned_count": payload["assigned_count"],
        "snapshot_maps_equal": payload["snapshot_maps_equal"],
        "error_count": payload["error_count"],
    })
    if not payload["snapshot_maps_equal"] or payload["error_count"]:
        raise RuntimeError(f"Shard failed its own validation: {path}")
    records.extend(payload["snapshot_2"])

records.sort(key=lambda row: int(row["position"]))
ids = [row["source_id"] for row in records]
positions = [int(row["position"]) for row in records]
names = [row.get("name") for row in records]
statuses = [row.get("detail_status") for row in records]

validation = {
    "status": "PASS",
    "shards": shards,
    "record_count": len(records),
    "unique_source_id_count": len(set(ids)),
    "unique_position_count": len(set(positions)),
    "positions_are_1_through_321": positions == list(range(1, 322)),
    "unique_name_count": len(set(names)),
    "all_detail_status_200": all(status == 200 for status in statuses),
    "website_count": sum(bool(row.get("website")) for row in records),
    "source_missing_website_count": sum(not row.get("website") for row in records),
    "error_count": sum(bool(row.get("error")) for row in records),
}

strict = all([
    validation["record_count"] == 321,
    validation["unique_source_id_count"] == 321,
    validation["unique_position_count"] == 321,
    validation["positions_are_1_through_321"],
    validation["unique_name_count"] == 321,
    validation["all_detail_status_200"],
    validation["error_count"] == 0,
])
validation["status"] = "PASS" if strict else "FAIL"

dump(OUT / "index-complete.json", records)
dump(OUT / "index-aggregate-validation.json", validation)
print("INDEX_AGGREGATE_RESULT")
print(json.dumps(validation, ensure_ascii=False, indent=2))
if not strict:
    raise SystemExit(2)
