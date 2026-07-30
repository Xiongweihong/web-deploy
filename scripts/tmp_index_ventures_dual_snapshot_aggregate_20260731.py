from __future__ import annotations

import json
import os
from pathlib import Path

INPUT = Path(os.environ.get("INDEX_SHARD_DIR", "index-shards"))
OUT = Path(os.environ.get("INDEX_AGGREGATE_DIR", "artifact"))
OUT.mkdir(parents=True, exist_ok=True)


def dump(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


files = sorted(INPUT.rglob("index-snapshot-*-shard-*.json"))
if len(files) != 40:
    raise RuntimeError(f"Expected 40 Index snapshot shard files, found {len(files)}")

snapshots = {1: [], 2: []}
shard_audit = []
for path in files:
    payload = json.loads(path.read_text(encoding="utf-8"))
    snapshot = int(payload["snapshot"])
    if payload["error_count"]:
        raise RuntimeError(f"Shard contains errors: {path}")
    snapshots[snapshot].extend(payload["records"])
    shard_audit.append({
        "file": str(path),
        "snapshot": snapshot,
        "shard": int(payload["shard"]),
        "assigned_count": int(payload["assigned_count"]),
        "success_count": int(payload["success_count"]),
        "error_count": int(payload["error_count"]),
    })

for snapshot in [1, 2]:
    snapshots[snapshot].sort(key=lambda row: int(row["position"]))


def stable_map(rows):
    return {
        str(row["source_id"]): (
            str(row["name"]),
            str(row.get("website") or ""),
            int(row["detail_status"]),
        )
        for row in rows
    }

first = snapshots[1]
second = snapshots[2]
first_map = stable_map(first)
second_map = stable_map(second)
positions = [int(row["position"]) for row in second]
ids = [str(row["source_id"]) for row in second]
names = [str(row["name"]) for row in second]
statuses = [int(row["detail_status"]) for row in second]

validation = {
    "status": "PASS",
    "shard_count": len(shard_audit),
    "snapshot_1_record_count": len(first),
    "snapshot_2_record_count": len(second),
    "snapshot_1_unique_id_count": len(first_map),
    "snapshot_2_unique_id_count": len(second_map),
    "snapshot_maps_equal": first_map == second_map,
    "snapshot_2_unique_position_count": len(set(positions)),
    "positions_are_1_through_321": positions == list(range(1, 322)),
    "snapshot_2_unique_name_count": len(set(names)),
    "all_detail_status_200": all(status == 200 for status in statuses),
    "website_count": sum(bool(row.get("website")) for row in second),
    "source_missing_website_count": sum(not row.get("website") for row in second),
    "shards": sorted(shard_audit, key=lambda row: (row["snapshot"], row["shard"])),
}
strict = all([
    validation["shard_count"] == 40,
    validation["snapshot_1_record_count"] == 321,
    validation["snapshot_2_record_count"] == 321,
    validation["snapshot_1_unique_id_count"] == 321,
    validation["snapshot_2_unique_id_count"] == 321,
    validation["snapshot_maps_equal"],
    validation["snapshot_2_unique_position_count"] == 321,
    validation["positions_are_1_through_321"],
    validation["snapshot_2_unique_name_count"] == 321,
    validation["all_detail_status_200"],
])
validation["status"] = "PASS" if strict else "FAIL"
dump(OUT / "index-complete.json", second)
dump(OUT / "index-snapshot-1.json", first)
dump(OUT / "index-aggregate-validation.json", validation)
print("INDEX_DUAL_SNAPSHOT_AGGREGATE_RESULT")
print(json.dumps(validation, ensure_ascii=False, indent=2))
if not strict:
    raise SystemExit(2)
