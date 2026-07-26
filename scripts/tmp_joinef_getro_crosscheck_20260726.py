from __future__ import annotations

import hashlib
import json
import shutil
import time
import traceback
from pathlib import Path

import requests

API = "https://api.getro.com/api/v2/collections/228/search/companies"
ORIGIN = "https://portfolio.joinef.com"
REFERER = "https://portfolio.joinef.com/companies"
OUT = Path("artifact")
UA = "Mozilla/5.0 (compatible; joinef-evidence-crawler/2.1)"
TARGET_NAMES = {
    "Sonantic","Credit Kudos","Thalya","Zash","Fortephy","Lemonade","Vine Health","Avie","Triple","Bioponics",
    "Keyframe","ForeQast","Represent","Energy Lite","Avocarrot","Magic Pony Technology","TriplePlay","Tera-X","Friday Finance","Lightning Tree"
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def save(path: Path, raw: bytes, meta: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    meta = {**meta, "bytes": len(raw), "sha256": sha256(raw)}
    path.with_suffix(path.suffix + ".meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def fetch(page: int, page_size: int = 100) -> tuple[bytes, dict]:
    payload = {"hitsPerPage": page_size, "page": page, "query": "", "filters": ""}
    last = None
    for attempt in range(1, 6):
        try:
            response = requests.post(
                API,
                json=payload,
                timeout=120,
                headers={
                    "User-Agent": UA,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Origin": ORIGIN,
                    "Referer": REFERER,
                },
            )
            response.raise_for_status()
            return response.content, {
                "url": response.url,
                "status": response.status_code,
                "content_type": response.headers.get("content-type"),
                "request_payload": payload,
                "date": response.headers.get("date"),
                "attempt": attempt,
            }
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < 5:
                time.sleep(min(20, 2 ** attempt))
    raise RuntimeError(f"fetch failed: {last}")


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    all_companies = []
    totals = []
    page_sizes = []
    for page in range(50):
        raw, meta = fetch(page)
        save(OUT / "raw" / f"page-{page:03d}.json", raw, meta)
        data = json.loads(raw)
        results = data.get("results") or {}
        companies = results.get("companies") or []
        total = int(results.get("count") or 0)
        totals.append(total)
        page_sizes.append(len(companies))
        all_companies.extend(companies)
        if not companies or (total and len(all_companies) >= total):
            break
    target_lower = {name.casefold(): name for name in TARGET_NAMES}
    exact = []
    fuzzy = []
    for company in all_companies:
        name = str(company.get("name") or "").strip()
        if name.casefold() in target_lower:
            exact.append(company)
        elif any(token in name.casefold() or name.casefold() in token for token in target_lower):
            fuzzy.append(company)
    result = {
        "reported_totals": sorted(set(totals)),
        "page_sizes": page_sizes,
        "raw_record_count": len(all_companies),
        "unique_id_count": len({str(c.get('id') or c.get('objectID') or c.get('slug') or c.get('name')) for c in all_companies}),
        "exact_matches": exact,
        "fuzzy_matches": fuzzy,
        "unmatched_target_names": sorted(TARGET_NAMES - {str(c.get('name') or '').strip() for c in exact}, key=str.casefold),
    }
    (OUT / "getro-crosscheck.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({
        "reported_totals": result["reported_totals"],
        "page_sizes": result["page_sizes"],
        "raw_record_count": result["raw_record_count"],
        "exact_match_count": len(exact),
        "unmatched_target_names": result["unmatched_target_names"],
        "matches": [{"name": c.get("name"), "domain": c.get("domain"), "slug": c.get("slug"), "id": c.get("id") or c.get("objectID")} for c in exact],
    }, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "fatal-error.json").write_text(json.dumps({"error": repr(exc), "traceback": traceback.format_exc()}, ensure_ascii=False, indent=2), encoding="utf-8")
        raise
