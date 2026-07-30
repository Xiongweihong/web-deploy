from __future__ import annotations

import json
import re
from pathlib import Path

import requests

OUT = Path("artifact/jobs-probe")
OUT.mkdir(parents=True, exist_ok=True)
BASE = "https://jobs.a16z.com"
UA = "Mozilla/5.0 (compatible; A16ZJobsAudit/1.0)"

s = requests.Session()
s.headers.update({"User-Agent": UA, "Accept": "application/json,text/html,*/*"})
page = s.get(BASE + "/companies", timeout=120)
page.raise_for_status()
(OUT / "companies.html").write_bytes(page.content)
m = re.search(r"window\.serverInitialData\s*=\s*(\{.*?\});", page.text, re.S)
if not m:
    raise RuntimeError("serverInitialData not found")
initial = json.loads(m.group(1))
token = initial["csrfToken"]
board = initial["board"]
headers = {"X-CSRF-Token": token, "Content-Type": "application/json", "Accept": "application/json", "Referer": BASE + "/companies"}
results = []
for meta in [
    {"size": 45},
    {"size": 45, "page": 0},
    {"size": 1000},
    {"size": 45, "offset": 0},
    {"size": 45, "from": 0},
]:
    payload = {"query": {}, "meta": meta, "board": board}
    r = s.post(BASE + "/api-boards/search-companies", headers=headers, json=payload, timeout=120)
    body_text = r.text
    try:
        body = r.json()
    except Exception:
        body = None
    results.append({
        "payload": payload,
        "status": r.status_code,
        "headers": dict(r.headers),
        "body_type": type(body).__name__,
        "body_keys": list(body) if isinstance(body, dict) else None,
        "body": body,
        "body_preview": body_text[:5000],
    })

(OUT / "probe-results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
summary = []
for x in results:
    body = x.get("body")
    summary.append({
        "payload": x["payload"],
        "status": x["status"],
        "keys": x.get("body_keys"),
        "total": body.get("total") if isinstance(body, dict) else None,
        "meta": body.get("meta") if isinstance(body, dict) else None,
        "companies_len": len(body.get("companies", [])) if isinstance(body, dict) and isinstance(body.get("companies"), list) else None,
        "items_len": len(body.get("items", [])) if isinstance(body, dict) and isinstance(body.get("items"), list) else None,
    })
print("A16Z_JOBS_PROBE_START")
print(json.dumps(summary, ensure_ascii=False, indent=2))
print("A16Z_JOBS_PROBE_END")
