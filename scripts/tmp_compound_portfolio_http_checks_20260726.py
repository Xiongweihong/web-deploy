from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

TARGET = "https://www.compound.vc/portfolio"
OUT = Path("artifact-http")
UA = "Mozilla/5.0 (compatible; compound-portfolio-evidence-crawler/1.1)"
WORKERS = 10


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def root_url(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text or text == "#":
        return ""
    if text.startswith("//"):
        text = "https:" + text
    elif "://" not in text:
        text = "https://" + text
    elif text.startswith("http://"):
        text = "https://" + text[7:]
    parsed = urlparse(text)
    host = parsed.netloc.casefold().removeprefix("www.")
    return f"https://{host}" if host and "." in host else ""


def extract_rows(html: bytes) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    category_by_parent = {
        "portfolio-aiml-text": "Automation",
        "collection-list": "Healthcare & Biology",
        "collection-list-2": "Crypto",
        "collection-list-3": "Other",
    }
    rows = []
    for position, card in enumerate(soup.select(".portfolio-logo-category.w-dyn-item"), start=1):
        anchor = card.select_one("a.portfolio-text")
        heading = card.find("h4")
        if not anchor or not heading:
            continue
        parent_classes = card.parent.get("class") or []
        category = next((category_by_parent[c] for c in parent_classes if c in category_by_parent), "Unknown")
        source_url = str(anchor.get("href") or "").strip()
        rows.append({
            "position": position,
            "category": category,
            "name": re.sub(r"\s+", " ", heading.get_text(" ", strip=True)).strip(),
            "source_url": source_url,
            "source_root": root_url(source_url),
            "stealth": source_url in {"", "#"},
        })
    return rows


def check(row: dict) -> dict:
    if row["stealth"]:
        return {**row, "status": "STEALTH_NO_PUBLIC_WEBSITE", "final_url": "", "final_root": "", "history": [], "error": ""}
    url = row["source_url"]
    last = None
    for attempt in range(1, 4):
        try:
            response = requests.get(
                url,
                timeout=25,
                allow_redirects=True,
                headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"},
                stream=True,
            )
            final_url = response.url
            history = [{"status": item.status_code, "url": item.url, "location": item.headers.get("location")} for item in response.history]
            return {
                **row,
                "status": response.status_code,
                "final_url": final_url,
                "final_root": root_url(final_url),
                "history": history,
                "content_type": response.headers.get("content-type"),
                "error": "",
                "attempt": attempt,
            }
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < 3:
                time.sleep(attempt * 2)
    return {**row, "status": "ERROR", "final_url": "", "final_root": "", "history": [], "error": repr(last), "attempt": 3}


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    page = requests.get(TARGET, timeout=90, headers={"User-Agent": UA})
    page.raise_for_status()
    (OUT / "portfolio.html").write_bytes(page.content)
    write_json(OUT / "portfolio.html.meta.json", {
        "url": page.url,
        "status": page.status_code,
        "date": page.headers.get("date"),
        "bytes": len(page.content),
        "sha256": sha256(page.content),
    })
    rows = extract_rows(page.content)
    results = []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(check, row) for row in rows]
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda row: row["position"])

    extra = []
    for name, url in [("Shadows historical-domain candidate", "https://shadows.co")]:
        extra.append(check({"position": 0, "category": "Review", "name": name, "source_url": url, "source_root": root_url(url), "stealth": False}))

    summary = {
        "source_count": len(rows),
        "linked_count": sum(not row["stealth"] for row in rows),
        "stealth_count": sum(row["stealth"] for row in rows),
        "category_counts": {category: sum(row["category"] == category for row in rows) for category in sorted({row["category"] for row in rows})},
        "clear_http_results": sum(isinstance(row["status"], int) for row in results),
        "errors": sum(row["status"] == "ERROR" for row in results),
        "redirected_root_count": sum(bool(row["final_root"] and row["final_root"] != row["source_root"]) for row in results),
        "generated_at_epoch": time.time(),
    }
    write_json(OUT / "website-checks.json", results)
    write_json(OUT / "extra-review-checks.json", extra)
    write_json(OUT / "summary.json", summary)
    manifest = {"files": {}}
    for path in sorted(OUT.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            manifest["files"][str(path.relative_to(OUT))] = {"bytes": path.stat().st_size, "sha256": sha256(path.read_bytes())}
    write_json(OUT / "manifest.json", manifest)
    print("HTTP_CHECK_SUMMARY_START")
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    print("HTTP_CHECK_SUMMARY_END")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        OUT.mkdir(parents=True, exist_ok=True)
        write_json(OUT / "fatal-error.json", {"error": repr(exc), "traceback": traceback.format_exc()})
        raise
