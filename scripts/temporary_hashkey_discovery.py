from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
import traceback
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

PAGE_URL = "https://hashkey.capital/portfolio/index.html"
ENDPOINT = "https://hashkey.capital/portfolio/getIconList"
INDEX_JS_URL = "https://hashkey.capital/static/js/index.js"
OUT = Path("artifact")
RAW = OUT / "raw"
NORMALIZED = OUT / "normalized"
UA = "Mozilla/5.0 (compatible; hashkey-portfolio-audit/2.0)"

OVERRIDES = {
    "Solayer": {
        "source_website": "https://fuzz.land/",
        "final_website": "https://solayer.org",
        "reason": "The source portfolio link points to Fuzzland, while Solayer's current official site is solayer.org.",
        "evidence_urls": ["https://solayer.org/"],
    },
    "Sign": {
        "source_website": "https://www.ethsign.xyz/",
        "final_website": "https://sign.global",
        "reason": "EthSign rebranded to Sign; the current official site is sign.global.",
        "evidence_urls": ["https://sign.global/", "https://docs.sign.global/"],
    },
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def root_url(value: object) -> str:
    text = clean(value)
    if not text:
        return ""
    if text.startswith("//"):
        text = "https:" + text
    elif "://" not in text:
        text = "https://" + text
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    host = parsed.netloc.casefold().removeprefix("www.")
    return f"https://{host}"


def save(path: Path, data: bytes, meta: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    payload = {**meta, "bytes": len(data), "sha256": sha256(data)}
    path.with_suffix(path.suffix + ".meta.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )


def get_with_retries(session: requests.Session, url: str, attempts: int = 5) -> requests.Response:
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = session.get(url, timeout=60, allow_redirects=True)
            if response.status_code in {408, 429} or response.status_code >= 500:
                raise RuntimeError(f"transient HTTP {response.status_code}")
            response.raise_for_status()
            return response
        except Exception as exc:
            last = exc
            if attempt < attempts:
                time.sleep(min(15, 2**attempt))
    raise RuntimeError(f"GET failed for {url}: {last}")


def post_portfolio(session: requests.Session, attempts: int = 5) -> requests.Response:
    last: Exception | None = None
    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/json",
        "Origin": "https://hashkey.capital",
        "Referer": PAGE_URL,
        "X-Requested-With": "XMLHttpRequest",
    }
    for attempt in range(1, attempts + 1):
        try:
            response = session.post(ENDPOINT, json={"key": ""}, headers=headers, timeout=60)
            if response.status_code in {408, 429} or response.status_code >= 500:
                raise RuntimeError(f"transient HTTP {response.status_code}")
            response.raise_for_status()
            return response
        except Exception as exc:
            last = exc
            if attempt < attempts:
                time.sleep(min(15, 2**attempt))
    raise RuntimeError(f"POST failed for {ENDPOINT}: {last}")


def parse_primary(raw_json: bytes) -> tuple[list[dict], dict]:
    payload = json.loads(raw_json)
    if payload.get("success") is not True or payload.get("code") != 0:
        raise RuntimeError(f"unexpected endpoint envelope: {payload!r}")
    html = payload.get("data")
    if not isinstance(html, str):
        raise RuntimeError("endpoint data is not an HTML string")
    soup = BeautifulSoup(html, "lxml")
    rows: list[dict] = []
    anchor_inconsistencies = []
    empty_entries = []
    for position, dd in enumerate(soup.select("dd"), start=1):
        p = dd.find("p")
        text_anchor = p.find("a", href=True) if p else None
        all_anchors = dd.find_all("a", href=True)
        name = clean(text_anchor.get_text(" ", strip=True) if text_anchor else (p.get_text(" ", strip=True) if p else ""))
        source_website = clean(text_anchor.get("href") if text_anchor else (all_anchors[0].get("href") if all_anchors else ""))
        image = dd.find("img")
        image_src = urljoin(PAGE_URL, image.get("src")) if image and image.get("src") else ""
        anchor_hrefs = [clean(anchor.get("href")) for anchor in all_anchors]
        if anchor_hrefs and len(set(anchor_hrefs)) != 1:
            anchor_inconsistencies.append({"position": position, "name": name, "hrefs": anchor_hrefs})
        if not name or not source_website:
            empty_entries.append({"position": position, "name": name, "website": source_website})
        override = OVERRIDES.get(name)
        if override and root_url(source_website) == root_url(override["source_website"]):
            final_website = override["final_website"]
            provenance = "reviewed_override"
        else:
            final_website = root_url(source_website)
            provenance = "hashkey_source_link"
        source_id = sha256(f"{name}\0{source_website}\0{image_src}".encode("utf-8"))[:24]
        rows.append(
            {
                "source_position": position,
                "source_id": source_id,
                "name": name,
                "source_website": source_website,
                "website": final_website,
                "website_provenance": provenance,
                "image_url": image_src,
                "source_anchor_count": len(all_anchors),
            }
        )
    info = {
        "success": payload.get("success"),
        "code": payload.get("code"),
        "message": payload.get("msg"),
        "html_bytes": len(html.encode("utf-8")),
        "dd_count": len(soup.select("dd")),
        "li_count": len(soup.select("li")),
        "image_count": len(soup.select("dd img")),
        "text_link_count": len(soup.select("dd p a[href]")),
        "all_company_anchor_count": len(soup.select("dd a[href]")),
        "anchor_inconsistencies": anchor_inconsistencies,
        "empty_entries": empty_entries,
    }
    return rows, info


def parse_independent(raw_json: bytes) -> list[tuple[str, str]]:
    payload = json.loads(raw_json)
    html = payload["data"]
    # Independent reconstruction from the repeated textual <p><a> pattern.
    pattern = re.compile(
        r'<p\s+class="fnt_15"\s*>\s*<a\s+href="([^"]+)"[^>]*>(.*?)</a>\s*</p>',
        re.I | re.S,
    )
    rows = []
    for href, label_html in pattern.findall(html):
        label = BeautifulSoup(label_html, "lxml").get_text(" ", strip=True)
        rows.append((clean(label), clean(href)))
    return rows


def capture(snapshot: int) -> dict:
    snap_dir = RAW / f"snapshot-{snapshot:02d}"
    snap_dir.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.8"})

    page = get_with_retries(session, PAGE_URL)
    save(
        snap_dir / "portfolio-page.html",
        page.content,
        {
            "requested_url": PAGE_URL,
            "final_url": page.url,
            "status": page.status_code,
            "content_type": page.headers.get("content-type"),
            "date": page.headers.get("date"),
            "etag": page.headers.get("etag"),
            "fetched_at_epoch": time.time(),
        },
    )
    index_js = get_with_retries(session, INDEX_JS_URL)
    save(
        snap_dir / "index.js",
        index_js.content,
        {
            "requested_url": INDEX_JS_URL,
            "final_url": index_js.url,
            "status": index_js.status_code,
            "content_type": index_js.headers.get("content-type"),
            "fetched_at_epoch": time.time(),
        },
    )
    response = post_portfolio(session)
    save(
        snap_dir / "portfolio-list.json",
        response.content,
        {
            "requested_url": ENDPOINT,
            "final_url": response.url,
            "status": response.status_code,
            "content_type": response.headers.get("content-type"),
            "request_method": "POST",
            "request_json": {"key": ""},
            "date": response.headers.get("date"),
            "etag": response.headers.get("etag"),
            "fetched_at_epoch": time.time(),
        },
    )
    rows, info = parse_primary(response.content)
    page_text = page.text
    js_text = index_js.text
    source_contract = {
        "page_invokes_getIconList": bool(re.search(r"getIconList\s*\(\s*\)\s*;", page_text)),
        "endpoint_in_script": "/portfolio/getIconList" in js_text,
        "blank_key_request_supported": '"key":icon_searchkey' in js_text or "'key':icon_searchkey" in js_text,
        "pagination_terms_near_function": False,
    }
    match = re.search(r"function\s+getIconList\s*\(\s*\)\s*\{(.*?)\n\}", js_text, re.S)
    if match:
        function_body = match.group(1)
        source_contract["pagination_terms_near_function"] = bool(
            re.search(r"page|cursor|sequence|offset|limit", function_body, re.I)
        )
        source_contract["function_body"] = function_body[:3000]
    summary = {
        "snapshot": snapshot,
        "captured_at_epoch": time.time(),
        "endpoint_sha256": sha256(response.content),
        "row_count": len(rows),
        "source_info": info,
        "source_contract": source_contract,
    }
    (snap_dir / "primary-rows.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    (snap_dir / "snapshot-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    return {"rows": rows, "summary": summary, "dir": snap_dir}


def check_website(row: dict) -> dict:
    url = row["website"]
    result = {"name": row["name"], "requested_url": url}
    try:
        response = requests.get(
            url,
            timeout=20,
            allow_redirects=True,
            stream=True,
            headers={"User-Agent": UA, "Accept": "text/html,*/*"},
        )
        result.update(
            {
                "status": response.status_code,
                "final_url": response.url,
                "final_host": urlparse(response.url).netloc.casefold().removeprefix("www."),
                "redirect_count": len(response.history),
                "content_type": response.headers.get("content-type"),
                "ok_http": response.status_code < 500,
            }
        )
        response.close()
    except Exception as exc:
        result.update({"error": repr(exc), "ok_http": False})
    return result


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    RAW.mkdir(parents=True)
    NORMALIZED.mkdir(parents=True)

    snapshots = [capture(1)]
    time.sleep(3)
    snapshots.append(capture(2))
    selected = snapshots[-1]
    rows = selected["rows"]

    source_maps = [
        [(row["name"], row["source_website"], row["image_url"]) for row in snap["rows"]]
        for snap in snapshots
    ]
    snapshot_comparison = {
        "status": "PASS" if source_maps[0] == source_maps[1] else "FAIL",
        "snapshot_1_count": len(source_maps[0]),
        "snapshot_2_count": len(source_maps[1]),
        "ordered_source_rows_equal": source_maps[0] == source_maps[1],
        "snapshot_1_endpoint_sha256": snapshots[0]["summary"]["endpoint_sha256"],
        "snapshot_2_endpoint_sha256": snapshots[1]["summary"]["endpoint_sha256"],
    }
    (OUT / "snapshot-comparison.json").write_text(
        json.dumps(snapshot_comparison, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )

    txt_path = NORMALIZED / "hashkey-selected-portfolio-companies.txt"
    json_path = NORMALIZED / "hashkey-selected-portfolio-companies.json"
    txt_path.write_text(
        "\n".join(f'{row["name"]} + {row["website"]}' for row in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )
    json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    # Independent audit from exact raw bytes and a separate parser.
    raw_path = selected["dir"] / "portfolio-list.json"
    raw_meta_path = raw_path.with_suffix(raw_path.suffix + ".meta.json")
    raw_meta = json.loads(raw_meta_path.read_text(encoding="utf-8"))
    audit_pairs = parse_independent(raw_path.read_bytes())
    primary_pairs = [(row["name"], row["source_website"]) for row in rows]
    txt_lines = [line for line in txt_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    expected_lines = [f'{row["name"]} + {row["website"]}' for row in rows]
    independent_audit = {
        "status": "PASS"
        if sha256(raw_path.read_bytes()) == raw_meta.get("sha256")
        and audit_pairs == primary_pairs
        and txt_lines == expected_lines
        else "FAIL",
        "raw_hash_matches_metadata": sha256(raw_path.read_bytes()) == raw_meta.get("sha256"),
        "independent_pair_count": len(audit_pairs),
        "primary_pair_count": len(primary_pairs),
        "source_pairs_equal": audit_pairs == primary_pairs,
        "txt_lines_equal": txt_lines == expected_lines,
        "txt_line_count": len(txt_lines),
    }
    (OUT / "independent-audit.json").write_text(
        json.dumps(independent_audit, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )

    website_checks = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(check_website, row): row["name"] for row in rows}
        for future in as_completed(futures):
            website_checks.append(future.result())
    website_checks.sort(key=lambda item: next(row["source_position"] for row in rows if row["name"] == item["name"]))
    (OUT / "website-checks.json").write_text(
        json.dumps(website_checks, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )

    override_ledger = []
    for row in rows:
        if row["name"] in OVERRIDES and row["website_provenance"] == "reviewed_override":
            override_ledger.append({"name": row["name"], **OVERRIDES[row["name"]]})
    (OUT / "override-ledger.json").write_text(
        json.dumps(override_ledger, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )

    names = [row["name"].casefold() for row in rows]
    source_websites = [root_url(row["source_website"]) for row in rows]
    final_domains = [urlparse(row["website"]).netloc.casefold().removeprefix("www.") for row in rows]
    ids = [row["source_id"] for row in rows]
    duplicate_names = {name: count for name, count in Counter(names).items() if count > 1}
    duplicate_source_websites = {site: count for site, count in Counter(source_websites).items() if count > 1}
    duplicate_final_domains = {site: count for site, count in Counter(final_domains).items() if count > 1}
    missing_names = [row for row in rows if not row["name"]]
    missing_websites = [row for row in rows if not row["website"]]
    invalid_websites = [
        row
        for row in rows
        if not row["website"].startswith("https://") or not urlparse(row["website"]).netloc
    ]
    source_info = selected["summary"]["source_info"]
    contract = selected["summary"]["source_contract"]
    errors = []
    if len(rows) != source_info["dd_count"]:
        errors.append("parsed row count does not match source dd count")
    if source_info["dd_count"] != source_info["image_count"]:
        errors.append("company card and logo counts differ")
    if source_info["dd_count"] != source_info["text_link_count"]:
        errors.append("company card and textual link counts differ")
    if source_info["all_company_anchor_count"] != source_info["dd_count"] * 2:
        errors.append("each company card does not contain exactly two links")
    if source_info["anchor_inconsistencies"]:
        errors.append("company card logo and text links disagree")
    if source_info["empty_entries"]:
        errors.append("source contains empty company entries")
    if duplicate_names:
        errors.append("duplicate company names")
    if duplicate_source_websites:
        errors.append("duplicate source websites")
    if duplicate_final_domains:
        errors.append("duplicate final domains")
    if len(ids) != len(set(ids)):
        errors.append("duplicate source identities")
    if missing_names or missing_websites or invalid_websites:
        errors.append("missing or invalid normalized fields")
    if not contract["page_invokes_getIconList"] or not contract["endpoint_in_script"]:
        errors.append("page-to-endpoint contract not proven")
    if contract["pagination_terms_near_function"]:
        errors.append("unexpected pagination term in getIconList function")
    if snapshot_comparison["status"] != "PASS":
        errors.append("two source snapshots differ")
    if independent_audit["status"] != "PASS":
        errors.append("independent audit failed")

    validation = {
        "source_page": PAGE_URL,
        "source_endpoint": ENDPOINT,
        "scope": "Only selected portfolio companies showcased by the source page",
        "status": "PASS" if not errors else "FAIL",
        "source_company_card_count": source_info["dd_count"],
        "source_logo_count": source_info["image_count"],
        "source_text_link_count": source_info["text_link_count"],
        "source_all_company_anchor_count": source_info["all_company_anchor_count"],
        "output_line_count": len(rows),
        "unique_source_id_count": len(set(ids)),
        "unique_name_count": len(set(names)),
        "unique_source_website_count": len(set(source_websites)),
        "unique_final_domain_count": len(set(final_domains)),
        "missing_name_count": len(missing_names),
        "missing_website_count": len(missing_websites),
        "invalid_website_count": len(invalid_websites),
        "duplicate_name_groups": duplicate_names,
        "duplicate_source_website_groups": duplicate_source_websites,
        "duplicate_final_domain_groups": duplicate_final_domains,
        "reviewed_override_count": len(override_ledger),
        "website_http_ok_count": sum(bool(item.get("ok_http")) for item in website_checks),
        "website_check_error_count": sum(not bool(item.get("ok_http")) for item in website_checks),
        "source_contract": contract,
        "snapshot_comparison": snapshot_comparison,
        "independent_audit": independent_audit,
        "errors": errors,
        "notes": [
            "The page explicitly states that only selected portfolio companies are showcased.",
            "The page invokes one unpaginated POST request with an empty search key; the response supplies the full displayed list.",
            "HTTP availability is recorded separately and is not treated as company-set completeness.",
        ],
    }
    (OUT / "validation.json").write_text(
        json.dumps(validation, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )

    manifest = {"generated_at_epoch": time.time(), "status": validation["status"], "files": {}}
    for path in sorted(OUT.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            manifest["files"][str(path.relative_to(OUT))] = {
                "bytes": path.stat().st_size,
                "sha256": sha256(path.read_bytes()),
            }
    (OUT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )

    print("VALIDATION_SUMMARY_START")
    print(
        json.dumps(
            {
                "status": validation["status"],
                "source_company_card_count": validation["source_company_card_count"],
                "output_line_count": validation["output_line_count"],
                "unique_name_count": validation["unique_name_count"],
                "unique_final_domain_count": validation["unique_final_domain_count"],
                "reviewed_override_count": validation["reviewed_override_count"],
                "website_http_ok_count": validation["website_http_ok_count"],
                "snapshot_comparison": snapshot_comparison["status"],
                "independent_audit": independent_audit["status"],
                "errors": errors,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    print("VALIDATION_SUMMARY_END")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        OUT.mkdir(parents=True, exist_ok=True)
        error = {"status": "FATAL", "error": repr(exc), "traceback": traceback.format_exc()}
        (OUT / "fatal-error.json").write_text(
            json.dumps(error, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )
        print(error["traceback"])
        raise
