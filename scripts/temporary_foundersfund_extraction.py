from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
import time
import traceback
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE = "https://foundersfund.com"
PORTFOLIO_URL = BASE + "/portfolio/"
OUT = Path("artifact")
RAW = OUT / "raw"
PAGES = RAW / "company-pages"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def slugify(value: str) -> str:
    value = value.casefold().replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value


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
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return text.rstrip("/")


def get(session: requests.Session, url: str, *, timeout: int = 60) -> requests.Response:
    last = None
    for attempt in range(1, 6):
        try:
            response = session.get(url, timeout=timeout, allow_redirects=True)
            if response.status_code >= 500 or response.status_code in {408, 429}:
                raise RuntimeError(f"transient HTTP {response.status_code}")
            response.raise_for_status()
            return response
        except Exception as exc:
            last = exc
            if attempt == 5:
                break
            time.sleep(min(20, 2**attempt))
    raise RuntimeError(f"GET failed for {url}: {last}")


def save_response(response: requests.Response, path: Path) -> dict:
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
        "sha256": sha256(response.content),
        "fetched_at_epoch": time.time(),
    }
    path.with_suffix(path.suffix + ".meta.json").write_text(
        json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8"
    )
    return meta


def parse_json_scripts(soup: BeautifulSoup) -> list[object]:
    values: list[object] = []
    for script in soup.find_all("script"):
        script_type = clean(script.get("type")).casefold()
        text = script.string or script.get_text("", strip=False)
        if not text or script.get("src"):
            continue
        if "json" in script_type or text.lstrip().startswith(("{", "[")):
            try:
                values.append(json.loads(text))
            except Exception:
                pass
    return values


def walk(value: object, path: tuple[object, ...] = ()):  # generic JSON walker
    yield path, value
    if isinstance(value, dict):
        for key, child in value.items():
            yield from walk(child, path + (key,))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from walk(child, path + (index,))


def external_links(soup: BeautifulSoup) -> list[dict]:
    rows = []
    for anchor in soup.find_all("a", href=True):
        href = urljoin(BASE, anchor.get("href"))
        parsed = urlparse(href)
        host = parsed.netloc.casefold().removeprefix("www.")
        if parsed.scheme not in {"http", "https"}:
            continue
        if host == "foundersfund.com" or host.endswith(".foundersfund.com"):
            continue
        rows.append(
            {
                "text": clean(anchor.get_text(" ", strip=True)),
                "href": href,
                "host": host,
                "rel": anchor.get("rel"),
                "class": anchor.get("class"),
                "aria_label": anchor.get("aria-label"),
            }
        )
    unique = []
    seen = set()
    for row in rows:
        key = (row["text"], row["href"])
        if key not in seen:
            seen.add(key)
            unique.append(row)
    return unique


def discover_wp(session: requests.Session) -> dict:
    report: dict = {"probes": {}}
    probe_urls = [
        BASE + "/wp-json/",
        BASE + "/wp-json/wp/v2/types",
        BASE + "/wp-json/wp/v2/search?search=SpaceX&per_page=20",
        BASE + "/wp-json/wp/v2/company?per_page=100",
        BASE + "/wp-json/wp/v2/companies?per_page=100",
        BASE + "/wp-json/wp/v2/portfolio?per_page=100",
        BASE + "/wp-json/wp/v2/portfolio_company?per_page=100",
        BASE + "/wp-json/wp/v2/portfolio-companies?per_page=100",
    ]
    for url in probe_urls:
        try:
            response = session.get(url, timeout=30, allow_redirects=True)
            row = {
                "status": response.status_code,
                "content_type": response.headers.get("content-type"),
                "bytes": len(response.content),
                "sha256": sha256(response.content),
                "final_url": response.url,
                "sample": response.text[:1000],
            }
            try:
                payload = response.json()
                row["json_type"] = type(payload).__name__
                if isinstance(payload, dict):
                    row["json_keys"] = sorted(payload.keys())[:100]
                    if url.endswith("/wp-json/"):
                        routes = payload.get("routes") or {}
                        row["candidate_routes"] = sorted(
                            key for key in routes if re.search(r"company|portfolio", key, re.I)
                        )[:500]
                elif isinstance(payload, list):
                    row["json_length"] = len(payload)
                    row["json_sample"] = payload[:2]
            except Exception:
                pass
            report["probes"][url] = row
        except Exception as exc:
            report["probes"][url] = {"error": repr(exc)}
    return report


def candidate_records_from_json(json_values: list[object], known_names: set[str]) -> list[dict]:
    candidates = []
    for root_index, root in enumerate(json_values):
        for path, value in walk(root):
            if not isinstance(value, dict):
                continue
            lower = {str(key).casefold(): child for key, child in value.items()}
            name = clean(
                lower.get("name")
                or lower.get("title")
                or lower.get("company_name")
                or lower.get("companyname")
            )
            if not name:
                continue
            website = clean(
                lower.get("website")
                or lower.get("website_url")
                or lower.get("url")
                or lower.get("external_url")
            )
            page_url = clean(lower.get("link") or lower.get("permalink") or lower.get("slug"))
            if name.casefold() in known_names or website or "company" in " ".join(map(str, path)).casefold():
                candidates.append(
                    {
                        "root_index": root_index,
                        "path": list(path),
                        "name": name,
                        "website": website,
                        "page_url_or_slug": page_url,
                        "keys": sorted(map(str, value.keys())),
                        "raw": value,
                    }
                )
    return candidates


def select_website(links: list[dict]) -> tuple[str, str, list[dict]]:
    excluded_hosts = {
        "twitter.com",
        "x.com",
        "linkedin.com",
        "www.linkedin.com",
        "facebook.com",
        "www.facebook.com",
        "instagram.com",
        "www.instagram.com",
        "youtube.com",
        "www.youtube.com",
        "crunchbase.com",
        "www.crunchbase.com",
    }
    scored = []
    for link in links:
        host = link["host"]
        score = 0
        text = link["text"].casefold()
        if text == "website":
            score += 100
        elif "website" in text:
            score += 60
        if host in excluded_hosts or any(host.endswith("." + h) for h in excluded_hosts):
            score -= 100
        if link["href"].lower().startswith("mailto:"):
            score -= 100
        scored.append((score, link))
    scored.sort(key=lambda item: (-item[0], item[1]["href"]))
    for score, link in scored:
        if score >= 0 and link["host"] not in excluded_hosts:
            return normalize_url(link["href"]), f"company_page_external_link_score_{score}", [x[1] for x in scored]
    return "", "missing", [x[1] for x in scored]


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    PAGES.mkdir(parents=True)

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (compatible; foundersfund-evidence-crawler/1.0)",
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.8",
        }
    )

    response = get(session, PORTFOLIO_URL)
    portfolio_meta = save_response(response, RAW / "portfolio.html")
    soup = BeautifulSoup(response.content, "lxml")

    h2_names = [clean(node.get_text(" ", strip=True)) for node in soup.find_all("h2")]
    h2_names = [name for name in h2_names if name]
    known_names = {name.casefold() for name in h2_names}

    anchors = []
    for anchor in soup.find_all("a", href=True):
        anchors.append(
            {
                "text": clean(anchor.get_text(" ", strip=True)),
                "href": urljoin(BASE, anchor.get("href")),
                "class": anchor.get("class"),
                "data": {key: value for key, value in anchor.attrs.items() if str(key).startswith("data-")},
            }
        )

    company_paths = set()
    for anchor in anchors:
        match = re.search(r"/company/([^/?#]+)/?", anchor["href"], re.I)
        if match:
            company_paths.add(match.group(1))
    for match in re.finditer(r"(?:https?://foundersfund\.com)?/company/([a-z0-9-]+)/?", response.text, re.I):
        company_paths.add(match.group(1))

    json_values = parse_json_scripts(soup)
    json_candidates = candidate_records_from_json(json_values, known_names)

    script_sources = [urljoin(BASE, script.get("src")) for script in soup.find_all("script", src=True)]
    script_inventory = []
    for index, source in enumerate(script_sources):
        try:
            script_response = get(session, source)
            path = RAW / "scripts" / f"script-{index:03d}.js"
            meta = save_response(script_response, path)
            text = script_response.text
            script_inventory.append(
                {
                    "url": source,
                    **meta,
                    "contains_spacex": "spacex" in text.casefold(),
                    "contains_company_path": "/company/" in text,
                    "contains_website_key": "website" in text.casefold(),
                    "company_path_matches": sorted(
                        set(re.findall(r"/company/([a-z0-9-]+)/?", text, re.I))
                    )[:500],
                    "absolute_url_count": len(re.findall(r"https?://[^\"'\\s<>]+", text)),
                }
            )
            company_paths.update(script_inventory[-1]["company_path_matches"])
        except Exception as exc:
            script_inventory.append({"url": source, "error": repr(exc)})

    wp_report = discover_wp(session)

    # If no explicit slugs exist, probe predictable slugs from the authoritative H2 names.
    inferred_slugs = {slugify(name) for name in h2_names}
    slugs_to_probe = sorted(company_paths | inferred_slugs)
    page_results = []
    for slug in slugs_to_probe:
        url = f"{BASE}/company/{slug}/"
        try:
            company_response = get(session, url)
            page_path = PAGES / f"{slug}.html"
            meta = save_response(company_response, page_path)
            company_soup = BeautifulSoup(company_response.content, "lxml")
            h1 = clean(company_soup.find("h1").get_text(" ", strip=True)) if company_soup.find("h1") else ""
            title = clean(company_soup.title.get_text(" ", strip=True)) if company_soup.title else ""
            links = external_links(company_soup)
            website, provenance, ranked_links = select_website(links)
            page_results.append(
                {
                    "slug": slug,
                    "requested_url": url,
                    "final_url": company_response.url,
                    "status": company_response.status_code,
                    "h1": h1,
                    "title": title,
                    "website": website,
                    "website_provenance": provenance,
                    "external_links": links,
                    "ranked_external_links": ranked_links,
                    "sha256": meta["sha256"],
                    "bytes": meta["bytes"],
                }
            )
        except Exception as exc:
            page_results.append({"slug": slug, "requested_url": url, "error": repr(exc)})

    # Build rows by exact page H1/title name first, then by inferred slug.
    name_to_pages: dict[str, list[dict]] = defaultdict(list)
    slug_to_page = {row.get("slug"): row for row in page_results if row.get("slug")}
    for row in page_results:
        for value in (row.get("h1"), re.sub(r"\s*[-–|].*$", "", clean(row.get("title")))):
            if value:
                name_to_pages[value.casefold()].append(row)

    rows = []
    unresolved = []
    for name in h2_names:
        candidates = name_to_pages.get(name.casefold(), [])
        if not candidates:
            candidates = [slug_to_page[slugify(name)]] if slugify(name) in slug_to_page else []
        usable = [row for row in candidates if row.get("website")]
        unique_websites = sorted({row["website"] for row in usable})
        if len(unique_websites) == 1:
            chosen = next(row for row in usable if row["website"] == unique_websites[0])
            rows.append(
                {
                    "name": name,
                    "website": unique_websites[0],
                    "company_page": chosen.get("final_url") or chosen.get("requested_url"),
                    "slug": chosen.get("slug"),
                    "website_provenance": chosen.get("website_provenance"),
                    "raw_page_sha256": chosen.get("sha256"),
                }
            )
        else:
            unresolved.append(
                {
                    "name": name,
                    "inferred_slug": slugify(name),
                    "candidate_count": len(candidates),
                    "candidate_websites": unique_websites,
                    "candidate_pages": candidates,
                }
            )

    rows.sort(key=lambda row: (row["name"].casefold(), row["website"]))
    normalized = OUT / "normalized"
    normalized.mkdir()
    (normalized / "foundersfund-portfolio.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    (normalized / "foundersfund-portfolio.txt").write_text(
        "\n".join(f'{row["name"]} + {row["website"]}' for row in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )

    discovery = {
        "portfolio_meta": portfolio_meta,
        "title": clean(soup.title.get_text(" ", strip=True)) if soup.title else "",
        "h2_names": h2_names,
        "h2_count": len(h2_names),
        "h2_unique_count": len(set(name.casefold() for name in h2_names)),
        "anchor_count": len(anchors),
        "anchors": anchors,
        "company_paths_from_source": sorted(company_paths),
        "inferred_slugs": sorted(inferred_slugs),
        "script_sources": script_sources,
        "script_inventory": script_inventory,
        "json_script_count": len(json_values),
        "json_candidates": json_candidates,
        "wp_report": wp_report,
        "page_results": page_results,
    }
    (OUT / "discovery.json").write_text(
        json.dumps(discovery, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    (OUT / "unresolved.json").write_text(
        json.dumps(unresolved, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )

    validation = {
        "source": PORTFOLIO_URL,
        "portfolio_h2_count": len(h2_names),
        "portfolio_unique_name_count": len(set(name.casefold() for name in h2_names)),
        "company_path_count": len(company_paths),
        "probed_page_count": len(page_results),
        "resolved_count": len(rows),
        "unresolved_count": len(unresolved),
        "duplicate_names": [name for name, count in Counter(row["name"].casefold() for row in rows).items() if count > 1],
        "duplicate_websites": [host for host, count in Counter(urlparse(row["website"]).netloc.casefold().removeprefix("www.") for row in rows).items() if count > 1],
        "status": "PASS" if len(rows) == len(h2_names) and not unresolved else "DISCOVERY_NEEDED",
    }
    (OUT / "validation.json").write_text(
        json.dumps(validation, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )

    manifest = {"generated_at_epoch": time.time(), "files": {}}
    for path in sorted(OUT.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            manifest["files"][str(path.relative_to(OUT))] = {
                "bytes": path.stat().st_size,
                "sha256": sha256(path.read_bytes()),
            }
    (OUT / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )

    print("DISCOVERY_SUMMARY_START")
    print(json.dumps(validation, indent=2, sort_keys=True))
    print("DISCOVERY_SUMMARY_END")
    print("H2_NAMES", json.dumps(h2_names, ensure_ascii=False))
    print("COMPANY_PATHS", json.dumps(sorted(company_paths)))
    print("SCRIPT_SOURCES", json.dumps(script_sources))
    print("WP_CANDIDATE_ROUTES", json.dumps((wp_report.get("probes", {}).get(BASE + "/wp-json/", {}) or {}).get("candidate_routes", [])))


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
