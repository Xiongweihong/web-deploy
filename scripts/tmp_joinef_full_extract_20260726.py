from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
import time
import traceback
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

TARGET = "https://www.joinef.com/portfolio/"
ORIGIN = "https://www.joinef.com"
AJAX = f"{ORIGIN}/wp-admin/admin-ajax.php"
SITEMAPS = [
    f"{ORIGIN}/company-sitemap.xml",
    f"{ORIGIN}/wp-sitemap-posts-company-1.xml",
]
OUT = Path("artifact")
RAW = OUT / "raw"
NORMALIZED = OUT / "normalized"
UA = "Mozilla/5.0 (compatible; joinef-evidence-crawler/2.0)"
WORKERS = 8


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def save_bytes(path: Path, data: bytes, meta: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    payload = dict(meta)
    payload.update({"bytes": len(data), "sha256": sha256(data)})
    write_json(path.with_suffix(path.suffix + ".meta.json"), payload)


def normalize_url(value: object) -> str:
    text = clean(value)
    if not text:
        return ""
    if text.startswith("//"):
        text = "https:" + text
    elif "://" not in text:
        text = "https://" + text
    elif text.startswith("http://"):
        text = "https://" + text[7:]
    parsed = urlparse(text)
    host = parsed.netloc.casefold().removeprefix("www.")
    if not host or "." not in host:
        return ""
    path = parsed.path or ""
    if path == "/":
        path = ""
    else:
        path = path.rstrip("/")
    return urlunparse(("https", host, path, "", "", ""))


def request_get(url: str, *, attempts: int = 6) -> tuple[bytes, dict]:
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(
                url,
                timeout=120,
                allow_redirects=True,
                headers={"User-Agent": UA, "Accept": "text/html,application/xml,application/json;q=0.9,*/*;q=0.8"},
            )
            response.raise_for_status()
            return response.content, {
                "requested_url": response.request.url,
                "final_url": response.url,
                "method": "GET",
                "status": response.status_code,
                "content_type": response.headers.get("content-type"),
                "date": response.headers.get("date"),
                "etag": response.headers.get("etag"),
                "last_modified": response.headers.get("last-modified"),
                "fetched_at_epoch": time.time(),
            }
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < attempts:
                time.sleep(min(30, 2**attempt))
    raise RuntimeError(f"GET failed after {attempts} attempts: {url}: {last}")


def request_post(data: dict, *, attempts: int = 6) -> tuple[bytes, dict]:
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = requests.post(
                AJAX,
                data=data,
                timeout=120,
                allow_redirects=True,
                headers={
                    "User-Agent": UA,
                    "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
                    "Origin": ORIGIN,
                    "Referer": TARGET,
                    "X-Requested-With": "XMLHttpRequest",
                },
            )
            response.raise_for_status()
            return response.content, {
                "requested_url": response.request.url,
                "final_url": response.url,
                "method": "POST",
                "request_data": data,
                "status": response.status_code,
                "content_type": response.headers.get("content-type"),
                "date": response.headers.get("date"),
                "fetched_at_epoch": time.time(),
                "attempt": attempt,
            }
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < attempts:
                time.sleep(min(30, 2**attempt))
    raise RuntimeError(f"POST failed after {attempts} attempts: {data.get('action')} {data.get('company') or data.get('page')}: {last}")


def parse_php_variables(html: bytes) -> dict:
    soup = BeautifulSoup(html, "lxml")
    script = soup.find("script", id="main-js-extra")
    text = script.get_text() if script else ""
    match = re.search(r"var php_variables = (\{.*?\});\s*var filter_settings", text, re.S)
    if not match:
        raise ValueError("php_variables not found")
    return json.loads(match.group(1))


def parse_tiles(html: bytes, *, section_hint: str | None = None) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    rows: list[dict] = []
    for node in soup.select(".tile--company .tile__link[data-companyslug]"):
        tile = node.find_parent("div", class_=lambda value: value and "tile--company" in value)
        featured = bool(tile and "tile--company--featured" in (tile.get("class") or []))
        section = "featured" if featured else (section_hint or "all")
        rows.append(
            {
                "name": clean(node.get("data-companyname") or node.get_text(" ", strip=True)),
                "slug": clean(node.get("data-companyslug")),
                "index": clean(node.get("data-index")) or "0",
                "section": section,
                "featured": featured,
            }
        )
    return rows


def parse_sitemap(xml: bytes) -> list[str]:
    soup = BeautifulSoup(xml, "xml")
    return [
        clean(node.get_text())
        for node in soup.find_all("loc")
        if "/companies/" in clean(node.get_text())
    ]


def capture_snapshot(number: int) -> dict:
    root = RAW / f"snapshot-{number:02d}"
    page_raw, page_meta = request_get(TARGET)
    save_bytes(root / "portfolio.html", page_raw, page_meta)
    variables = parse_php_variables(page_raw)
    write_json(root / "php-variables.json", variables)

    initial_rows = parse_tiles(page_raw)
    featured_rows = [row for row in initial_rows if row["featured"]]
    all_rows = [row for row in initial_rows if not row["featured"]]
    max_page = int(variables.get("max_page") or 1)
    ajax_page_sizes: list[int] = []
    terminal_empty_page: int | None = None

    for page_number in range(1, max_page + 1):
        data = {
            "action": "loadmore",
            "query": variables["posts"],
            "page": str(page_number),
            "format": "default",
        }
        raw, meta = request_post(data)
        save_bytes(root / "loadmore" / f"page-{page_number:03d}.html", raw, meta)
        rows = parse_tiles(raw, section_hint="all")
        ajax_page_sizes.append(len(rows))
        if not rows:
            terminal_empty_page = page_number
            break
        all_rows.extend(rows)

    entries = featured_rows + all_rows
    for position, entry in enumerate(entries, start=1):
        entry["source_position"] = position

    slug_counts = Counter(entry["slug"] for entry in entries)
    name_counts = Counter(entry["name"].casefold() for entry in entries)
    summary = {
        "snapshot": number,
        "captured_at_epoch": time.time(),
        "source_page_sha256": page_meta["sha256"] if "sha256" in page_meta else sha256(page_raw),
        "current_page": int(variables.get("current_page") or 1),
        "max_page": max_page,
        "featured_count": len(featured_rows),
        "initial_all_count": len([row for row in initial_rows if not row["featured"]]),
        "ajax_page_sizes": ajax_page_sizes,
        "terminal_empty_page": terminal_empty_page,
        "all_nonfeatured_count": len(all_rows),
        "total_entry_count": len(entries),
        "unique_slug_count": len(slug_counts),
        "duplicate_slug_groups": {key: value for key, value in slug_counts.items() if value > 1},
        "duplicate_name_groups": {key: value for key, value in name_counts.items() if value > 1},
    }
    write_json(root / "entries.json", entries)
    write_json(root / "snapshot-summary.json", summary)
    return {"entries": entries, "summary": summary, "root": root, "variables": variables}


def parse_detail(html: bytes, entry: dict) -> dict:
    soup = BeautifulSoup(html, "lxml")
    company = soup.select_one(".company[data-companyid]")
    overlay_name = soup.select_one(".company__name") or soup.select_one(".companyoverlay__name")
    website_raw = ""
    for row in soup.select(".meta__row"):
        label = row.select_one(".meta__row__name")
        if label and clean(label.get_text(" ", strip=True)).casefold() == "website":
            anchor = row.find("a", href=True)
            if anchor:
                website_raw = clean(anchor.get("href"))
                break
    if not website_raw:
        internal_host = urlparse(ORIGIN).netloc.casefold()
        excluded_hosts = {"linkedin.com", "www.linkedin.com", "twitter.com", "x.com", internal_host}
        for anchor in soup.find_all("a", href=True):
            href = urljoin(TARGET, clean(anchor.get("href")))
            host = urlparse(href).netloc.casefold()
            if host and host not in excluded_hosts and not host.endswith("joinef.com"):
                website_raw = href
                break
    return {
        **entry,
        "source_id": clean(company.get("data-companyid")) if company else "",
        "detail_name": clean(overlay_name.get_text(" ", strip=True)) if overlay_name else "",
        "source_website": website_raw,
        "website": normalize_url(website_raw),
        "website_domain": urlparse(normalize_url(website_raw)).netloc.casefold().removeprefix("www."),
        "detail_sha256": sha256(html),
    }


def fetch_detail(entry: dict, snapshot_root: Path) -> dict:
    data = {
        "action": "getcompany",
        "company": entry["slug"],
        "index": entry.get("index") or "0",
        "featured": "true" if entry["featured"] else "false",
    }
    raw, meta = request_post(data)
    safe_slug = re.sub(r"[^a-zA-Z0-9._-]+", "_", entry["slug"])
    path = snapshot_root / "details" / f"{entry['source_position']:04d}_{safe_slug}.html"
    save_bytes(path, raw, meta)
    row = parse_detail(raw, entry)
    row["detail_path"] = str(path.relative_to(snapshot_root))
    return row


def reconstruct_from_raw(snapshot_root: Path) -> tuple[list[dict], dict]:
    hash_errors: list[str] = []
    page_path = snapshot_root / "portfolio.html"
    page_meta = json.loads(page_path.with_suffix(".html.meta.json").read_text(encoding="utf-8"))
    if sha256(page_path.read_bytes()) != page_meta.get("sha256"):
        hash_errors.append(str(page_path.relative_to(snapshot_root)))
    initial = parse_tiles(page_path.read_bytes())
    featured = [row for row in initial if row["featured"]]
    all_rows = [row for row in initial if not row["featured"]]
    page_sizes = []
    for path in sorted((snapshot_root / "loadmore").glob("page-*.html")):
        meta = json.loads(path.with_suffix(".html.meta.json").read_text(encoding="utf-8"))
        if sha256(path.read_bytes()) != meta.get("sha256"):
            hash_errors.append(str(path.relative_to(snapshot_root)))
        parsed = parse_tiles(path.read_bytes(), section_hint="all")
        page_sizes.append(len(parsed))
        if parsed:
            all_rows.extend(parsed)
    entries = featured + all_rows
    for position, entry in enumerate(entries, start=1):
        entry["source_position"] = position
    return entries, {"hash_errors": hash_errors, "page_sizes": page_sizes}


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    RAW.mkdir(parents=True)
    NORMALIZED.mkdir(parents=True)

    snapshots = [capture_snapshot(1), capture_snapshot(2)]
    snapshot_maps = [
        {entry["slug"]: (entry["name"], entry["section"], entry["source_position"]) for entry in snapshot["entries"]}
        for snapshot in snapshots
    ]
    comparison = {
        "status": "PASS" if snapshot_maps[0] == snapshot_maps[1] else "FAIL",
        "snapshot_1_count": len(snapshot_maps[0]),
        "snapshot_2_count": len(snapshot_maps[1]),
        "added_slugs": sorted(set(snapshot_maps[1]) - set(snapshot_maps[0])),
        "removed_slugs": sorted(set(snapshot_maps[0]) - set(snapshot_maps[1])),
        "changed_slugs": sorted(
            slug for slug in set(snapshot_maps[0]) & set(snapshot_maps[1]) if snapshot_maps[0][slug] != snapshot_maps[1][slug]
        ),
        "snapshot_1_summary": snapshots[0]["summary"],
        "snapshot_2_summary": snapshots[1]["summary"],
    }
    write_json(OUT / "snapshot-comparison.json", comparison)

    selected = snapshots[-1]
    entries = selected["entries"]
    snapshot_root = selected["root"]
    details: list[dict] = []
    failures: list[dict] = []
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = {executor.submit(fetch_detail, entry, snapshot_root): entry for entry in entries}
        for completed, future in enumerate(as_completed(futures), start=1):
            entry = futures[future]
            try:
                row = future.result()
                with lock:
                    details.append(row)
            except Exception as exc:  # noqa: BLE001
                with lock:
                    failures.append({"slug": entry["slug"], "name": entry["name"], "error": repr(exc)})
            if completed % 50 == 0:
                print(f"details completed: {completed}/{len(entries)}; failures={len(failures)}", flush=True)

    details.sort(key=lambda row: row["source_position"])
    write_json(OUT / "detail-failures.json", failures)
    if failures:
        raise RuntimeError(f"detail fetch failures: {len(failures)}")

    sitemap_sets = []
    sitemap_info = []
    for index, url in enumerate(SITEMAPS, start=1):
        raw, meta = request_get(url)
        path = RAW / "sitemaps" / f"company-sitemap-{index}.xml"
        save_bytes(path, raw, meta)
        urls = parse_sitemap(raw)
        slugs = [urlparse(url).path.rstrip("/").split("/")[-1] for url in urls]
        sitemap_sets.append(set(slugs))
        sitemap_info.append({"url": url, "url_count": len(urls), "unique_slug_count": len(set(slugs)), "sha256": sha256(raw)})

    alphabetical = sorted(details, key=lambda row: (row["name"].casefold(), row["slug"]))
    txt_lines = [f'{row["name"]} + {row["website"]}' for row in alphabetical]
    txt_path = NORMALIZED / "joinef-portfolio-companies.txt"
    json_path = NORMALIZED / "joinef-portfolio-companies.json"
    txt_path.write_text("\n".join(txt_lines) + "\n", encoding="utf-8")
    write_json(json_path, alphabetical)
    write_json(NORMALIZED / "joinef-portfolio-companies-source-order.json", details)

    slug_counts = Counter(row["slug"] for row in details)
    id_counts = Counter(row["source_id"] for row in details if row["source_id"])
    name_counts = Counter(row["name"].casefold() for row in details)
    domain_counts = Counter(row["website_domain"] for row in details if row["website_domain"])
    missing_ids = [row for row in details if not row["source_id"]]
    missing_names = [row for row in details if not row["name"]]
    missing_websites = [row for row in details if not row["website"]]
    invalid_websites = [
        row for row in details if row["website"] and (urlparse(row["website"]).scheme != "https" or not urlparse(row["website"]).netloc)
    ]
    name_mismatches = [
        row for row in details if row["detail_name"] and row["detail_name"].casefold() != row["name"].casefold()
    ]
    source_slugs = set(row["slug"] for row in details)

    reconstructed_entries, reconstructed_info = reconstruct_from_raw(snapshot_root)
    reconstructed_details = []
    detail_hash_errors = []
    for path in sorted((snapshot_root / "details").glob("*.html")):
        meta = json.loads(path.with_suffix(".html.meta.json").read_text(encoding="utf-8"))
        if sha256(path.read_bytes()) != meta.get("sha256"):
            detail_hash_errors.append(str(path.relative_to(snapshot_root)))
        request_data = meta.get("request_data") or {}
        entry = next((item for item in reconstructed_entries if item["slug"] == request_data.get("company")), None)
        if entry:
            reconstructed_details.append(parse_detail(path.read_bytes(), entry))
    reconstructed_details.sort(key=lambda row: row["source_position"])
    primary_map = {
        row["slug"]: (row["source_id"], row["name"], row["website"], row["section"], row["source_position"])
        for row in details
    }
    audit_map = {
        row["slug"]: (row["source_id"], row["name"], row["website"], row["section"], row["source_position"])
        for row in reconstructed_details
    }
    actual_txt = [line for line in txt_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    expected_txt = [f'{row["name"]} + {row["website"]}' for row in sorted(reconstructed_details, key=lambda row: (row["name"].casefold(), row["slug"]))]
    audit_status = "PASS" if (
        not reconstructed_info["hash_errors"]
        and not detail_hash_errors
        and primary_map == audit_map
        and actual_txt == expected_txt
        and all(sitemap_set == source_slugs for sitemap_set in sitemap_sets)
        and reconstructed_info["page_sizes"][-1:] == [0]
    ) else "FAIL"
    audit = {
        "status": audit_status,
        "method": "Reparse preserved homepage, load-more HTML, company-detail HTML and two company sitemaps without using normalized JSON as a data source.",
        "source_page_and_loadmore_hash_errors": reconstructed_info["hash_errors"],
        "detail_hash_errors": detail_hash_errors,
        "reconstructed_entry_count": len(reconstructed_entries),
        "reconstructed_detail_count": len(reconstructed_details),
        "primary_and_audit_maps_equal": primary_map == audit_map,
        "txt_lines_equal": actual_txt == expected_txt,
        "txt_line_count": len(actual_txt),
        "sitemap_sets_equal_source_slugs": [sitemap_set == source_slugs for sitemap_set in sitemap_sets],
        "terminal_empty_page": reconstructed_info["page_sizes"][-1:] == [0],
        "page_sizes": reconstructed_info["page_sizes"],
    }
    write_json(OUT / "independent-audit.json", audit)

    errors = []
    if comparison["status"] != "PASS": errors.append("two source snapshots differ")
    if selected["summary"]["total_entry_count"] != 523: errors.append("source entry count is not 523")
    if selected["summary"]["unique_slug_count"] != selected["summary"]["total_entry_count"]: errors.append("duplicate source slugs")
    if selected["summary"]["terminal_empty_page"] is None: errors.append("no terminal empty page")
    if any(sitemap_set != source_slugs for sitemap_set in sitemap_sets): errors.append("sitemap slug set mismatch")
    if missing_ids: errors.append("missing company IDs")
    if missing_names: errors.append("missing company names")
    if missing_websites: errors.append("missing company websites")
    if invalid_websites: errors.append("invalid company websites")
    if name_mismatches: errors.append("tile/detail name mismatches")
    if audit_status != "PASS": errors.append("independent audit failed")

    validation = {
        "status": "PASS" if not errors else "NEEDS_REVIEW",
        "source_url": TARGET,
        "captured_at_epoch": time.time(),
        "featured_count": selected["summary"]["featured_count"],
        "initial_nonfeatured_count": selected["summary"]["initial_all_count"],
        "ajax_page_sizes": selected["summary"]["ajax_page_sizes"],
        "terminal_empty_page": selected["summary"]["terminal_empty_page"],
        "nonfeatured_count": selected["summary"]["all_nonfeatured_count"],
        "total_company_count": len(details),
        "unique_slug_count": len(slug_counts),
        "unique_source_id_count": len(id_counts),
        "unique_name_count": len(name_counts),
        "unique_website_domain_count": len(domain_counts),
        "output_line_count": len(txt_lines),
        "missing_id_count": len(missing_ids),
        "missing_name_count": len(missing_names),
        "missing_website_count": len(missing_websites),
        "invalid_website_count": len(invalid_websites),
        "name_mismatch_count": len(name_mismatches),
        "duplicate_slug_groups": {key: value for key, value in slug_counts.items() if value > 1},
        "duplicate_source_id_groups": {key: value for key, value in id_counts.items() if value > 1},
        "duplicate_name_groups": {key: value for key, value in name_counts.items() if value > 1},
        "duplicate_domain_groups": {key: value for key, value in domain_counts.items() if value > 1},
        "sitemaps": sitemap_info,
        "sitemap_sets_equal_source": [sitemap_set == source_slugs for sitemap_set in sitemap_sets],
        "snapshot_comparison": comparison,
        "independent_audit": audit,
        "errors": errors,
        "notes": [
            "Output website values are the explicit Website links supplied by Entrepreneurs First company-detail overlays, normalized to HTTPS and stripped of query/fragment/trailing slash.",
            "Duplicate website domains, if any, are reported but are not automatically treated as errors because acquisitions or related entities can legitimately share a domain.",
        ],
    }
    write_json(OUT / "validation.json", validation)
    write_json(OUT / "review-queue.json", {
        "missing_ids": missing_ids,
        "missing_names": missing_names,
        "missing_websites": missing_websites,
        "invalid_websites": invalid_websites,
        "name_mismatches": name_mismatches,
        "duplicate_names": validation["duplicate_name_groups"],
        "duplicate_domains": validation["duplicate_domain_groups"],
    })

    manifest = {"generated_at_epoch": time.time(), "status": validation["status"], "files": {}}
    for path in sorted(OUT.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            manifest["files"][str(path.relative_to(OUT))] = {"bytes": path.stat().st_size, "sha256": sha256(path.read_bytes())}
    write_json(OUT / "manifest.json", manifest)

    print("VALIDATION_SUMMARY_START")
    print(json.dumps({
        "status": validation["status"],
        "featured_count": validation["featured_count"],
        "nonfeatured_count": validation["nonfeatured_count"],
        "total_company_count": validation["total_company_count"],
        "unique_slug_count": validation["unique_slug_count"],
        "unique_source_id_count": validation["unique_source_id_count"],
        "unique_name_count": validation["unique_name_count"],
        "unique_website_domain_count": validation["unique_website_domain_count"],
        "missing_website_count": validation["missing_website_count"],
        "duplicate_name_groups": validation["duplicate_name_groups"],
        "duplicate_domain_groups": validation["duplicate_domain_groups"],
        "sitemap_sets_equal_source": validation["sitemap_sets_equal_source"],
        "snapshot_comparison": comparison["status"],
        "independent_audit": audit["status"],
        "errors": errors,
    }, ensure_ascii=False, indent=2, sort_keys=True))
    print("VALIDATION_SUMMARY_END")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        OUT.mkdir(parents=True, exist_ok=True)
        write_json(OUT / "fatal-error.json", {"error": repr(exc), "traceback": traceback.format_exc()})
        raise
