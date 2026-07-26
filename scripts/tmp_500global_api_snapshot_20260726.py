from __future__ import annotations

import hashlib
import json
import shutil
import time
from collections import Counter
from pathlib import Path

import requests

URL = "https://500.co/api/startups"
OUT = Path("artifact-api")
UA = "Mozilla/5.0 (compatible; evidence-audit/1.0)"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def save(path: Path, raw: bytes, meta: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    path.with_suffix(path.suffix + ".meta.json").write_text(
        json.dumps({**meta, "bytes": len(raw), "sha256": sha256(raw)}, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def fetch() -> tuple[bytes, dict]:
    response = requests.get(URL, timeout=180, headers={"User-Agent": UA, "Accept": "application/json"})
    response.raise_for_status()
    return response.content, {
        "url": response.url,
        "status": response.status_code,
        "content_type": response.headers.get("content-type"),
        "date": response.headers.get("date"),
        "etag": response.headers.get("etag"),
        "fetched_at_epoch": time.time(),
    }


def display_name(row: dict) -> str:
    org = row.get("organization") or {}
    return str(org.get("businessName") or org.get("alternativeName") or org.get("name") or "").strip()


def summarize(raw: bytes) -> dict:
    payload = json.loads(raw)
    rows = payload.get("res") or []
    org_ids = [str((row.get("organization") or {}).get("id") or "") for row in rows]
    investment_ids = [str(row.get("id") or "") for row in rows]
    names = [display_name(row).casefold() for row in rows]
    urls = [str((row.get("organization") or {}).get("companyUrl") or "").strip() for row in rows]
    return {
        "status": payload.get("status"),
        "record_count": len(rows),
        "unique_org_id_count": len(set(org_ids)),
        "unique_investment_id_count": len(set(investment_ids)),
        "unique_display_name_count": len(set(names)),
        "duplicate_display_name_groups": {k: v for k, v in Counter(names).items() if v > 1},
        "missing_company_url_count": sum(not value for value in urls),
        "org_id_sequence": org_ids,
        "record_digest_sequence": [sha256(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")) for row in rows],
    }


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    snapshots = []
    for index in (1, 2):
        raw, meta = fetch()
        save(OUT / f"snapshot-{index:02d}.json", raw, meta)
        snapshots.append(summarize(raw))
        if index == 1:
            time.sleep(5)
    comparison = {
        "status": "PASS" if snapshots[0] == snapshots[1] else "CHANGED",
        "snapshot_1": snapshots[0],
        "snapshot_2": snapshots[1],
    }
    (OUT / "comparison.json").write_text(json.dumps(comparison, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({
        "status": comparison["status"],
        "record_count": snapshots[1]["record_count"],
        "unique_org_id_count": snapshots[1]["unique_org_id_count"],
        "unique_display_name_count": snapshots[1]["unique_display_name_count"],
        "duplicate_display_name_groups": snapshots[1]["duplicate_display_name_groups"],
        "missing_company_url_count": snapshots[1]["missing_company_url_count"],
    }, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
