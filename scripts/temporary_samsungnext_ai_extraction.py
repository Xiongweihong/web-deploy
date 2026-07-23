from __future__ import annotations

import concurrent.futures
import hashlib
import json
import re
import shutil
import time
import traceback
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

TARGET_URL = "https://www.samsungnext.com/ai-portfolio"
OUT = Path("artifact")
RAW = OUT / "raw"
NORMALIZED = OUT / "normalized"
USER_AGENT = "Mozilla/5.0 (compatible; samsungnext-evidence-crawler/1.0)"
EXCLUDED_HOSTS = {
    "samsungnext.com",
    "www.samsungnext.com",
    "samsung.com",
    "www.samsung.com",
    "samsungnext.typeform.com",
    "typeform.com",
    "www.typeform.com",
    "linkedin.com",
    "www.linkedin.com",
    "twitter.com",
    "x.com",
    "facebook.com",
    "www.facebook.com",
    "instagram.com",
    "www.instagram.com",
    "youtube.com",
    "www.youtube.com",
}
GENERIC_NAME_WORDS = {
    "logo",
    "image",
    "company",
    "portfolio",
    "samsung next",
    "samsungnext",
}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_host(url: str) -> str:
    return urlparse(url).netloc.casefold().removeprefix("www.")


def root_url(url: str) -> str:
    text = clean(url)
    if not text:
        return ""
    if text.startswith("//"):
        text = "https:" + text
    elif "://" not in text:
        text = "https://" + text
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return f"https://{parsed.netloc.casefold().removeprefix('www.')}"


def name_from_domain(url: str) -> str:
    host = normalize_host(url)
    label = host.split(".")[0]
    return label.replace("-", " ").replace("_", " ").strip().title()


def clean_name_candidate(value: object) -> str:
    text = clean(value)
    if not text:
        return ""
    text = re.sub(r"https?://\S+", "", text, flags=re.I).strip()
    text = text.split("?")[0]
    if "/" in text and not re.search(r"\s", text):
        text = text.rsplit("/", 1)[-1]
    text = re.sub(r"\.(?:png|jpe?g|webp|gif|svg)$", "", text, flags=re.I)
    text = re.sub(r"\b(?:company\s+)?logo\b", "", text, flags=re.I)
    text = re.sub(r"\b(?:portfolio\s+)?image\b", "", text, flags=re.I)
    text = re.sub(r"[_-]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" -_|:")
    if text.casefold() in GENERIC_NAME_WORDS:
        return ""
    return text


def fetch(url: str, *, timeout: int = 60) -> requests.Response:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
    last: Exception | None = None
    for attempt in range(1, 6):
        try:
            response = session.get(url, timeout=timeout, allow_redirects=True)
            if response.status_code in {408, 429} or response.status_code >= 500:
                raise RuntimeError(f"transient HTTP {response.status_code}")
            response.raise_for_status()
            return response
        except Exception as exc:
            last = exc
            if attempt < 5:
                time.sleep(min(16, 2**attempt))
    raise RuntimeError(f"GET failed for {url}: {last}")


def save_response(path: Path, response: requests.Response) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(response.content)
    meta = {
        "requested_url": response.request.url,
        "final_url": response.url,
        "status": response.status_code,
        "content_type": response.headers.get("content-type"),
        "date": response.headers.get("date"),
        "etag": response.headers.get("etag"),
        "last_modified": response.headers.get("last-modified"),
        "bytes": len(response.content),
        "sha256": digest(response.content),
        "fetched_at_epoch": time.time(),
    }
    path.with_suffix(path.suffix + ".meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    return meta


def attribute_values(tag) -> list[dict]:
    values: list[dict] = []
    if tag is None:
        return values
    for key, value in tag.attrs.items():
        if isinstance(value, list):
            value = " ".join(map(str, value))
        text = clean(value)
        if text:
            values.append({"key": str(key), "value": text})
    return values


def is_company_anchor(anchor) -> bool:
    href = urljoin(TARGET_URL, clean(anchor.get("href")))
    parsed = urlparse(href)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    host = parsed.netloc.casefold()
    bare = host.removeprefix("www.")
    if host in EXCLUDED_HOSTS or bare in EXCLUDED_HOSTS:
        return False
    if bare.endswith("samsungnext.com") or bare.endswith("samsung.com"):
        return False
    # Portfolio cards are image links. This excludes footer/navigation links.
    if anchor.find("img") is not None:
        return True
    parent = anchor.parent
    for _ in range(3):
        if parent is None:
            break
        if parent.find("img") is not None and parent.find("p") is not None:
            return True
        parent = parent.parent
    return False


def extract_name(anchor, href: str) -> tuple[str, str, list[dict]]:
    candidates: list[tuple[int, str, str]] = []
    debug: list[dict] = []
    for img_index, img in enumerate(anchor.find_all("img")):
        attrs = attribute_values(img)
        debug.append({"type": "img", "index": img_index, "attrs": attrs})
        for key, priority in (
            ("alt", 120),
            ("title", 110),
            ("data-title", 105),
            ("data-image-title", 100),
            ("aria-label", 95),
            ("data-filename", 80),
            ("data-src", 60),
            ("src", 50),
        ):
            value = img.get(key)
            cleaned = clean_name_candidate(value)
            if cleaned:
                candidates.append((priority, cleaned, f"img.{key}"))
    for key, priority in (("aria-label", 115), ("title", 105), ("data-title", 100)):
        cleaned = clean_name_candidate(anchor.get(key))
        if cleaned:
            candidates.append((priority, cleaned, f"anchor.{key}"))
    anchor_text = clean_name_candidate(" ".join(anchor.stripped_strings))
    if anchor_text:
        candidates.append((90, anchor_text, "anchor.text"))
    parent = anchor.parent
    for depth in range(1, 6):
        if parent is None:
            break
        debug.append({"type": "parent", "depth": depth, "tag": parent.name, "attrs": attribute_values(parent)})
        for key, priority in (("data-title", 92), ("data-image-title", 91), ("aria-label", 90), ("title", 89)):
            cleaned = clean_name_candidate(parent.get(key))
            if cleaned:
                candidates.append((priority - depth, cleaned, f"parent{depth}.{key}"))
        parent = parent.parent
    domain_token = normalize_host(href).split(".")[0].replace("-", " ").casefold()
    filtered = []
    for priority, candidate, source in candidates:
        cfold = candidate.casefold()
        if cfold.startswith("http") or len(candidate) > 80:
            continue
        if cfold in {"external link", "learn more", "visit website"}:
            continue
        # Prefer human labels to raw image filenames/hosts.
        penalty = 0
        if cfold == domain_token:
            penalty = 3
        filtered.append((priority - penalty, candidate, source))
    filtered.sort(key=lambda item: (-item[0], len(item[1]), item[1].casefold()))
    if filtered:
        return filtered[0][1], filtered[0][2], debug
    return "", "missing", debug


def parse_snapshot(html: bytes) -> tuple[list[dict], dict]:
    soup = BeautifulSoup(html, "lxml")
    rows: list[dict] = []
    seen_hrefs: set[str] = set()
    for order, anchor in enumerate(soup.find_all("a", href=True)):
        if not is_company_anchor(anchor):
            continue
        href = urljoin(TARGET_URL, clean(anchor.get("href")))
        if href in seen_hrefs:
            continue
        seen_hrefs.add(href)
        name, provenance, debug = extract_name(anchor, href)
        card = anchor
        for _ in range(5):
            if card.parent is None:
                break
            card = card.parent
            if card.find("p") is not None:
                break
        description = ""
        if card is not None:
            paragraphs = [clean(p.get_text(" ", strip=True)) for p in card.find_all("p")]
            description = next((p for p in paragraphs if p), "")
        rows.append(
            {
                "order": len(rows) + 1,
                "document_anchor_index": order,
                "name": name,
                "name_provenance": provenance,
                "source_url": href,
                "source_root_url": root_url(href),
                "source_host": normalize_host(href),
                "description": description,
                "anchor_attrs": attribute_values(anchor),
                "debug": debug,
            }
        )
    info = {
        "title": clean(soup.title.get_text(" ", strip=True)) if soup.title else "",
        "all_anchor_count": len(soup.find_all("a", href=True)),
        "company_anchor_count": len(rows),
        "unique_company_host_count": len({row["source_host"] for row in rows}),
    }
    return rows, info


def inspect_website(row: dict) -> dict:
    url = row["source_url"]
    result = {
        "source_url": url,
        "source_host": row["source_host"],
        "status": None,
        "final_url": "",
        "final_root_url": row["source_root_url"],
        "final_host": row["source_host"],
        "title": "",
        "og_site_name": "",
        "error": "",
    }
    try:
        response = fetch(url, timeout=25)
        result["status"] = response.status_code
        result["final_url"] = response.url
        result["final_root_url"] = root_url(response.url) or row["source_root_url"]
        result["final_host"] = normalize_host(response.url) or row["source_host"]
        content_type = response.headers.get("content-type", "")
        if "html" in content_type.casefold() or response.text.lstrip().startswith("<"):
            soup = BeautifulSoup(response.content[:2_000_000], "lxml")
            if soup.title:
                result["title"] = clean(soup.title.get_text(" ", strip=True))
            og = soup.find("meta", attrs={"property": "og:site_name"})
            if og:
                result["og_site_name"] = clean(og.get("content"))
    except Exception as exc:
        result["error"] = repr(exc)
    return result


def derive_final_name(row: dict, check: dict) -> tuple[str, str]:
    source_name = clean_name_candidate(row.get("name"))
    if source_name:
        return source_name, row.get("name_provenance") or "source"
    for value, provenance in (
        (check.get("og_site_name"), "website_og_site_name"),
        (check.get("title"), "website_title"),
    ):
        candidate = clean_name_candidate(value)
        if candidate:
            candidate = re.split(r"\s+[|–—-]\s+", candidate)[0].strip()
            if candidate:
                return candidate, provenance
    return name_from_domain(row["source_url"]), "domain_fallback_needs_review"


def independent_parse(html: bytes) -> list[tuple[str, str]]:
    # Separate reconstruction path: use CSS image-link selection and source host only.
    soup = BeautifulSoup(html, "html.parser")
    results: list[tuple[str, str]] = []
    seen: set[str] = set()
    for img in soup.select("a[href] img"):
        anchor = img.find_parent("a", href=True)
        if anchor is None:
            continue
        href = urljoin(TARGET_URL, clean(anchor.get("href")))
        host = normalize_host(href)
        if not host or host in EXCLUDED_HOSTS or host.endswith("samsungnext.com") or host.endswith("samsung.com"):
            continue
        if href in seen:
            continue
        seen.add(href)
        label = clean_name_candidate(img.get("alt") or img.get("title") or img.get("data-title") or img.get("data-src") or img.get("src"))
        results.append((label, href))
    return results


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    RAW.mkdir(parents=True)
    NORMALIZED.mkdir(parents=True)

    snapshots = []
    for snapshot in (1, 2):
        response = fetch(TARGET_URL)
        path = RAW / f"snapshot-{snapshot:02d}.html"
        meta = save_response(path, response)
        rows, info = parse_snapshot(response.content)
        snapshot_data = {"snapshot": snapshot, "meta": meta, "info": info, "rows": rows}
        (RAW / f"snapshot-{snapshot:02d}-parsed.json").write_text(
            json.dumps(snapshot_data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )
        snapshots.append(snapshot_data)
        if snapshot == 1:
            time.sleep(2)

    selected = snapshots[-1]
    source_rows = selected["rows"]
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
        checks = list(executor.map(inspect_website, source_rows))
    check_by_url = {row["source_url"]: row for row in checks}
    (OUT / "website-checks.json").write_text(
        json.dumps(checks, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )

    final_rows = []
    for source_row in source_rows:
        check = check_by_url[source_row["source_url"]]
        name, name_provenance = derive_final_name(source_row, check)
        website = check.get("final_root_url") or source_row["source_root_url"]
        final_rows.append(
            {
                "order": source_row["order"],
                "name": name,
                "website": website,
                "source_name": source_row["name"],
                "source_name_provenance": source_row["name_provenance"],
                "final_name_provenance": name_provenance,
                "source_url": source_row["source_url"],
                "source_host": source_row["source_host"],
                "final_url": check.get("final_url"),
                "final_host": normalize_host(website),
                "http_status": check.get("status"),
                "http_error": check.get("error"),
                "description": source_row["description"],
            }
        )

    txt_path = NORMALIZED / "samsungnext-ai-portfolio-companies.txt"
    json_path = NORMALIZED / "samsungnext-ai-portfolio-companies.json"
    txt_path.write_text(
        "\n".join(f'{row["name"]} + {row["website"]}' for row in final_rows) + ("\n" if final_rows else ""),
        encoding="utf-8",
    )
    json_path.write_text(json.dumps(final_rows, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    snapshot_maps = [
        [(row["source_url"], row["name"], row["description"]) for row in snapshot["rows"]]
        for snapshot in snapshots
    ]
    snapshot_comparison = {
        "status": "PASS" if snapshot_maps[0] == snapshot_maps[1] else "FAIL",
        "snapshot_1_count": len(snapshot_maps[0]),
        "snapshot_2_count": len(snapshot_maps[1]),
        "snapshot_1_sha256": snapshots[0]["meta"]["sha256"],
        "snapshot_2_sha256": snapshots[1]["meta"]["sha256"],
        "same_raw_sha256": snapshots[0]["meta"]["sha256"] == snapshots[1]["meta"]["sha256"],
        "added_urls": sorted(set(x[0] for x in snapshot_maps[1]) - set(x[0] for x in snapshot_maps[0])),
        "removed_urls": sorted(set(x[0] for x in snapshot_maps[0]) - set(x[0] for x in snapshot_maps[1])),
    }
    (OUT / "snapshot-comparison.json").write_text(
        json.dumps(snapshot_comparison, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )

    independent = independent_parse((RAW / "snapshot-02.html").read_bytes())
    independent_urls = [url for _, url in independent]
    primary_urls = [row["source_url"] for row in source_rows]
    audit = {
        "status": "PASS" if independent_urls == primary_urls else "FAIL",
        "independent_count": len(independent_urls),
        "primary_count": len(primary_urls),
        "url_order_equal": independent_urls == primary_urls,
        "missing_in_independent": sorted(set(primary_urls) - set(independent_urls)),
        "extra_in_independent": sorted(set(independent_urls) - set(primary_urls)),
        "raw_sha256_verified": digest((RAW / "snapshot-02.html").read_bytes()) == snapshots[1]["meta"]["sha256"],
    }
    (OUT / "independent-audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )

    names = [row["name"].casefold() for row in final_rows]
    source_urls = [row["source_url"] for row in final_rows]
    final_hosts = [row["final_host"] for row in final_rows]
    missing_names = [row for row in final_rows if not row["name"]]
    missing_websites = [row for row in final_rows if not row["website"]]
    fallback_names = [row for row in final_rows if row["final_name_provenance"] == "domain_fallback_needs_review"]
    duplicate_names = {name: count for name, count in Counter(names).items() if count > 1}
    duplicate_source_urls = {url: count for url, count in Counter(source_urls).items() if count > 1}
    duplicate_final_hosts = {host: count for host, count in Counter(final_hosts).items() if host and count > 1}
    invalid_websites = [
        row for row in final_rows
        if urlparse(row["website"]).scheme != "https" or not urlparse(row["website"]).netloc
    ]
    http_failures = [row for row in final_rows if row["http_error"] or not row["http_status"]]

    errors = []
    if len(final_rows) != 89:
        errors.append(f"expected 89 source company links from the page, found {len(final_rows)}")
    if missing_names:
        errors.append("missing company names")
    if missing_websites:
        errors.append("missing company websites")
    if duplicate_source_urls:
        errors.append("duplicate source URLs")
    if duplicate_names:
        errors.append("duplicate final company names")
    if duplicate_final_hosts:
        errors.append("duplicate final website hosts")
    if invalid_websites:
        errors.append("invalid final website URLs")
    if snapshot_comparison["status"] != "PASS":
        errors.append("two source snapshots differ")
    if audit["status"] != "PASS":
        errors.append("independent source reconstruction failed")

    validation = {
        "source_url": TARGET_URL,
        "captured_at_epoch": snapshots[1]["meta"]["fetched_at_epoch"],
        "status": "PASS" if not errors else "NEEDS_REVIEW",
        "source_company_link_count": len(final_rows),
        "source_unique_host_count": len({row["source_host"] for row in final_rows}),
        "output_line_count": len(final_rows),
        "unique_name_count": len(set(names)),
        "unique_final_host_count": len(set(final_hosts)),
        "missing_name_count": len(missing_names),
        "missing_website_count": len(missing_websites),
        "fallback_name_count": len(fallback_names),
        "fallback_names": fallback_names,
        "invalid_website_count": len(invalid_websites),
        "duplicate_name_groups": duplicate_names,
        "duplicate_source_url_groups": duplicate_source_urls,
        "duplicate_final_host_groups": duplicate_final_hosts,
        "website_check_failure_count": len(http_failures),
        "website_check_failures": http_failures,
        "snapshot_comparison": snapshot_comparison,
        "independent_audit": audit,
        "errors": errors,
        "notes": [
            "The source page contains 89 external image-linked AI & Robotics portfolio cards.",
            "Company names are extracted from source image/card metadata; website metadata is only a fallback.",
            "Final websites use the HTTPS root of the source link or its verified redirect destination.",
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
                "sha256": digest(path.read_bytes()),
            }
    (OUT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )

    print("VALIDATION_SUMMARY_START")
    print(json.dumps({
        "status": validation["status"],
        "source_company_link_count": validation["source_company_link_count"],
        "unique_name_count": validation["unique_name_count"],
        "unique_final_host_count": validation["unique_final_host_count"],
        "fallback_name_count": validation["fallback_name_count"],
        "website_check_failure_count": validation["website_check_failure_count"],
        "snapshot_comparison": snapshot_comparison["status"],
        "independent_audit": audit["status"],
        "errors": errors,
    }, ensure_ascii=False, indent=2, sort_keys=True))
    print("VALIDATION_SUMMARY_END")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        OUT.mkdir(parents=True, exist_ok=True)
        failure = {"status": "FATAL", "error": repr(exc), "traceback": traceback.format_exc()}
        (OUT / "fatal-error.json").write_text(
            json.dumps(failure, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )
        print(failure["traceback"])
