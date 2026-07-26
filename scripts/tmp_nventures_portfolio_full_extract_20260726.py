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
from urllib.parse import urlparse

import requests
from playwright.sync_api import sync_playwright

TARGET = "https://www.nvidia.com/en-us/startups/nventures/portfolio/?ncid=no-ncid"
DATA_URL = "https://www.nvidia.com/content/dam/en-zz/nvidiaweb/startups/nventures/portfolio/companies.json"
OUT = Path("artifact")
RAW = OUT / "raw"
NORMALIZED = OUT / "normalized"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
WORKERS = 12


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def save_bytes(path: Path, raw: bytes, meta: dict | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    payload = dict(meta or {})
    payload.update({"bytes": len(raw), "sha256": sha256(raw)})
    write_json(path.with_suffix(path.suffix + ".meta.json"), payload)


def request_get(url: str, *, accept: str = "*/*", attempts: int = 6) -> tuple[bytes, dict]:
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(
                url,
                timeout=120,
                allow_redirects=True,
                headers={
                    "User-Agent": UA,
                    "Accept": accept,
                    "Accept-Language": "en-US,en;q=0.9",
                },
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
                "attempt": attempt,
                "fetched_at_epoch": time.time(),
            }
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < attempts:
                time.sleep(min(30, 2**attempt))
    raise RuntimeError(f"GET failed after {attempts} attempts: {url}: {last}")


def normalize_root(value: object) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
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
    return f"https://{host}" if host and "." in host else ""


def parse_data(raw: bytes) -> list[dict]:
    payload = json.loads(raw)
    if not isinstance(payload, list):
        raise ValueError("NVentures companies JSON must be a list")
    rows: list[dict] = []
    for position, record in enumerate(payload, start=1):
        if not isinstance(record, dict):
            raise ValueError(f"record {position} is not an object")
        name = re.sub(r"\s+", " ", str(record.get("companyName") or "")).strip()
        industry = re.sub(r"\s+", " ", str(record.get("industry") or "")).strip()
        source_website = re.sub(r"\s+", " ", str(record.get("websiteUrl") or "")).strip()
        logo_url = re.sub(r"\s+", " ", str(record.get("logoUrl") or "")).strip()
        website = normalize_root(source_website)
        rows.append(
            {
                "source_position": position,
                "name": name,
                "industry": industry,
                "source_website": source_website,
                "website": website,
                "website_domain": urlparse(website).netloc.casefold().removeprefix("www."),
                "logo_url": logo_url,
                "source_record_sha256": sha256(
                    json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
                ),
            }
        )
    return rows


def capture_source_snapshots() -> list[dict]:
    snapshots: list[dict] = []
    for number in (1, 2):
        root = RAW / f"snapshot-{number:02d}"
        page_raw, page_meta = request_get(TARGET, accept="text/html,application/xhtml+xml")
        data_raw, data_meta = request_get(DATA_URL, accept="application/json")
        save_bytes(root / "portfolio.html", page_raw, page_meta)
        save_bytes(root / "companies.json", data_raw, data_meta)
        rows = parse_data(data_raw)
        write_json(root / "parsed-companies.json", rows)
        snapshots.append(
            {
                "snapshot": number,
                "captured_at_epoch": time.time(),
                "page_sha256": sha256(page_raw),
                "data_sha256": sha256(data_raw),
                "company_count": len(rows),
                "map": [
                    (row["name"], row["industry"], row["source_website"], row["logo_url"])
                    for row in rows
                ],
            }
        )
    return snapshots


def browser_capture() -> dict:
    root = RAW / "browser"
    root.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=UA,
            viewport={"width": 1440, "height": 1000},
            locale="en-US",
        )
        page = context.new_page()
        page.goto(TARGET, wait_until="domcontentloaded", timeout=180_000)
        page.wait_for_selector("article.company-card", timeout=120_000)
        page.wait_for_timeout(4_000)

        for pattern in (
            re.compile(r"accept all", re.I),
            re.compile(r"accept cookies", re.I),
            re.compile(r"agree", re.I),
            re.compile(r"continue", re.I),
        ):
            locator = page.get_by_role("button", name=pattern)
            for index in range(min(locator.count(), 5)):
                try:
                    if locator.nth(index).is_visible():
                        locator.nth(index).click(timeout=2_000)
                        page.wait_for_timeout(500)
                        break
                except Exception:  # noqa: BLE001
                    pass

        def extract_cards() -> list[dict]:
            return page.locator("article.company-card").evaluate_all(
                """
                cards => cards.map((card, index) => {
                  const link = card.querySelector('a.company-card__inner');
                  const name = card.querySelector('h3');
                  const texts = [...card.querySelectorAll('p,span')]
                    .map(x => (x.textContent || '').replace(/\\s+/g, ' ').trim())
                    .filter(Boolean);
                  const image = card.querySelector('img');
                  return {
                    page_position: index + 1,
                    name: name ? name.textContent.replace(/\\s+/g, ' ').trim() : '',
                    website: link ? link.href : '',
                    industry: texts.length ? texts[texts.length - 1] : '',
                    logo_alt: image ? (image.alt || '') : '',
                    logo_src: image ? image.src : ''
                  };
                })
                """
            )

        initial_text = page.locator("body").inner_text()
        showing_match = re.search(r"Showing\s+([0-9,]+)\s+Results", initial_text, re.I)
        displayed_total = int(showing_match.group(1).replace(",", "")) if showing_match else None
        industry_counts = {}
        for match in re.finditer(r"^(.+?)\s+\(([0-9,]+)\)$", initial_text, re.M):
            label = match.group(1).strip()
            if label not in {"Page", "Display"}:
                industry_counts[label] = int(match.group(2).replace(",", ""))

        # Switch to the largest supported page size, proving the UI dataset in two pages.
        page_size = page.locator(".page-size select")
        if page_size.count() != 1:
            raise RuntimeError(f"expected one page-size selector, found {page_size.count()}")
        page_size.select_option("60")
        page.wait_for_function("document.querySelectorAll('article.company-card').length === 60", timeout=60_000)
        page.wait_for_timeout(1_000)
        page_1_cards = extract_cards()
        page_1_html = page.content().encode("utf-8")
        save_bytes(root / "page-001.html", page_1_html, {"page": 1, "page_size": 60, "fetched_at_epoch": time.time()})
        write_json(root / "page-001-cards.json", page_1_cards)
        page.screenshot(path=str(root / "page-001.png"), full_page=True)

        page_select = page.locator('nav[aria-label="Bottom pagination"] select')
        if page_select.count() != 1:
            raise RuntimeError(f"expected one bottom page selector, found {page_select.count()}")
        page_select.select_option("2")
        page.wait_for_function("document.querySelectorAll('article.company-card').length === 36", timeout=60_000)
        page.wait_for_timeout(1_000)
        page_2_cards = extract_cards()
        page_2_html = page.content().encode("utf-8")
        save_bytes(root / "page-002.html", page_2_html, {"page": 2, "page_size": 60, "fetched_at_epoch": time.time()})
        write_json(root / "page-002-cards.json", page_2_cards)
        page.screenshot(path=str(root / "page-002.png"), full_page=True)

        final_text = page.locator("body").inner_text()
        range_match = re.search(r"([0-9,]+)-([0-9,]+)\s+of\s+([0-9,]+)\s+Items", final_text, re.I)
        page_range = (
            [int(range_match.group(i).replace(",", "")) for i in (1, 2, 3)]
            if range_match
            else None
        )
        all_cards = page_1_cards + page_2_cards
        write_json(root / "all-ui-cards.json", all_cards)
        browser.close()

    return {
        "displayed_total": displayed_total,
        "industry_counts": industry_counts,
        "industry_count_sum": sum(industry_counts.values()),
        "page_size": 60,
        "page_1_count": len(page_1_cards),
        "page_2_count": len(page_2_cards),
        "page_2_range": page_range,
        "all_cards": all_cards,
        "page_1_sha256": sha256(page_1_html),
        "page_2_sha256": sha256(page_2_html),
    }


def check_website(row: dict) -> dict:
    url = row["source_website"]
    result = {
        "name": row["name"],
        "source_url": url,
        "source_root": row["website"],
        "status": None,
        "final_url": None,
        "final_root": None,
        "history": [],
        "error": None,
    }
    try:
        response = requests.get(
            url,
            timeout=30,
            allow_redirects=True,
            stream=True,
            headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"},
        )
        result["status"] = response.status_code
        result["final_url"] = response.url
        result["final_root"] = normalize_root(response.url)
        result["history"] = [
            {"status": item.status_code, "url": item.url, "location": item.headers.get("location")}
            for item in response.history
        ]
        response.close()
    except Exception as exc:  # noqa: BLE001
        result["error"] = repr(exc)
    return result


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    RAW.mkdir(parents=True)
    NORMALIZED.mkdir(parents=True)

    snapshots = capture_source_snapshots()
    rows = parse_data((RAW / "snapshot-02" / "companies.json").read_bytes())
    browser = browser_capture()

    checks: list[dict] = []
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = {executor.submit(check_website, row): row for row in rows}
        for completed, future in enumerate(as_completed(futures), start=1):
            result = future.result()
            with lock:
                checks.append(result)
            if completed % 20 == 0:
                print(f"website checks: {completed}/{len(rows)}", flush=True)
    checks.sort(key=lambda item: next(row["source_position"] for row in rows if row["name"] == item["name"]))
    write_json(OUT / "website-checks.json", checks)

    redirect_candidates = [
        item
        for item in checks
        if item.get("final_root") and item["final_root"] != item["source_root"]
    ]
    reachability_errors = [item for item in checks if item.get("error")]
    write_json(OUT / "redirect-review-candidates.json", redirect_candidates)
    write_json(OUT / "reachability-errors.json", reachability_errors)

    ui_map = [
        (
            re.sub(r"\s+", " ", card.get("name") or "").strip(),
            re.sub(r"\s+", " ", card.get("industry") or "").strip(),
            card.get("website") or "",
        )
        for card in browser["all_cards"]
    ]
    data_map = [
        (row["name"], row["industry"], row["source_website"])
        for row in rows
    ]

    names = [row["name"].casefold() for row in rows]
    domains = [row["website_domain"] for row in rows]
    industries = Counter(row["industry"] for row in rows)
    snapshot_comparison = {
        "status": "PASS" if snapshots[0]["map"] == snapshots[1]["map"] else "FAIL",
        "snapshot_1_count": snapshots[0]["company_count"],
        "snapshot_2_count": snapshots[1]["company_count"],
        "data_hashes_equal": snapshots[0]["data_sha256"] == snapshots[1]["data_sha256"],
        "page_hashes_equal": snapshots[0]["page_sha256"] == snapshots[1]["page_sha256"],
        "maps_equal": snapshots[0]["map"] == snapshots[1]["map"],
        "snapshot_1": snapshots[0],
        "snapshot_2": snapshots[1],
    }
    write_json(OUT / "snapshot-comparison.json", snapshot_comparison)

    txt_lines = [f'{row["name"]} + {row["website"]}' for row in rows]
    txt_path = NORMALIZED / "nventures-portfolio-companies.txt"
    json_path = NORMALIZED / "nventures-portfolio-companies.json"
    txt_path.write_text("\n".join(txt_lines) + "\n", encoding="utf-8")
    write_json(json_path, rows)

    hard_checks = {
        "source_json_count_is_96": len(rows) == 96,
        "two_source_maps_equal": snapshots[0]["map"] == snapshots[1]["map"],
        "two_data_hashes_equal": snapshots[0]["data_sha256"] == snapshots[1]["data_sha256"],
        "browser_displayed_total_is_96": browser["displayed_total"] == 96,
        "browser_ui_pages_are_60_plus_36": browser["page_1_count"] == 60 and browser["page_2_count"] == 36,
        "browser_second_page_range_is_61_96_of_96": browser["page_2_range"] == [61, 96, 96],
        "industry_filter_sum_is_96": browser["industry_count_sum"] == 96,
        "source_industry_sum_is_96": sum(industries.values()) == 96,
        "industry_counts_match_ui": dict(sorted(industries.items())) == dict(sorted(browser["industry_counts"].items())),
        "ui_map_equals_source_map": ui_map == data_map,
        "unique_names_equal_96": len(set(names)) == 96,
        "unique_domains_equal_96": len(set(domains)) == 96,
        "missing_names_zero": all(row["name"] for row in rows),
        "missing_industries_zero": all(row["industry"] for row in rows),
        "missing_websites_zero": all(row["website"] for row in rows),
        "invalid_websites_zero": all(urlparse(row["website"]).scheme == "https" and urlparse(row["website"]).netloc for row in rows),
        "txt_line_count_is_96": len(txt_lines) == 96,
    }
    validation_status = "PASS" if all(hard_checks.values()) else "FAIL"
    validation = {
        "status": validation_status,
        "source_url": TARGET,
        "data_url": DATA_URL,
        "captured_at_epoch": time.time(),
        "source_company_count": len(rows),
        "browser_displayed_total": browser["displayed_total"],
        "browser_page_counts": [browser["page_1_count"], browser["page_2_count"]],
        "browser_page_2_range": browser["page_2_range"],
        "unique_name_count": len(set(names)),
        "unique_website_domain_count": len(set(domains)),
        "missing_name_count": sum(not row["name"] for row in rows),
        "missing_industry_count": sum(not row["industry"] for row in rows),
        "missing_website_count": sum(not row["website"] for row in rows),
        "duplicate_name_groups": {key: value for key, value in Counter(names).items() if value > 1},
        "duplicate_domain_groups": {key: value for key, value in Counter(domains).items() if value > 1},
        "industry_counts": dict(sorted(industries.items())),
        "browser_industry_counts": dict(sorted(browser["industry_counts"].items())),
        "website_check_count": len(checks),
        "website_check_error_count": len(reachability_errors),
        "redirect_candidate_count": len(redirect_candidates),
        "snapshot_comparison": snapshot_comparison,
        "hard_checks": hard_checks,
        "notes": [
            "Source completeness is established from NVIDIA's own companies.json plus the rendered 60+36 pagination path.",
            "Website reachability and redirect status are reported separately and do not alter source-set completeness.",
            "Final normalized URLs in this preliminary extraction are HTTPS root domains from NVIDIA's explicit websiteUrl fields.",
        ],
    }
    write_json(OUT / "validation.json", validation)

    # Independent audit reparses exact raw JSON and rendered card captures.
    raw_1 = (RAW / "snapshot-01" / "companies.json").read_bytes()
    raw_2 = (RAW / "snapshot-02" / "companies.json").read_bytes()
    meta_1 = json.loads((RAW / "snapshot-01" / "companies.json.meta.json").read_text(encoding="utf-8"))
    meta_2 = json.loads((RAW / "snapshot-02" / "companies.json.meta.json").read_text(encoding="utf-8"))
    reconstructed_1 = parse_data(raw_1)
    reconstructed_2 = parse_data(raw_2)
    reconstructed_map_1 = [(row["name"], row["industry"], row["source_website"]) for row in reconstructed_1]
    reconstructed_map_2 = [(row["name"], row["industry"], row["source_website"]) for row in reconstructed_2]
    actual_txt = [line for line in txt_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    expected_txt = [f'{row["name"]} + {row["website"]}' for row in reconstructed_2]
    audit_checks = {
        "raw_snapshot_1_hash_matches": sha256(raw_1) == meta_1.get("sha256"),
        "raw_snapshot_2_hash_matches": sha256(raw_2) == meta_2.get("sha256"),
        "raw_reconstructions_each_have_96": len(reconstructed_1) == len(reconstructed_2) == 96,
        "raw_reconstruction_maps_equal": reconstructed_map_1 == reconstructed_map_2,
        "ui_map_equals_raw_reconstruction": ui_map == reconstructed_map_2,
        "txt_lines_equal_reconstructed_output": actual_txt == expected_txt,
    }
    audit = {
        "status": "PASS" if all(audit_checks.values()) else "FAIL",
        "method": "Reparse two exact NVIDIA companies.json responses, verify SHA-256 metadata, compare against separately captured rendered UI pages, and regenerate the TXT lines without reading normalized JSON as a source.",
        "checks": audit_checks,
        "reconstructed_counts": [len(reconstructed_1), len(reconstructed_2)],
        "ui_count": len(ui_map),
        "txt_line_count": len(actual_txt),
    }
    write_json(OUT / "independent-audit.json", audit)

    if validation_status != "PASS" or audit["status"] != "PASS":
        raise RuntimeError("NVentures validation or independent audit failed")

    manifest = {"generated_at_epoch": time.time(), "status": "PASS", "files": {}}
    for path in sorted(OUT.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            manifest["files"][str(path.relative_to(OUT))] = {
                "bytes": path.stat().st_size,
                "sha256": sha256(path.read_bytes()),
            }
    write_json(OUT / "manifest.json", manifest)

    print("VALIDATION_SUMMARY_START")
    print(json.dumps({
        "status": validation_status,
        "source_company_count": len(rows),
        "browser_displayed_total": browser["displayed_total"],
        "browser_page_counts": [browser["page_1_count"], browser["page_2_count"]],
        "industry_count_sum": sum(industries.values()),
        "unique_name_count": len(set(names)),
        "unique_domain_count": len(set(domains)),
        "website_check_error_count": len(reachability_errors),
        "redirect_candidate_count": len(redirect_candidates),
        "snapshot_comparison": snapshot_comparison["status"],
        "independent_audit": audit["status"],
    }, ensure_ascii=False, indent=2, sort_keys=True))
    print("VALIDATION_SUMMARY_END")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        OUT.mkdir(parents=True, exist_ok=True)
        write_json(OUT / "fatal-error.json", {"error": repr(exc), "traceback": traceback.format_exc()})
        raise
