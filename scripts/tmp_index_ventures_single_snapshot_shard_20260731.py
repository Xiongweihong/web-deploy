from __future__ import annotations

import json
import os
import re
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

TARGET = "https://www.indexventures.com/companies/"
OUT = Path("artifact")
OUT.mkdir(parents=True, exist_ok=True)
SNAPSHOT = int(os.environ["SNAPSHOT"])
SHARD = int(os.environ["SHARD"])
TOTAL_SHARDS = int(os.environ.get("TOTAL_SHARDS", "20"))
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
    value = value.replace("http://www.", "https://").replace("https://www.", "https://")
    return value.rstrip("/")


def source_companies() -> list[dict]:
    response = requests.get(TARGET, headers={"User-Agent": UA}, timeout=120)
    response.raise_for_status()
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
    records = [
        {
            "position": int(item["position"]),
            "source_id": str(item["url"]).rstrip("/").rsplit("/", 1)[-1],
            "source_name": str(item["name"]).strip(),
            "detail_url": str(item["url"]),
        }
        for item in (collection.get("itemListElement") or [])
    ]
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


def main() -> None:
    all_records = source_companies()
    assigned = [row for index, row in enumerate(all_records) if index % TOTAL_SHARDS == SHARD]
    output = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 900})
        context.add_cookies([
            {
                "name": "wtm",
                "value": "necessary:true,media:false,analytics:false",
                "domain": "www.indexventures.com",
                "path": "/",
                "secure": True,
            }
        ])
        context.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in {"image", "media", "font"}
            else route.continue_(),
        )
        page = context.new_page()
        page.goto(TARGET, wait_until="domcontentloaded", timeout=120_000)
        page.wait_for_timeout(500)
        for row in assigned:
            current = dict(row)
            response = page.goto(row["detail_url"], wait_until="domcontentloaded", timeout=45_000)
            status = response.status if response else None
            title = page.title()
            h1 = page.locator("h1").first.inner_text(timeout=10_000).strip()
            website = clean_url(extract_website(page))
            current.update(
                {
                    "name": h1,
                    "website": website,
                    "detail_status": status,
                    "detail_title": title,
                    "detail_final_url": page.url,
                    "snapshot": SNAPSHOT,
                    "shard": SHARD,
                }
            )
            if status != 200 or not h1 or h1.casefold() == "403 error":
                current["error"] = f"Invalid official detail response: status={status}, h1={h1!r}"
            output.append(current)
            page.wait_for_timeout(350)
        browser.close()

    errors = [row for row in output if row.get("error")]
    payload = {
        "snapshot": SNAPSHOT,
        "shard": SHARD,
        "total_shards": TOTAL_SHARDS,
        "total_source_count": len(all_records),
        "assigned_count": len(assigned),
        "records": output,
        "success_count": len(output) - len(errors),
        "error_count": len(errors),
    }
    dump(OUT / f"index-snapshot-{SNAPSHOT}-shard-{SHARD}.json", payload)
    print("INDEX_SINGLE_SHARD_RESULT")
    print(json.dumps({
        "snapshot": SNAPSHOT,
        "shard": SHARD,
        "assigned": len(assigned),
        "success": len(output) - len(errors),
        "errors": len(errors),
    }, indent=2))
    if errors:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
