from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

TARGET = "https://www.indexventures.com/companies/"
OUT = Path("artifact")
OUT.mkdir(parents=True, exist_ok=True)
SHARD = int(os.environ["SHARD"])
TOTAL_SHARDS = int(os.environ.get("TOTAL_SHARDS", "8"))
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)


def dump(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def clean_url(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if value.startswith("//"):
        value = "https:" + value
    if not re.match(r"^https?://", value, re.I):
        value = "https://" + value.lstrip("/")
    return value.rstrip("/")


def source_companies() -> list[dict]:
    response = requests.get(TARGET, headers={"User-Agent": UA}, timeout=120)
    response.raise_for_status()
    (OUT / f"index-main-shard-{SHARD}.html").write_bytes(response.content)
    soup = BeautifulSoup(response.content, "lxml")
    collection = None
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            payload = json.loads(script.get_text())
        except Exception:
            continue
        graph = payload.get("@graph", []) if isinstance(payload, dict) else []
        for node in graph:
            if isinstance(node, dict) and node.get("@type") == "CollectionPage":
                entity = node.get("mainEntity") or {}
                if entity.get("@type") == "ItemList":
                    collection = entity
                    break
        if collection:
            break
    if not collection:
        raise RuntimeError("Index Ventures JSON-LD company collection not found")
    items = collection.get("itemListElement") or []
    records = []
    for item in items:
        records.append(
            {
                "position": int(item["position"]),
                "source_id": str(item["url"]).rstrip("/").rsplit("/", 1)[-1],
                "source_name": str(item["name"]).strip(),
                "detail_url": str(item["url"]),
            }
        )
    if len(records) != 321:
        raise RuntimeError(f"Expected 321 Index companies, got {len(records)}")
    return records


def extract_website(page) -> str:
    return page.evaluate(
        r"""
        () => {
          const norm = s => (s || '').replace(/\s+/g, ' ').trim();
          const labels = [...document.querySelectorAll('dt,dd,li,div,p,span,strong')]
            .filter(el => norm(el.textContent).toLowerCase() === 'website');
          for (const label of labels) {
            let node = label;
            for (let depth = 0; depth < 7 && node; depth++, node = node.parentElement) {
              const links = [...node.querySelectorAll('a[href]')].filter(a => {
                try {
                  const host = new URL(a.href).hostname.replace(/^www\./, '');
                  return host && host !== 'indexventures.com';
                } catch { return false; }
              });
              if (links.length) return links[0].href;
            }
          }
          return '';
        }
        """
    )


def one_snapshot(records: list[dict], snapshot: int) -> list[dict]:
    output = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 900})
        context.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in {"image", "media", "font"}
            else route.continue_(),
        )
        page = context.new_page()
        page.goto(TARGET, wait_until="domcontentloaded", timeout=120_000)
        page.wait_for_timeout(500)

        for ordinal, row in enumerate(records, start=1):
            current = dict(row)
            success = False
            attempts = []
            for attempt in range(1, 6):
                try:
                    response = page.goto(row["detail_url"], wait_until="domcontentloaded", timeout=45_000)
                    status = response.status if response else None
                    title = page.title()
                    h1 = page.locator("h1").first.inner_text(timeout=10_000).strip()
                    website = clean_url(extract_website(page))
                    attempts.append({"attempt": attempt, "status": status, "title": title})
                    if status == 200 and h1 and h1.casefold() != "403 error":
                        current.update(
                            {
                                "name": h1,
                                "website": website,
                                "detail_status": status,
                                "detail_title": title,
                                "detail_final_url": page.url,
                                "snapshot": snapshot,
                            }
                        )
                        success = True
                        break
                except Exception as exc:
                    attempts.append({"attempt": attempt, "error": repr(exc)})
                page.wait_for_timeout(1500 * attempt)
                if attempt in {2, 4}:
                    try:
                        page.close()
                    except Exception:
                        pass
                    page = context.new_page()
                    page.goto(TARGET, wait_until="domcontentloaded", timeout=120_000)
                    page.wait_for_timeout(1000)
            current["attempts"] = attempts
            if not success:
                current["error"] = "Unable to obtain a non-403 official detail page"
            output.append(current)
            page.wait_for_timeout(700)
            if ordinal % 10 == 0:
                dump(
                    OUT / f"index-shard-{SHARD}-snapshot-{snapshot}-progress.json",
                    {
                        "processed": ordinal,
                        "assigned": len(records),
                        "success": sum("error" not in item for item in output),
                    },
                )
        browser.close()
    return output


def main() -> None:
    all_records = source_companies()
    assigned = [row for index, row in enumerate(all_records) if index % TOTAL_SHARDS == SHARD]
    first = one_snapshot(assigned, 1)
    time.sleep(3)
    second = one_snapshot(assigned, 2)

    first_map = {row["source_id"]: (row.get("name"), row.get("website"), row.get("detail_status")) for row in first}
    second_map = {row["source_id"]: (row.get("name"), row.get("website"), row.get("detail_status")) for row in second}
    errors = [row for row in first + second if row.get("error")]
    payload = {
        "shard": SHARD,
        "total_shards": TOTAL_SHARDS,
        "total_source_count": len(all_records),
        "assigned_count": len(assigned),
        "snapshot_1": first,
        "snapshot_2": second,
        "snapshot_maps_equal": first_map == second_map,
        "error_count": len(errors),
    }
    dump(OUT / f"index-shard-{SHARD}.json", payload)
    print("INDEX_SHARD_RESULT")
    print(json.dumps({
        "shard": SHARD,
        "assigned": len(assigned),
        "snapshot_1_success": sum("error" not in row for row in first),
        "snapshot_2_success": sum("error" not in row for row in second),
        "snapshot_maps_equal": payload["snapshot_maps_equal"],
        "errors": len(errors),
    }, indent=2))
    if errors or not payload["snapshot_maps_equal"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
