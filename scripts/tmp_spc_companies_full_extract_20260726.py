from __future__ import annotations

import concurrent.futures
import hashlib
import json
import random
import re
import shutil
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

TARGET = "https://www.southparkcommons.com/companies"
OUT = Path("artifact-full")
RAW = OUT / "raw"
UA = "Mozilla/5.0 (compatible; spc-evidence-crawler/2.0)"
MAX_WORKERS = 10


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def save_bytes(path: Path, raw: bytes, meta: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    write_json(path.with_suffix(path.suffix + ".meta.json"), {**meta, "bytes": len(raw), "sha256": sha256(raw)})


def normalize_url(value: object, *, keep_path: bool = True) -> str:
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
    if not host or "." not in host:
        return ""
    path = (parsed.path or "").rstrip("/") if keep_path else ""
    return urlunparse(("https", host, path, "", "", ""))


def fetch(url: str, *, timeout: int = 90, attempts: int = 4) -> requests.Response:
    last = None
    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(
                url,
                timeout=timeout,
                headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8"},
                allow_redirects=True,
            )
            response.raise_for_status()
            return response
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < attempts:
                time.sleep(min(12, 2 ** attempt) + random.random())
    raise RuntimeError(f"GET failed for {url}: {last}")


def parse_listing(raw: bytes) -> list[dict]:
    soup = BeautifulSoup(raw, "lxml")
    script = soup.find("script", id="company-data", attrs={"type": "application/json"})
    if not script or not script.string:
        raise RuntimeError("company-data JSON script was not found")
    payload = json.loads(script.string)
    if not isinstance(payload, list):
        raise TypeError("company-data is not a list")
    rows = []
    for position, record in enumerate(payload, start=1):
        name = re.sub(r"\s+", " ", str(record.get("name") or "")).strip()
        slug = str(record.get("slug") or "").strip()
        rows.append(
            {
                "source_position": position,
                "source_id": str(record.get("id") or "").strip(),
                "name": name,
                "slug": slug,
                "detail_url": urljoin(TARGET + "/", slug),
                "bio": re.sub(r"\s+", " ", str(record.get("bio") or "")).strip(),
                "industry": re.sub(r"\s+", " ", str(record.get("industry") or "")).strip(),
                "founded": str(record.get("founded") or "").strip(),
                "status": re.sub(r"\s+", " ", str(record.get("status") or "")).strip(),
                "location": re.sub(r"\s+", " ", str(record.get("location") or "")).strip(),
                "thumbnail_url": str(record.get("thumbnailUrl") or "").strip(),
                "highlighted": bool(record.get("highlighted")),
                "source_record_sha256": sha256(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")),
            }
        )
    return rows


def parse_detail(raw: bytes, expected: dict) -> dict:
    soup = BeautifulSoup(raw, "lxml")
    heading = soup.find("h1")
    page_name = re.sub(r"\s+", " ", heading.get_text(" ", strip=True)).strip() if heading else ""
    website_href = ""
    linkedin_href = ""
    x_href = ""
    for anchor in soup.find_all("a", href=True):
        text = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True)).strip().casefold()
        href = str(anchor.get("href") or "").strip()
        if text == "website" and not website_href:
            website_href = href
        elif text == "linkedin" and not linkedin_href:
            linkedin_href = href
        elif text in {"x", "twitter"} and not x_href:
            x_href = href
    canonical = ""
    canonical_node = soup.find("link", rel=lambda value: value and "canonical" in value)
    if canonical_node:
        canonical = str(canonical_node.get("href") or "").strip()
    description = ""
    meta_description = soup.find("meta", attrs={"name": "description"})
    if meta_description:
        description = str(meta_description.get("content") or "").strip()
    return {
        "source_id": expected["source_id"],
        "source_position": expected["source_position"],
        "expected_name": expected["name"],
        "page_name": page_name,
        "slug": expected["slug"],
        "detail_url": expected["detail_url"],
        "canonical_url": canonical,
        "source_website": website_href,
        "website": normalize_url(website_href, keep_path=True),
        "website_root": normalize_url(website_href, keep_path=False),
        "linkedin_url": linkedin_href,
        "x_url": x_href,
        "meta_description": description,
    }


def fetch_detail(snapshot: int, company: dict) -> dict:
    response = fetch(company["detail_url"])
    raw = response.content
    target = RAW / f"details-{snapshot:02d}" / f"{company['source_position']:03d}_{company['slug']}.html"
    save_bytes(
        target,
        raw,
        {
            "requested_url": company["detail_url"],
            "final_url": response.url,
            "status": response.status_code,
            "content_type": response.headers.get("content-type"),
            "date": response.headers.get("date"),
            "etag": response.headers.get("etag"),
            "fetched_at_epoch": time.time(),
            "source_id": company["source_id"],
            "source_position": company["source_position"],
            "slug": company["slug"],
        },
    )
    parsed = parse_detail(raw, company)
    parsed.update(
        {
            "http_status": response.status_code,
            "final_detail_url": response.url,
            "raw_path": str(target.relative_to(OUT)),
            "raw_sha256": sha256(raw),
        }
    )
    return parsed


def fetch_all_details(snapshot: int, companies: list[dict]) -> list[dict]:
    results: list[dict] = []
    errors: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(fetch_detail, snapshot, company): company for company in companies}
        for future in concurrent.futures.as_completed(futures):
            company = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:  # noqa: BLE001
                errors.append({"company": company, "error": repr(exc)})
    results.sort(key=lambda row: row["source_position"])
    write_json(OUT / f"details-{snapshot:02d}-parsed.json", results)
    write_json(OUT / f"details-{snapshot:02d}-errors.json", errors)
    if errors:
        raise RuntimeError(f"Detail snapshot {snapshot} had {len(errors)} errors")
    return results


def check_website(item: dict) -> dict:
    url = item["website"]
    if not url:
        return {"source_id": item["source_id"], "name": item["expected_name"], "source_url": "", "status": None, "final_url": "", "final_normalized": "", "history": [], "error": "source website missing"}
    try:
        response = requests.get(
            url,
            timeout=35,
            headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"},
            allow_redirects=True,
            stream=True,
        )
        result = {
            "source_id": item["source_id"],
            "name": item["expected_name"],
            "source_url": url,
            "status": response.status_code,
            "final_url": response.url,
            "final_normalized": normalize_url(response.url, keep_path=True),
            "final_root": normalize_url(response.url, keep_path=False),
            "history": [{"status": prior.status_code, "url": prior.url, "location": prior.headers.get("location")} for prior in response.history],
            "content_type": response.headers.get("content-type"),
            "error": "",
        }
        response.close()
        return result
    except Exception as exc:  # noqa: BLE001
        return {"source_id": item["source_id"], "name": item["expected_name"], "source_url": url, "status": None, "final_url": "", "final_normalized": "", "final_root": "", "history": [], "error": repr(exc)}


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    RAW.mkdir(parents=True)

    listings = []
    listing_metas = []
    for snapshot in (1, 2):
        response = fetch(TARGET)
        raw = response.content
        path = RAW / f"listing-{snapshot:02d}" / "companies.html"
        meta = {
            "requested_url": TARGET,
            "final_url": response.url,
            "status": response.status_code,
            "content_type": response.headers.get("content-type"),
            "date": response.headers.get("date"),
            "etag": response.headers.get("etag"),
            "fetched_at_epoch": time.time(),
        }
        save_bytes(path, raw, meta)
        listings.append(parse_listing(raw))
        listing_metas.append({**meta, "sha256": sha256(raw), "bytes": len(raw)})
        if snapshot == 1:
            time.sleep(2)

    if len(listings[0]) != 178 or len(listings[1]) != 178:
        raise RuntimeError(f"Unexpected listing counts: {len(listings[0])}, {len(listings[1])}")

    listing_map_1 = [(row["source_id"], row["name"], row["slug"], row["source_record_sha256"]) for row in listings[0]]
    listing_map_2 = [(row["source_id"], row["name"], row["slug"], row["source_record_sha256"]) for row in listings[1]]
    if listing_map_1 != listing_map_2:
        raise RuntimeError("Listing company-data snapshots differ")

    # Independently expand the client grid to its deterministic terminal state.
    click_log = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(user_agent=UA, viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        nav = page.goto(TARGET, wait_until="domcontentloaded", timeout=120_000)
        try:
            page.wait_for_load_state("networkidle", timeout=30_000)
        except Exception:
            pass
        page.wait_for_timeout(1500)

        island = page.locator('astro-island[component-url*="CompanyGrid"]')
        if island.count() != 1:
            raise RuntimeError(f"Expected one CompanyGrid island, found {island.count()}")

        def grid_state() -> dict:
            return island.evaluate(
                """
                root => {
                  const links = [...root.querySelectorAll('a[href^="/companies/"]')];
                  return {
                    totalLinks: links.length,
                    uniqueHrefs: [...new Set(links.map(a => a.href))].length,
                    visibleLinks: links.filter(a => !!(a.offsetWidth || a.offsetHeight || a.getClientRects().length)).length,
                    names: [...new Set(links.map(a => (a.innerText || a.textContent || '').replace(/\s+/g,' ').trim()).filter(Boolean))],
                  };
                }
                """
            )

        for iteration in range(30):
            before = grid_state()
            button = island.locator('button[data-see-more="true"]')
            visible_button = button.count() and button.is_visible()
            entry = {"iteration": iteration, "before": before, "button_visible": bool(visible_button)}
            if not visible_button:
                click_log.append(entry)
                break
            button.click()
            page.wait_for_timeout(300)
            after = grid_state()
            entry["after"] = after
            click_log.append(entry)
            if after["uniqueHrefs"] <= before["uniqueHrefs"]:
                raise RuntimeError(f"See More did not increase company count at iteration {iteration}")
        final_state = grid_state()
        if final_state["uniqueHrefs"] != 178:
            raise RuntimeError(f"Expanded grid count is {final_state['uniqueHrefs']}, expected 178")
        if island.locator('button[data-see-more="true"]').count() and island.locator('button[data-see-more="true"]').is_visible():
            raise RuntimeError("See More remains visible after reaching expected total")

        html = page.content().encode("utf-8")
        save_bytes(
            RAW / "browser" / "fully-expanded.html",
            html,
            {"url": page.url, "status": nav.status if nav else None, "fetched_at_epoch": time.time()},
        )
        page.screenshot(path=str(RAW / "browser" / "fully-expanded.png"), full_page=True)
        write_json(OUT / "browser-click-log.json", click_log)
        write_json(OUT / "browser-final-state.json", final_state)
        context.close()
        browser.close()

    details_1 = fetch_all_details(1, listings[1])
    details_2 = fetch_all_details(2, listings[1])

    detail_map_1 = [(row["source_id"], row["page_name"], row["website"], row["canonical_url"]) for row in details_1]
    detail_map_2 = [(row["source_id"], row["page_name"], row["website"], row["canonical_url"]) for row in details_2]
    if detail_map_1 != detail_map_2:
        differences = []
        for one, two in zip(details_1, details_2):
            if (one["page_name"], one["website"], one["canonical_url"]) != (two["page_name"], two["website"], two["canonical_url"]):
                differences.append({"snapshot_1": one, "snapshot_2": two})
        write_json(OUT / "detail-snapshot-differences.json", differences)
        raise RuntimeError(f"Detail parsed maps differ for {len(differences)} companies")

    # Website reachability and deterministic redirects are a separate audit dimension.
    website_checks = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(check_website, item) for item in details_2]
        for future in concurrent.futures.as_completed(futures):
            website_checks.append(future.result())
    website_checks.sort(key=lambda row: next(item["source_position"] for item in details_2 if item["source_id"] == row["source_id"]))
    write_json(OUT / "website-checks.json", website_checks)

    missing_websites = [row for row in details_2 if not row["website"]]
    name_mismatches = [row for row in details_2 if row["page_name"].casefold() != row["expected_name"].casefold()]
    duplicate_names = {key: value for key, value in Counter(row["expected_name"].casefold() for row in details_2).items() if value > 1}
    duplicate_slugs = {key: value for key, value in Counter(row["slug"] for row in details_2).items() if value > 1}
    duplicate_source_ids = {key: value for key, value in Counter(row["source_id"] for row in details_2).items() if value > 1}
    source_domains = [urlparse(row["website_root"]).netloc.casefold().removeprefix("www.") for row in details_2 if row["website_root"]]
    duplicate_domains = {key: value for key, value in Counter(source_domains).items() if value > 1}
    redirect_candidates = [
        check for check in website_checks
        if check.get("source_url") and check.get("final_normalized") and normalize_url(check["source_url"], keep_path=True) != check["final_normalized"]
    ]
    reachability_errors = [check for check in website_checks if check.get("error") or check.get("status") is None]

    summary = {
        "status": "DISCOVERY_COMPLETE",
        "target": TARGET,
        "listing_counts": [len(listings[0]), len(listings[1])],
        "listing_record_maps_equal": listing_map_1 == listing_map_2,
        "listing_raw_hashes": [listing_metas[0]["sha256"], listing_metas[1]["sha256"]],
        "listing_raw_hashes_equal": listing_metas[0]["sha256"] == listing_metas[1]["sha256"],
        "unique_source_ids": len({row["source_id"] for row in listings[1]}),
        "unique_slugs": len({row["slug"] for row in listings[1]}),
        "unique_names": len({row["name"].casefold() for row in listings[1]}),
        "highlighted_count": sum(row["highlighted"] for row in listings[1]),
        "status_counts": dict(sorted(Counter(row["status"] for row in listings[1]).items())),
        "industry_counts": dict(sorted(Counter(row["industry"] for row in listings[1]).items())),
        "browser_initial_grid_count": click_log[0]["before"]["uniqueHrefs"] if click_log else None,
        "browser_see_more_click_count": sum(1 for item in click_log if item.get("after")),
        "browser_final_grid_count": final_state["uniqueHrefs"],
        "browser_terminal_button_visible": bool(click_log[-1].get("button_visible")) if click_log else None,
        "detail_counts": [len(details_1), len(details_2)],
        "detail_maps_equal": detail_map_1 == detail_map_2,
        "missing_website_count": len(missing_websites),
        "missing_websites": missing_websites,
        "name_mismatch_count": len(name_mismatches),
        "name_mismatches": name_mismatches,
        "duplicate_source_ids": duplicate_source_ids,
        "duplicate_slugs": duplicate_slugs,
        "duplicate_names": duplicate_names,
        "duplicate_source_domain_groups": duplicate_domains,
        "website_check_count": len(website_checks),
        "website_check_status_counts": dict(sorted(Counter(str(row["status"]) if row.get("status") is not None else "ERROR" for row in website_checks).items())),
        "redirect_candidate_count": len(redirect_candidates),
        "redirect_candidates": redirect_candidates,
        "reachability_error_count": len(reachability_errors),
        "reachability_errors": reachability_errors,
    }
    write_json(OUT / "full-extraction-summary.json", summary)

    manifest = {"files": {}}
    for path in sorted(OUT.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            manifest["files"][str(path.relative_to(OUT))] = {"bytes": path.stat().st_size, "sha256": sha256(path.read_bytes())}
    write_json(OUT / "manifest.json", manifest)

    print("FULL_EXTRACTION_SUMMARY_START")
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    print("FULL_EXTRACTION_SUMMARY_END")


if __name__ == "__main__":
    main()
