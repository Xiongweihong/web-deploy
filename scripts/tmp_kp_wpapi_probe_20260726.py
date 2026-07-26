from __future__ import annotations

import hashlib
import json
import shutil
import time
import traceback
from pathlib import Path

import requests

BASE = "https://www.kleinerperkins.com"
OUT = Path("artifact-api")
UA = "Mozilla/5.0 (compatible; kp-evidence-crawler/1.1)"

def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

def get(name: str, url: str) -> dict:
    last = None
    for attempt in range(1, 6):
        try:
            r = requests.get(url, timeout=90, allow_redirects=True, headers={"User-Agent": UA, "Accept": "application/json,*/*;q=0.8"})
            raw = r.content
            path = OUT / f"{name}.bin"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            meta = {
                "requested_url": r.request.url,
                "final_url": r.url,
                "status": r.status_code,
                "content_type": r.headers.get("content-type"),
                "date": r.headers.get("date"),
                "x_wp_total": r.headers.get("x-wp-total"),
                "x_wp_totalpages": r.headers.get("x-wp-totalpages"),
                "bytes": len(raw),
                "sha256": sha256(raw),
                "attempt": attempt,
            }
            write_json(path.with_suffix(".meta.json"), meta)
            try:
                payload = r.json()
            except Exception:
                payload = None
            return {"meta": meta, "json": payload, "text_prefix": r.text[:1000]}
        except Exception as exc:
            last = exc
            if attempt < 5:
                time.sleep(min(20, 2 ** attempt))
    return {"error": repr(last), "url": url}

def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    urls = {
        "root": f"{BASE}/wp-json/",
        "types": f"{BASE}/wp-json/wp/v2/types",
        "search-home": f"{BASE}/wp-json/wp/v2/search?search=%40Home%20Network&per_page=10",
        "company-list": f"{BASE}/wp-json/wp/v2/company?per_page=1",
        "companies-list": f"{BASE}/wp-json/wp/v2/companies?per_page=1",
        "partnership-list": f"{BASE}/wp-json/wp/v2/partnership?per_page=1",
        "partnerships-list": f"{BASE}/wp-json/wp/v2/partnerships?per_page=1",
        "company-710": f"{BASE}/wp-json/wp/v2/company/710",
        "companies-710": f"{BASE}/wp-json/wp/v2/companies/710",
        "partnership-710": f"{BASE}/wp-json/wp/v2/partnership/710",
        "partnerships-710": f"{BASE}/wp-json/wp/v2/partnerships/710",
    }
    results = {name: get(name, url) for name, url in urls.items()}

    type_routes = {}
    types_payload = results.get("types", {}).get("json")
    if isinstance(types_payload, dict):
        for key, value in types_payload.items():
            type_routes[key] = {
                "name": value.get("name"),
                "rest_base": value.get("rest_base"),
                "rest_namespace": value.get("rest_namespace"),
                "slug": value.get("slug"),
                "viewable": value.get("viewable"),
            }

    summary = {
        "results": {
            name: {
                "status": value.get("meta", {}).get("status"),
                "content_type": value.get("meta", {}).get("content_type"),
                "x_wp_total": value.get("meta", {}).get("x_wp_total"),
                "x_wp_totalpages": value.get("meta", {}).get("x_wp_totalpages"),
                "json_type": type(value.get("json")).__name__ if "json" in value else None,
                "json_keys": sorted(value.get("json").keys()) if isinstance(value.get("json"), dict) else None,
                "json_count": len(value.get("json")) if isinstance(value.get("json"), (list, dict)) else None,
                "text_prefix": value.get("text_prefix", "")[:300],
                "error": value.get("error"),
            }
            for name, value in results.items()
        },
        "type_routes": type_routes,
    }
    write_json(OUT / "api-probe-summary.json", summary)
    print("API_PROBE_SUMMARY_START")
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    print("API_PROBE_SUMMARY_END")

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        OUT.mkdir(parents=True, exist_ok=True)
        write_json(OUT / "fatal-error.json", {"error": repr(exc), "traceback": traceback.format_exc()})
        raise
