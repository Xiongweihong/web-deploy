from __future__ import annotations

import hashlib
import html
import json
import re
import shutil
import sys
import time
import traceback
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

BASE = "https://foundersfund.com"
PORTFOLIO_URL = BASE + "/portfolio/"
API_URL = BASE + "/wp-json/wp/v2/company"
OUT = Path("artifact")
RAW = OUT / "raw"
NORMALIZED = OUT / "normalized"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_title(value: object) -> str:
    if isinstance(value, dict):
        value = value.get("rendered")
    return clean(BeautifulSoup(html.unescape(str(value or "")), "lxml").get_text(" ", strip=True))


def normalize_website(value: object) -> str:
    text = html.unescape(clean(value))
    if not text:
        return ""
    # Correct malformed WordPress links such as http:///www.spacex.com/.
    text = re.sub(r"^(https?):/{3,}", r"\1://", text, flags=re.I)
    if text.startswith("//"):
        text = "https:" + text
    elif "://" not in text:
        text = "https://" + text.lstrip("/")
    parsed = urlparse(text)
    if not parsed.netloc and parsed.path.startswith("www."):
        parsed = urlparse("https://" + parsed.path)
    if not parsed.netloc:
        return ""
    host = parsed.netloc.casefold()
    if host.startswith("www."):
        host = host[4:]
    # User-facing output uses HTTPS while preserving path/query supplied by the source.
    path = parsed.path or ""
    if path == "/":
        path = ""
    normalized = urlunparse(("https", host, path.rstrip("/"), "", parsed.query, ""))
    return normalized.rstrip("/")


def request(session: requests.Session, url: str, *, params: dict | None = None, timeout: int = 60) -> requests.Response:
    last: Exception | None = None
    for attempt in range(1, 5):
        try:
            response = session.get(url, params=params, timeout=timeout, allow_redirects=True)
            if 400 <= response.status_code < 500 and response.status_code not in {408, 429}:
                response.raise_for_status()
            if response.status_code >= 500 or response.status_code in {408, 429}:
                raise RuntimeError(f"transient HTTP {response.status_code}")
            response.raise_for_status()
            return response
        except requests.HTTPError:
            raise
        except Exception as exc:
            last = exc
            if attempt == 4:
                break
            time.sleep(min(8, 2**attempt))
    raise RuntimeError(f"request failed for {url}: {last}")


def response_meta(response: requests.Response, *, params: dict | None = None) -> dict:
    return {
        "requested_url": response.request.url,
        "final_url": response.url,
        "params": params or {},
        "status": response.status_code,
        "content_type": response.headers.get("content-type"),
        "date": response.headers.get("date"),
        "etag": response.headers.get("etag"),
        "last_modified": response.headers.get("last-modified"),
        "x_wp_total": response.headers.get("x-wp-total"),
        "x_wp_totalpages": response.headers.get("x-wp-totalpages"),
        "bytes": len(response.content),
        "sha256": digest(response.content),
        "fetched_at_epoch": time.time(),
    }


def save_response(response: requests.Response, path: Path, *, params: dict | None = None) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(response.content)
    meta = response_meta(response, params=params)
    path.with_suffix(path.suffix + ".meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    return meta


def portfolio_cards(raw: bytes) -> list[dict]:
    soup = BeautifulSoup(raw, "lxml")
    links = []
    for anchor in soup.find_all("a", href=True):
        match = re.search(r"/company/([^/?#]+)/?", anchor.get("href", ""), re.I)
        if not match:
            continue
        slug = match.group(1).casefold()
        name = ""
        node = anchor
        for _ in range(8):
            node = node.parent
            if not node:
                break
            heading = node.find("h2")
            if heading:
                name = clean(heading.get_text(" ", strip=True))
                break
        links.append({"slug": slug, "name": name, "company_page": urljoin(BASE, anchor.get("href"))})
    # De-duplicate while preserving the authoritative page order.
    deduped = []
    seen = set()
    for row in links:
        if row["slug"] in seen:
            continue
        seen.add(row["slug"])
        deduped.append(row)
    h2_names = [clean(node.get_text(" ", strip=True)) for node in soup.find_all("h2")]
    h2_names = [name for name in h2_names if name]
    if len(deduped) == len(h2_names) and any(not row["name"] for row in deduped):
        for row, name in zip(deduped, h2_names, strict=True):
            if not row["name"]:
                row["name"] = name
    return deduped


def extract_profile_links(profiles_html: object) -> list[dict]:
    soup = BeautifulSoup(str(profiles_html or ""), "lxml")
    links = []
    for anchor in soup.find_all("a", href=True):
        links.append(
            {
                "text": clean(anchor.get_text(" ", strip=True)),
                "raw_href": clean(anchor.get("href")),
                "normalized_href": normalize_website(anchor.get("href")),
            }
        )
    return links


def website_from_record(record: dict) -> tuple[str, str, list[dict]]:
    links = extract_profile_links(record.get("profiles"))
    exact = [link for link in links if link["text"].casefold() == "website" and link["normalized_href"]]
    if len(exact) == 1:
        return exact[0]["normalized_href"], "wordpress_profiles_website_anchor", links
    if len(exact) > 1:
        unique = sorted({link["normalized_href"] for link in exact})
        if len(unique) == 1:
            return unique[0], "wordpress_profiles_duplicate_website_anchor", links
        return "", "ambiguous_website_anchors", links
    social_hosts = {
        "twitter.com",
        "x.com",
        "linkedin.com",
        "facebook.com",
        "instagram.com",
        "youtube.com",
        "crunchbase.com",
        "wikipedia.org",
    }
    candidates = []
    for link in links:
        target = link["normalized_href"]
        host = urlparse(target).netloc.casefold().removeprefix("www.") if target else ""
        if target and not any(host == item or host.endswith("." + item) for item in social_hosts):
            candidates.append(target)
    unique = sorted(set(candidates))
    if len(unique) == 1:
        return unique[0], "wordpress_profiles_single_non_social_anchor", links
    return "", "missing_or_ambiguous", links


def fetch_snapshot(session: requests.Session, label: str) -> dict:
    snapshot_dir = RAW / label
    page_response = request(session, PORTFOLIO_URL)
    page_meta = save_response(page_response, snapshot_dir / "portfolio.html")
    cards = portfolio_cards(page_response.content)

    records: list[dict] = []
    api_pages = []
    page = 1
    total = None
    total_pages = None
    while True:
        params = {"per_page": 100, "page": page}
        api_response = request(session, API_URL, params=params)
        meta = save_response(api_response, snapshot_dir / f"company-api-page-{page:03d}.json", params=params)
        payload = api_response.json()
        if not isinstance(payload, list):
            raise RuntimeError("company API payload is not a list")
        header_total = int(api_response.headers.get("x-wp-total") or len(payload))
        header_pages = int(api_response.headers.get("x-wp-totalpages") or 1)
        if total is None:
            total = header_total
            total_pages = header_pages
        elif total != header_total or total_pages != header_pages:
            raise RuntimeError("WordPress totals changed during pagination")
        records.extend(payload)
        api_pages.append({"page": page, "count": len(payload), **meta})
        if page >= header_pages:
            break
        page += 1

    return {
        "label": label,
        "cards": cards,
        "records": records,
        "page_meta": page_meta,
        "api_pages": api_pages,
        "api_reported_total": total,
        "api_reported_pages": total_pages,
    }


class ProfileParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[dict] = []
        self.current: dict | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() == "a":
            values = dict(attrs)
            self.current = {"raw_href": values.get("href") or "", "text": ""}

    def handle_data(self, data: str) -> None:
        if self.current is not None:
            self.current["text"] += data

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "a" and self.current is not None:
            self.current["text"] = clean(self.current["text"])
            self.current["normalized_href"] = normalize_website(self.current["raw_href"])
            self.links.append(self.current)
            self.current = None


def audit_website(record: dict) -> tuple[str, str]:
    parser = ProfileParser()
    parser.feed(str(record.get("profiles") or ""))
    exact = [link for link in parser.links if link["text"].casefold() == "website" and link["normalized_href"]]
    unique = sorted({link["normalized_href"] for link in exact})
    if len(unique) == 1:
        return unique[0], "exact"
    social = ("twitter.com", "x.com", "linkedin.com", "facebook.com", "instagram.com", "youtube.com", "crunchbase.com", "wikipedia.org")
    candidates = []
    for link in parser.links:
        url = link["normalized_href"]
        host = urlparse(url).netloc.casefold().removeprefix("www.") if url else ""
        if url and not any(host == s or host.endswith("." + s) for s in social):
            candidates.append(url)
    unique = sorted(set(candidates))
    return (unique[0], "single_non_social") if len(unique) == 1 else ("", "missing_or_ambiguous")


def independent_audit(snapshot_dir: Path, normalized_rows: list[dict], txt_path: Path) -> dict:
    raw_hash_errors = []
    for path in sorted(snapshot_dir.glob("*")):
        if path.suffix not in {".html", ".json"} or path.name.endswith(".meta.json"):
            continue
        meta_path = path.with_suffix(path.suffix + ".meta.json")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if digest(path.read_bytes()) != meta.get("sha256"):
            raw_hash_errors.append(path.name)

    page_raw = (snapshot_dir / "portfolio.html").read_bytes().decode("utf-8", "replace")
    page_slugs = []
    for slug in re.findall(r'href=["\'](?:https?://foundersfund\.com)?/company/([^/?#"\']+)/?["\']', page_raw, re.I):
        slug = slug.casefold()
        if slug not in page_slugs:
            page_slugs.append(slug)

    records = []
    for path in sorted(snapshot_dir.glob("company-api-page-*.json")):
        records.extend(json.loads(path.read_bytes()))
    audit_map = {}
    unresolved = []
    for record in records:
        slug = clean(record.get("slug")).casefold()
        name = normalize_title(record.get("title"))
        website, mode = audit_website(record)
        if not slug or not name or not website:
            unresolved.append({"slug": slug, "name": name, "mode": mode})
            continue
        audit_map[slug] = (name, website, int(record.get("id")))

    primary_map = {row["slug"]: (row["name"], row["website"], row["source_id"]) for row in normalized_rows}
    txt_lines = [line for line in txt_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    expected_lines = [f'{row["name"]} + {row["website"]}' for row in normalized_rows]
    return {
        "raw_hash_errors": raw_hash_errors,
        "page_slug_count": len(page_slugs),
        "api_record_count": len(records),
        "audit_record_count": len(audit_map),
        "unresolved": unresolved,
        "page_slug_set_equals_api_slug_set": set(page_slugs) == set(audit_map),
        "source_maps_equal": primary_map == audit_map,
        "txt_lines_equal": txt_lines == expected_lines,
        "txt_line_count": len(txt_lines),
        "status": "PASS"
        if not raw_hash_errors
        and not unresolved
        and set(page_slugs) == set(audit_map)
        and primary_map == audit_map
        and txt_lines == expected_lines
        else "FAIL",
    }


def check_website(row: dict) -> dict:
    url = row["website"]
    result = {"slug": row["slug"], "name": row["name"], "url": url}
    headers = {"User-Agent": "Mozilla/5.0 (compatible; portfolio-link-check/1.0)"}
    try:
        response = requests.get(url, headers=headers, timeout=12, allow_redirects=True, stream=True)
        result.update(
            {
                "status": response.status_code,
                "final_url": response.url,
                "reachable": response.status_code < 500,
                "content_type": response.headers.get("content-type"),
            }
        )
        response.close()
    except Exception as exc:
        result.update({"reachable": False, "error": repr(exc)})
    return result


def build_rows(snapshot: dict) -> tuple[list[dict], list[dict], dict]:
    records = snapshot["records"]
    by_slug = {}
    duplicate_api_slugs = []
    for record in records:
        slug = clean(record.get("slug")).casefold()
        if slug in by_slug:
            duplicate_api_slugs.append(slug)
        by_slug[slug] = record

    rows = []
    unresolved = []
    for position, card in enumerate(snapshot["cards"], start=1):
        record = by_slug.get(card["slug"])
        if record is None:
            unresolved.append({"slug": card["slug"], "name": card["name"], "reason": "missing_api_record"})
            continue
        api_name = normalize_title(record.get("title"))
        website, provenance, links = website_from_record(record)
        if not website:
            unresolved.append(
                {
                    "slug": card["slug"],
                    "page_name": card["name"],
                    "api_name": api_name,
                    "reason": provenance,
                    "profile_links": links,
                    "profiles_html": record.get("profiles"),
                }
            )
            continue
        rows.append(
            {
                "position": position,
                "source_id": int(record.get("id")),
                "slug": card["slug"],
                "name": card["name"] or api_name,
                "api_name": api_name,
                "website": website,
                "website_provenance": provenance,
                "raw_website_links": links,
                "company_page": clean(record.get("link")) or card["company_page"],
                "modified": record.get("modified"),
            }
        )

    diagnostics = {
        "duplicate_api_slugs": duplicate_api_slugs,
        "page_slugs_not_in_api": sorted(set(card["slug"] for card in snapshot["cards"]) - set(by_slug)),
        "api_slugs_not_on_page": sorted(set(by_slug) - set(card["slug"] for card in snapshot["cards"])),
    }
    return rows, unresolved, diagnostics


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    NORMALIZED.mkdir(parents=True)

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (compatible; foundersfund-evidence-crawler/2.0)",
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.8",
        }
    )

    snapshot_a = fetch_snapshot(session, "snapshot-a")
    time.sleep(2)
    snapshot_b = fetch_snapshot(session, "snapshot-b")
    rows, unresolved, diagnostics = build_rows(snapshot_b)

    txt_path = NORMALIZED / "foundersfund-62-portfolio-companies.txt"
    json_path = NORMALIZED / "foundersfund-62-portfolio-companies.json"
    txt_path.write_text(
        "\n".join(f'{row["name"]} + {row["website"]}' for row in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )
    json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    (OUT / "unresolved.json").write_text(
        json.dumps(unresolved, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )

    map_a_rows, unresolved_a, diagnostics_a = build_rows(snapshot_a)
    map_a = {row["slug"]: (row["source_id"], row["name"], row["website"]) for row in map_a_rows}
    map_b = {row["slug"]: (row["source_id"], row["name"], row["website"]) for row in rows}
    snapshot_comparison = {
        "snapshot_a_page_sha256": snapshot_a["page_meta"]["sha256"],
        "snapshot_b_page_sha256": snapshot_b["page_meta"]["sha256"],
        "snapshot_a_api_hashes": [page["sha256"] for page in snapshot_a["api_pages"]],
        "snapshot_b_api_hashes": [page["sha256"] for page in snapshot_b["api_pages"]],
        "snapshot_a_company_count": len(map_a),
        "snapshot_b_company_count": len(map_b),
        "company_maps_equal": map_a == map_b,
        "added_slugs": sorted(set(map_b) - set(map_a)),
        "removed_slugs": sorted(set(map_a) - set(map_b)),
        "changed_slugs": sorted(slug for slug in set(map_a) & set(map_b) if map_a[slug] != map_b[slug]),
        "snapshot_a_unresolved": unresolved_a,
        "snapshot_a_diagnostics": diagnostics_a,
        "status": "PASS" if map_a == map_b and not unresolved_a else "FAIL",
    }
    (OUT / "snapshot-comparison.json").write_text(
        json.dumps(snapshot_comparison, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )

    audit = independent_audit(RAW / "snapshot-b", rows, txt_path)
    (OUT / "independent-audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(check_website, row) for row in rows]
        website_checks = [future.result() for future in as_completed(futures)]
    website_checks.sort(key=lambda row: (row["name"].casefold(), row["url"]))
    (OUT / "website-checks.json").write_text(
        json.dumps(website_checks, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )

    names = [row["name"].casefold() for row in rows]
    hosts = [urlparse(row["website"]).netloc.casefold().removeprefix("www.") for row in rows]
    duplicate_names = {name: count for name, count in Counter(names).items() if count > 1}
    duplicate_websites = {host: count for host, count in Counter(hosts).items() if count > 1}
    invalid_websites = [
        row for row in rows if urlparse(row["website"]).scheme != "https" or not urlparse(row["website"]).netloc
    ]
    name_mismatches = [
        {"slug": row["slug"], "page_name": row["name"], "api_name": row["api_name"]}
        for row in rows
        if row["name"].casefold() != row["api_name"].casefold()
    ]
    reachable_count = sum(bool(item.get("reachable")) for item in website_checks)

    validation = {
        "source": PORTFOLIO_URL,
        "api": API_URL,
        "snapshot_a": {
            "page_card_count": len(snapshot_a["cards"]),
            "api_reported_total": snapshot_a["api_reported_total"],
            "api_raw_count": len(snapshot_a["records"]),
            "api_unique_id_count": len({int(record["id"]) for record in snapshot_a["records"]}),
            "api_unique_slug_count": len({record["slug"] for record in snapshot_a["records"]}),
        },
        "snapshot_b": {
            "page_card_count": len(snapshot_b["cards"]),
            "page_unique_slug_count": len({card["slug"] for card in snapshot_b["cards"]}),
            "page_unique_name_count": len({card["name"].casefold() for card in snapshot_b["cards"]}),
            "api_reported_total": snapshot_b["api_reported_total"],
            "api_reported_pages": snapshot_b["api_reported_pages"],
            "api_raw_count": len(snapshot_b["records"]),
            "api_unique_id_count": len({int(record["id"]) for record in snapshot_b["records"]}),
            "api_unique_slug_count": len({record["slug"] for record in snapshot_b["records"]}),
        },
        "resolved_count": len(rows),
        "output_line_count": len([line for line in txt_path.read_text(encoding="utf-8").splitlines() if line.strip()]),
        "unresolved_count": len(unresolved),
        "duplicate_name_groups": duplicate_names,
        "duplicate_website_groups": duplicate_websites,
        "invalid_website_count": len(invalid_websites),
        "name_mismatch_count": len(name_mismatches),
        "name_mismatches": name_mismatches,
        "diagnostics": diagnostics,
        "snapshot_comparison": snapshot_comparison,
        "independent_audit": audit,
        "website_reachable_count": reachable_count,
        "website_unreachable_or_blocked_count": len(website_checks) - reachable_count,
        "website_reachability_is_non_blocking": True,
    }

    hard_errors = []
    expected = snapshot_b["api_reported_total"]
    if expected != 62:
        hard_errors.append(f"unexpected API total: {expected}")
    if len(snapshot_b["cards"]) != expected:
        hard_errors.append("portfolio page card count does not equal API total")
    if len(snapshot_b["records"]) != expected:
        hard_errors.append("API raw count does not equal reported total")
    if len({int(record["id"]) for record in snapshot_b["records"]}) != expected:
        hard_errors.append("API IDs are not unique and complete")
    if len({record["slug"] for record in snapshot_b["records"]}) != expected:
        hard_errors.append("API slugs are not unique and complete")
    if unresolved:
        hard_errors.append("unresolved company records")
    if len(rows) != expected:
        hard_errors.append("normalized output count does not equal source total")
    if diagnostics["page_slugs_not_in_api"] or diagnostics["api_slugs_not_on_page"]:
        hard_errors.append("portfolio page and API slug sets differ")
    if invalid_websites:
        hard_errors.append("invalid normalized websites")
    if name_mismatches:
        hard_errors.append("page names and API titles differ")
    if snapshot_comparison["status"] != "PASS":
        hard_errors.append("two complete snapshots differ")
    if audit["status"] != "PASS":
        hard_errors.append("independent audit failed")

    validation["hard_errors"] = hard_errors
    validation["status"] = "PASS" if not hard_errors else "FAIL"
    (OUT / "validation.json").write_text(
        json.dumps(validation, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
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
                "sha256": digest(path.read_bytes()),
            }
    (OUT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )

    print("VALIDATION_SUMMARY_START")
    print(
        json.dumps(
            {
                "status": validation["status"],
                "page_card_count": len(snapshot_b["cards"]),
                "api_reported_total": expected,
                "api_raw_count": len(snapshot_b["records"]),
                "api_unique_id_count": len({int(record["id"]) for record in snapshot_b["records"]}),
                "api_unique_slug_count": len({record["slug"] for record in snapshot_b["records"]}),
                "resolved_count": len(rows),
                "output_line_count": validation["output_line_count"],
                "unresolved_count": len(unresolved),
                "duplicate_name_group_count": len(duplicate_names),
                "duplicate_website_group_count": len(duplicate_websites),
                "snapshot_comparison": snapshot_comparison["status"],
                "independent_audit": audit["status"],
                "website_reachable_count": reachable_count,
                "hard_errors": hard_errors,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    print("VALIDATION_SUMMARY_END")
    print("OUTPUT_START")
    print(txt_path.read_text(encoding="utf-8"), end="")
    print("OUTPUT_END")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        OUT.mkdir(parents=True, exist_ok=True)
        error = {"status": "FATAL", "error": repr(exc), "traceback": traceback.format_exc()}
        (OUT / "fatal-error.json").write_text(
            json.dumps(error, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )
        print(error["traceback"], file=sys.stderr)
        sys.exit(0)
