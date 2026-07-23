from __future__ import annotations

import concurrent.futures
import json
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

PROJECT = "oqc9t74k"
DATASET = "production"
ENDPOINT = f"https://{PROJECT}.apicdn.sanity.io/v2026-07-01/data/query/{DATASET}"
OUT = Path("artifact-http")
USER_AGENT = "Mozilla/5.0 (compatible; dragonfly-portfolio-link-checker/1.0)"


def clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def root_url(value: str) -> str:
    text = clean(value)
    if not text:
        return ""
    if text.startswith("//"):
        text = "https:" + text
    elif "://" not in text:
        text = "https://" + text
    parsed = urlparse(text)
    host = parsed.netloc.casefold().removeprefix("www.")
    return f"https://{host}" if host else ""


def check(item: dict) -> dict:
    link = item.get("link") or {}
    source_url = clean(link.get("linkHref"))
    row = {
        "source_id": clean(item.get("_id")),
        "name": clean(item.get("title")),
        "source_url": source_url,
        "source_root": root_url(source_url),
        "source_host": urlparse(root_url(source_url)).netloc,
    }
    if not source_url:
        row.update({"status": None, "final_url": "", "final_root": "", "final_host": "", "history": [], "error": "missing source url"})
        return row
    try:
        response = requests.get(
            source_url,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"},
            timeout=25,
            allow_redirects=True,
            stream=True,
        )
        row.update(
            {
                "status": response.status_code,
                "final_url": response.url,
                "final_root": root_url(response.url),
                "final_host": urlparse(root_url(response.url)).netloc,
                "history": [
                    {"status": h.status_code, "url": h.url, "location": h.headers.get("location")}
                    for h in response.history
                ],
                "content_type": response.headers.get("content-type"),
                "error": "",
            }
        )
        response.close()
    except Exception as exc:  # noqa: BLE001
        row.update({"status": None, "final_url": "", "final_root": "", "final_host": "", "history": [], "content_type": "", "error": repr(exc)})
    return row


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    response = requests.get(
        ENDPOINT,
        params={"query": '*[_type == "portfolioItem"]'},
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=90,
    )
    response.raise_for_status()
    raw = response.content
    (OUT / "portfolio-items.json").write_bytes(raw)
    items = response.json().get("result") or []
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
        rows = list(executor.map(check, items))
    rows.sort(key=lambda row: (row["name"].casefold(), row["source_id"]))
    candidates = [
        row
        for row in rows
        if row["error"]
        or row["status"] is None
        or row["status"] >= 400
        or (row["final_host"] and row["source_host"] != row["final_host"])
        or row["source_host"] in {"x.com", "twitter.com"}
    ]
    (OUT / "website-checks.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    (OUT / "review-candidates.json").write_text(json.dumps(candidates, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    summary = {
        "captured_at_epoch": time.time(),
        "portfolio_item_count": len(items),
        "check_count": len(rows),
        "explicit_response_count": sum(1 for row in rows if row["status"] is not None),
        "success_or_redirect_response_count": sum(1 for row in rows if row["status"] is not None and row["status"] < 400),
        "error_count": sum(1 for row in rows if row["error"]),
        "different_final_host_count": sum(1 for row in rows if row["final_host"] and row["source_host"] != row["final_host"]),
        "review_candidate_count": len(candidates),
    }
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
