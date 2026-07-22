from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
import traceback
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

BASE = "https://obvious.com"
PORTFOLIO_URL = BASE + "/portfolio/"
AJAX_URL = BASE + "/wp-admin/admin-ajax.php"
OUT = Path("artifact")
RAW = OUT / "raw"
NORMALIZED = OUT / "normalized"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalized_name(value: object) -> str:
    return clean(value).casefold()


def normalized_source_id(value: object) -> str:
    text = clean(value).lstrip("#").strip("/")
    return text.casefold()


def normalize_website(value: object) -> str:
    text = clean(value)
    if not text:
        return ""
    if text.startswith("//"):
        text = "https:" + text
    elif "://" not in text:
        text = "https://" + text
    parsed = urlparse(text)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        return ""
    # Preserve source-provided paths because some portfolio links deliberately point
    # to a product or current-brand landing page. Remove only query/fragment noise.
    path = re.sub(r"/{2,}", "/", parsed.path or "")
    path = path.rstrip("/")
    return urlunparse(
        (
            parsed.scheme.casefold(),
            parsed.netloc.casefold(),
            path,
            "",
            "",
            "",
        )
    )


def canonical_domain(value: object) -> str:
    url = normalize_website(value)
    if not url:
        return ""
    host = (urlparse(url).hostname or "").casefold().removeprefix("www.")
    return host


def save_bytes(path: Path, data: bytes, metadata: dict | None = None) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    meta = {
        "bytes": len(data),
        "sha256": sha256(data),
        "saved_at_epoch": time.time(),
    }
    if metadata:
        meta.update(metadata)
    path.with_suffix(path.suffix + ".meta.json").write_text(
        json.dumps(meta, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8"
    )
    return meta


def response_metadata(response: requests.Response, *, request_payload: dict | None = None) -> dict:
    return {
        "requested_url": response.request.url,
        "final_url": response.url,
        "method": response.request.method,
        "request_payload": request_payload,
        "status": response.status_code,
        "content_type": response.headers.get("content-type"),
        "date": response.headers.get("date"),
        "etag": response.headers.get("etag"),
        "last_modified": response.headers.get("last-modified"),
        "x_wp_total": response.headers.get("x-wp-total"),
        "x_wp_totalpages": response.headers.get("x-wp-totalpages"),
        "fetched_at_epoch": time.time(),
    }


def external_company_links(item: BeautifulSoup) -> list[str]:
    links: list[str] = []
    selectors = [
        "a.list-secondary__link[href]",
        ".list-secondary__image a[href]",
        "a[href]",
    ]
    for selector in selectors:
        for anchor in item.select(selector):
            href = clean(anchor.get("href"))
            if not href:
                continue
            parsed = urlparse(href if "://" in href else "https://" + href.lstrip("/"))
            host = (parsed.hostname or "").casefold().removeprefix("www.")
            if not host or host == "obvious.com" or host.endswith(".obvious.com"):
                continue
            if host in {
                "linkedin.com",
                "x.com",
                "twitter.com",
                "facebook.com",
                "instagram.com",
                "youtube.com",
            }:
                continue
            normalized = normalize_website(href)
            if normalized and normalized not in links:
                links.append(normalized)
        if links:
            break
    return links


def parse_metadata(item: BeautifulSoup) -> dict[str, str]:
    result: dict[str, str] = {}
    for row in item.select(".list-secondary__meta > div"):
        label_node = row.find("span")
        value_node = row.find("strong")
        label = clean(label_node.get_text(" ", strip=True) if label_node else "").rstrip(":")
        value = clean(value_node.get_text(" ", strip=True) if value_node else "")
        if label and value:
            result[label.casefold()] = value
    return result


def parse_item(item: BeautifulSoup, *, page_number: int, source_order: int) -> dict:
    title_node = item.select_one(".list-secondary__title strong")
    if title_node is None:
        title_node = item.select_one(".list-secondary__title")
    name = clean(title_node.get_text(" ", strip=True) if title_node else "")
    source_id = normalized_source_id(item.get("data-id"))
    links = external_company_links(item)
    website = links[0] if links else ""
    metadata = parse_metadata(item)
    description_node = item.select_one(".list-secondary__text")
    description = clean(description_node.get_text(" ", strip=True) if description_node else "")
    return {
        "source_id": source_id,
        "name": name,
        "website": website,
        "raw_source_links": links,
        "canonical_domain": canonical_domain(website),
        "founded": metadata.get("founded"),
        "pillar": metadata.get("pillar"),
        "status": metadata.get("status"),
        "description": description,
        "page_number": page_number,
        "source_order": source_order,
        "website_provenance": "obvious_portfolio_link" if website else "missing",
    }


def parse_items_html(html: str, *, page_number: int, start_order: int) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    items = soup.select(".list-secondary__item")
    rows = [
        parse_item(item, page_number=page_number, source_order=start_order + index)
        for index, item in enumerate(items)
    ]
    return rows


def new_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (compatible; obvious-evidence-crawler/2.0)",
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.8",
            "Referer": PORTFOLIO_URL,
        }
    )
    return session


def capture_snapshot(label: str) -> dict:
    session = new_session()
    snapshot_dir = RAW / label
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    response = session.get(PORTFOLIO_URL, timeout=90, allow_redirects=True)
    response.raise_for_status()
    page_path = snapshot_dir / "page-001.html"
    save_bytes(page_path, response.content, response_metadata(response))
    page_soup = BeautifulSoup(response.content, "lxml")
    rows = parse_items_html(response.text, page_number=1, start_order=0)

    load_button = page_soup.select_one('a.btn-more[data-load="companies"][data-page]')
    next_page = int(load_button.get("data-page")) if load_button and clean(load_button.get("data-page")).isdigit() else None
    total_posts: int | None = None
    page_summaries = [
        {
            "page": 1,
            "record_count": len(rows),
            "source_ids": [row["source_id"] for row in rows],
            "sha256": sha256(response.content),
            "source": "portfolio_html",
        }
    ]
    raw_files = [str(page_path.relative_to(OUT))]
    seen_page_hashes = {sha256(response.content)}
    terminal: dict | None = None

    for _guard in range(20):
        if next_page is None:
            terminal = {"type": "no_load_more_marker", "after_page": page_summaries[-1]["page"]}
            break
        payload = {
            "action": "portfolio_load_more",
            "paged": str(next_page),
            "search": "",
        }
        ajax = session.post(
            AJAX_URL,
            data=payload,
            timeout=90,
            allow_redirects=True,
            headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json, text/javascript, */*; q=0.01"},
        )
        ajax.raise_for_status()
        ajax_path = snapshot_dir / f"page-{next_page:03d}.json"
        save_bytes(ajax_path, ajax.content, response_metadata(ajax, request_payload=payload))
        raw_files.append(str(ajax_path.relative_to(OUT)))
        body_hash = sha256(ajax.content)
        if body_hash in seen_page_hashes:
            raise RuntimeError(f"{label}: repeated page response body at page {next_page}")
        seen_page_hashes.add(body_hash)
        payload_json = ajax.json()
        if not isinstance(payload_json, dict) or payload_json.get("success") is not True:
            raise RuntimeError(f"{label}: invalid AJAX response at page {next_page}")
        data = payload_json.get("data") or {}
        if not isinstance(data, dict):
            raise RuntimeError(f"{label}: missing data object at page {next_page}")
        current_total = data.get("total_posts")
        if current_total is not None:
            current_total = int(current_total)
            if total_posts is None:
                total_posts = current_total
            elif total_posts != current_total:
                raise RuntimeError(
                    f"{label}: total_posts changed during crawl: {total_posts} -> {current_total}"
                )
        articles = data.get("articles") or ""
        page_rows = parse_items_html(
            articles,
            page_number=next_page,
            start_order=len(rows),
        )
        page_summaries.append(
            {
                "page": next_page,
                "record_count": len(page_rows),
                "source_ids": [row["source_id"] for row in page_rows],
                "sha256": body_hash,
                "source": "portfolio_ajax",
            }
        )
        rows.extend(page_rows)
        load_more_html = data.get("load_more") or ""
        load_more_soup = BeautifulSoup(load_more_html, "lxml")
        next_button = load_more_soup.select_one('a.btn-more[data-load="companies"][data-page]')
        if next_button and clean(next_button.get("data-page")).isdigit():
            proposed = int(next_button.get("data-page"))
            if proposed <= next_page:
                raise RuntimeError(f"{label}: non-increasing next page {next_page} -> {proposed}")
            next_page = proposed
        else:
            next_page = None
        if not page_rows and next_page is not None:
            raise RuntimeError(f"{label}: empty page returned while a next page marker remains")
    else:
        raise RuntimeError(f"{label}: max page guard reached without terminal proof")

    if total_posts is None:
        total_posts = len(rows)

    # Re-fetch the first page after pagination to detect a moving source snapshot.
    recheck = session.get(PORTFOLIO_URL, timeout=90, allow_redirects=True)
    recheck.raise_for_status()
    recheck_path = snapshot_dir / "page-001-recheck.html"
    save_bytes(recheck_path, recheck.content, response_metadata(recheck))
    raw_files.append(str(recheck_path.relative_to(OUT)))
    recheck_rows = parse_items_html(recheck.text, page_number=1, start_order=0)
    first_page_stable = [row["source_id"] for row in rows[: len(page_summaries[0]["source_ids"])]] == [
        row["source_id"] for row in recheck_rows
    ]

    ids = [row["source_id"] for row in rows]
    names = [normalized_name(row["name"]) for row in rows]
    duplicate_ids = sorted(source_id for source_id, count in Counter(ids).items() if source_id and count > 1)
    duplicate_names = sorted(name for name, count in Counter(names).items() if name and count > 1)
    empty_ids = [index for index, value in enumerate(ids) if not value]
    empty_names = [index for index, value in enumerate(names) if not value]

    summary = {
        "label": label,
        "captured_at_epoch": time.time(),
        "reported_total": total_posts,
        "raw_record_count": len(rows),
        "unique_id_count": len(set(ids)),
        "unique_name_count": len(set(names)),
        "duplicate_ids": duplicate_ids,
        "duplicate_normalized_names": duplicate_names,
        "empty_id_indexes": empty_ids,
        "empty_name_indexes": empty_names,
        "page_count": len(page_summaries),
        "page_sizes": [page["record_count"] for page in page_summaries],
        "pages": page_summaries,
        "terminal": terminal,
        "first_page_recheck_count": len(recheck_rows),
        "first_page_stable": first_page_stable,
        "raw_files": raw_files,
        "stable": (
            total_posts == len(rows)
            and len(set(ids)) == len(rows)
            and not duplicate_ids
            and not empty_ids
            and not empty_names
            and first_page_stable
            and terminal is not None
        ),
    }
    (snapshot_dir / "snapshot-summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    return {"rows": rows, "summary": summary, "snapshot_dir": snapshot_dir}


def audit_normalize_website(value: object) -> str:
    text = clean(value)
    if not text:
        return ""
    if text.startswith("//"):
        text = "https:" + text
    elif "://" not in text:
        text = "https://" + text
    parsed = urlparse(text)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return ""
    path = re.sub(r"/{2,}", "/", parsed.path or "").rstrip("/")
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", "", ""))


def audit_parse_fragment(html: str, page_number: int, start_order: int) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    rows = []
    for index, item in enumerate(soup.select("div.list-secondary__item")):
        title = item.select_one("div.list-secondary__title > strong")
        name = clean(title.get_text(" ", strip=True) if title else "")
        source_id = clean(item.get("data-id")).lstrip("#").casefold()
        link = item.select_one("a.list-secondary__link[href]")
        if link is None:
            link = item.select_one("div.list-secondary__image a[href]")
        website = audit_normalize_website(link.get("href") if link else "")
        rows.append(
            {
                "source_id": source_id,
                "name": name,
                "website": website,
                "page_number": page_number,
                "source_order": start_order + index,
            }
        )
    return rows


def independent_audit(snapshot: dict, normalized_rows: list[dict], txt_path: Path) -> dict:
    raw_hash_errors: list[str] = []
    audit_rows: list[dict] = []
    page_number = 1
    for relative in snapshot["summary"]["raw_files"]:
        path = OUT / relative
        if path.name == "page-001-recheck.html":
            continue
        meta_path = path.with_suffix(path.suffix + ".meta.json")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        actual_hash = sha256(path.read_bytes())
        if actual_hash != meta.get("sha256"):
            raw_hash_errors.append(relative)
        if path.suffix == ".html":
            html = path.read_text(encoding="utf-8")
        else:
            payload = json.loads(path.read_bytes())
            html = ((payload.get("data") or {}).get("articles") or "")
        page_rows = audit_parse_fragment(html, page_number, len(audit_rows))
        audit_rows.extend(page_rows)
        page_number += 1

    primary_map = {
        row["source_id"]: (row["name"], row["website"], row["source_order"])
        for row in normalized_rows
    }
    audit_map = {
        row["source_id"]: (row["name"], row["website"], row["source_order"])
        for row in audit_rows
    }
    expected_txt = [f'{row["name"]} + {row["website"]}' for row in normalized_rows]
    actual_txt = [line for line in txt_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    status = (
        "PASS"
        if not raw_hash_errors
        and primary_map == audit_map
        and expected_txt == actual_txt
        and len(audit_rows) == snapshot["summary"]["reported_total"]
        else "FAIL"
    )
    return {
        "status": status,
        "raw_hash_errors": raw_hash_errors,
        "reconstructed_record_count": len(audit_rows),
        "reconstructed_unique_id_count": len({row["source_id"] for row in audit_rows}),
        "source_id_maps_equal": primary_map == audit_map,
        "txt_lines_equal": expected_txt == actual_txt,
        "txt_line_count": len(actual_txt),
    }


def website_probe(row: dict) -> dict:
    url = row["website"]
    headers = {"User-Agent": "Mozilla/5.0 (compatible; website-validation/1.0)"}
    try:
        response = requests.get(url, timeout=(8, 15), allow_redirects=True, stream=True, headers=headers)
        result = {
            "source_id": row["source_id"],
            "name": row["name"],
            "website": url,
            "status": response.status_code,
            "final_url": response.url,
            "final_domain": canonical_domain(response.url),
            "content_type": response.headers.get("content-type"),
            "ok": response.status_code < 500,
        }
        response.close()
        return result
    except Exception as exc:
        return {
            "source_id": row["source_id"],
            "name": row["name"],
            "website": url,
            "ok": False,
            "error": repr(exc),
        }


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    NORMALIZED.mkdir(parents=True)

    snapshot_one = capture_snapshot("snapshot-01")
    time.sleep(2)
    snapshot_two = capture_snapshot("snapshot-02")

    # Preserve source order in JSON, but make the requested text list alphabetical.
    source_rows = snapshot_one["rows"]
    normalized_rows = sorted(
        source_rows,
        key=lambda row: (row["name"].casefold(), row["source_id"]),
    )

    txt_path = NORMALIZED / "obvious-portfolio-companies.txt"
    json_path = NORMALIZED / "obvious-portfolio-companies.json"
    txt_path.write_text(
        "\n".join(f'{row["name"]} + {row["website"]}' for row in normalized_rows) + "\n",
        encoding="utf-8",
    )
    json_path.write_text(
        json.dumps(normalized_rows, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )

    map_one = {
        row["source_id"]: (row["name"], row["website"], row["source_order"])
        for row in snapshot_one["rows"]
    }
    map_two = {
        row["source_id"]: (row["name"], row["website"], row["source_order"])
        for row in snapshot_two["rows"]
    }
    snapshot_comparison = {
        "status": "PASS" if map_one == map_two else "FAIL",
        "snapshot_01_count": len(map_one),
        "snapshot_02_count": len(map_two),
        "maps_equal": map_one == map_two,
        "added_ids": sorted(set(map_two) - set(map_one)),
        "removed_ids": sorted(set(map_one) - set(map_two)),
        "changed": {
            source_id: {"snapshot_01": map_one[source_id], "snapshot_02": map_two[source_id]}
            for source_id in sorted(set(map_one) & set(map_two))
            if map_one[source_id] != map_two[source_id]
        },
    }
    (OUT / "snapshot-comparison.json").write_text(
        json.dumps(snapshot_comparison, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )

    names = [normalized_name(row["name"]) for row in normalized_rows]
    source_ids = [row["source_id"] for row in normalized_rows]
    domains = [row["canonical_domain"] for row in normalized_rows if row["canonical_domain"]]
    duplicate_name_groups = {
        name: [row for row in normalized_rows if normalized_name(row["name"]) == name]
        for name, count in Counter(names).items()
        if name and count > 1
    }
    duplicate_domain_groups = {
        domain: [row for row in normalized_rows if row["canonical_domain"] == domain]
        for domain, count in Counter(domains).items()
        if domain and count > 1
    }
    missing_names = [row for row in normalized_rows if not row["name"]]
    missing_websites = [row for row in normalized_rows if not row["website"]]
    invalid_websites = []
    for row in normalized_rows:
        parsed = urlparse(row["website"])
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            invalid_websites.append(row)
        if canonical_domain(row["website"]) in {"obvious.com", "jobs.obvious.com"}:
            invalid_websites.append(row)

    audit = independent_audit(snapshot_one, normalized_rows, txt_path)
    (OUT / "independent-audit.json").write_text(
        json.dumps(audit, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )

    website_checks: list[dict] = []
    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = {executor.submit(website_probe, row): row for row in normalized_rows}
        for future in as_completed(futures):
            website_checks.append(future.result())
    website_checks.sort(key=lambda row: (row["name"].casefold(), row["source_id"]))
    (OUT / "website-checks.json").write_text(
        json.dumps(website_checks, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )

    hard_errors: list[str] = []
    if not snapshot_one["summary"]["stable"]:
        hard_errors.append("snapshot-01 failed source completeness invariants")
    if not snapshot_two["summary"]["stable"]:
        hard_errors.append("snapshot-02 failed source completeness invariants")
    if snapshot_comparison["status"] != "PASS":
        hard_errors.append("two complete snapshots differ")
    if len(source_ids) != len(set(source_ids)):
        hard_errors.append("duplicate normalized source IDs")
    if missing_names:
        hard_errors.append("missing company names")
    if missing_websites:
        hard_errors.append("missing company websites")
    if invalid_websites:
        hard_errors.append("invalid company websites")
    if duplicate_name_groups:
        hard_errors.append("duplicate normalized company names")
    if audit["status"] != "PASS":
        hard_errors.append("independent reconstruction audit failed")

    validation = {
        "source": PORTFOLIO_URL,
        "captured_at_epoch": time.time(),
        "status": "PASS" if not hard_errors else "NEEDS_REVIEW",
        "snapshot_01": snapshot_one["summary"],
        "snapshot_02": snapshot_two["summary"],
        "snapshot_comparison": snapshot_comparison,
        "reported_total": snapshot_one["summary"]["reported_total"],
        "raw_record_count": snapshot_one["summary"]["raw_record_count"],
        "unique_source_id_count": len(set(source_ids)),
        "unique_company_name_count": len(set(names)),
        "unique_domain_count": len(set(domains)),
        "output_line_count": len(normalized_rows),
        "missing_name_count": len(missing_names),
        "missing_website_count": len(missing_websites),
        "invalid_website_count": len(invalid_websites),
        "duplicate_name_group_count": len(duplicate_name_groups),
        "duplicate_domain_group_count": len(duplicate_domain_groups),
        "duplicate_names": duplicate_name_groups,
        "duplicate_domains": duplicate_domain_groups,
        "website_check_ok_count": sum(1 for row in website_checks if row.get("ok")),
        "website_check_non_ok_count": sum(1 for row in website_checks if not row.get("ok")),
        "independent_audit": audit,
        "hard_errors": hard_errors,
    }
    (OUT / "validation.json").write_text(
        json.dumps(validation, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )

    manifest = {
        "generated_at_epoch": time.time(),
        "status": validation["status"],
        "files": {},
    }
    for path in sorted(OUT.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            manifest["files"][str(path.relative_to(OUT))] = {
                "bytes": path.stat().st_size,
                "sha256": sha256(path.read_bytes()),
            }
    (OUT / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )

    summary = {
        "status": validation["status"],
        "reported_total": validation["reported_total"],
        "raw_record_count": validation["raw_record_count"],
        "unique_source_id_count": validation["unique_source_id_count"],
        "unique_company_name_count": validation["unique_company_name_count"],
        "unique_domain_count": validation["unique_domain_count"],
        "output_line_count": validation["output_line_count"],
        "page_sizes": snapshot_one["summary"]["page_sizes"],
        "terminal": snapshot_one["summary"]["terminal"],
        "missing_website_count": validation["missing_website_count"],
        "duplicate_name_group_count": validation["duplicate_name_group_count"],
        "duplicate_domain_group_count": validation["duplicate_domain_group_count"],
        "two_snapshot_comparison": snapshot_comparison["status"],
        "independent_audit": audit["status"],
        "website_check_ok_count": validation["website_check_ok_count"],
        "website_check_non_ok_count": validation["website_check_non_ok_count"],
        "hard_errors": hard_errors,
    }
    print("VALIDATION_SUMMARY_START")
    print(json.dumps(summary, indent=2, ensure_ascii=False, sort_keys=True))
    print("VALIDATION_SUMMARY_END")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        OUT.mkdir(parents=True, exist_ok=True)
        error = {
            "status": "FATAL",
            "error": repr(exc),
            "traceback": traceback.format_exc(),
        }
        (OUT / "fatal-error.json").write_text(
            json.dumps(error, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        print(error["traceback"])
