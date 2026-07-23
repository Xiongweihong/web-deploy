from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

TARGET = "https://www.dragonfly.xyz/"
SANITY_PROJECT = "oqc9t74k"
SANITY_DATASET = "production"
SANITY_ENDPOINT = (
    f"https://{SANITY_PROJECT}.apicdn.sanity.io/v2026-07-01/"
    f"data/query/{SANITY_DATASET}"
)
OUT = Path("artifact")
RAW = OUT / "raw"
USER_AGENT = "Mozilla/5.0 (compatible; dragonfly-home-evidence-crawler/1.0)"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def save_bytes(path: Path, data: bytes, meta: dict | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    payload = dict(meta or {})
    payload.update({"bytes": len(data), "sha256": sha256(data)})
    path.with_suffix(path.suffix + ".meta.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def save_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def fetch(url: str, *, params: dict | None = None) -> requests.Response:
    last: Exception | None = None
    for attempt in range(1, 6):
        try:
            response = requests.get(
                url,
                params=params,
                timeout=90,
                headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
            )
            response.raise_for_status()
            return response
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < 5:
                time.sleep(min(20, 2**attempt))
    raise RuntimeError(f"GET failed: {url}: {last}")


def sanity_query(name: str, query: str) -> dict:
    response = fetch(SANITY_ENDPOINT, params={"query": query})
    save_bytes(
        RAW / "sanity" / f"{name}.json",
        response.content,
        {
            "requested_url": response.request.url,
            "status": response.status_code,
            "content_type": response.headers.get("content-type"),
            "date": response.headers.get("date"),
            "query": query,
        },
    )
    return response.json()


def static_capture(index: int) -> dict:
    response = fetch(TARGET)
    path = RAW / f"static-{index:02d}" / "index.html"
    save_bytes(
        path,
        response.content,
        {
            "requested_url": response.request.url,
            "final_url": response.url,
            "status": response.status_code,
            "content_type": response.headers.get("content-type"),
            "date": response.headers.get("date"),
            "etag": response.headers.get("etag"),
            "fetched_at_epoch": time.time(),
        },
    )
    soup = BeautifulSoup(response.content, "lxml")
    scripts = [
        {
            "src": script.get("src"),
            "type": script.get("type"),
            "id": script.get("id"),
            "text_prefix": (script.string or script.get_text(" ", strip=True))[:500],
        }
        for script in soup.find_all("script")
    ]
    links = [
        {
            "text": a.get_text(" ", strip=True),
            "href": a.get("href"),
            "class": a.get("class"),
        }
        for a in soup.find_all("a")
    ]
    save_json(path.parent / "scripts.json", scripts)
    save_json(path.parent / "links.json", links)
    return {
        "sha256": sha256(response.content),
        "bytes": len(response.content),
        "script_count": len(scripts),
        "link_count": len(links),
    }


def capture_browser() -> dict:
    browser_dir = RAW / "browser"
    browser_dir.mkdir(parents=True, exist_ok=True)
    network_records: list[dict] = []
    response_counter = 0

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 1200},
            locale="en-US",
        )
        page = context.new_page()

        def on_response(response) -> None:
            nonlocal response_counter
            url = response.url
            if not any(
                token in url
                for token in (
                    "sanity.io",
                    "sanitycdn.com",
                    "/api/",
                    "_next/data",
                    "query",
                )
            ):
                return
            response_counter += 1
            try:
                body = response.body()
            except Exception:  # noqa: BLE001
                return
            suffix = ".json" if "json" in (response.headers.get("content-type") or "") else ".bin"
            safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", urlparse(url).path.strip("/") or "root")
            path = browser_dir / "network" / f"{response_counter:04d}_{safe[:120]}{suffix}"
            meta = {
                "url": url,
                "status": response.status,
                "content_type": response.headers.get("content-type"),
                "request_method": response.request.method,
                "request_post_data": response.request.post_data,
                "fetched_at_epoch": time.time(),
            }
            save_bytes(path, body, meta)
            network_records.append({**meta, "path": str(path)})

        page.on("response", on_response)
        page.goto(TARGET, wait_until="domcontentloaded", timeout=120_000)
        page.wait_for_timeout(6_000)

        def dump(stage: str) -> dict:
            html = page.content().encode("utf-8")
            save_bytes(
                browser_dir / f"{stage}.html",
                html,
                {"url": page.url, "fetched_at_epoch": time.time()},
            )
            body_text = page.locator("body").inner_text()
            (browser_dir / f"{stage}.txt").write_text(body_text, encoding="utf-8")
            page.screenshot(path=str(browser_dir / f"{stage}.png"), full_page=True)
            payload = page.evaluate(
                """
                () => {
                  const visible = (el) => {
                    const s = getComputedStyle(el);
                    const r = el.getBoundingClientRect();
                    return s.display !== 'none' && s.visibility !== 'hidden' &&
                           Number(s.opacity || 1) > 0 && r.width > 0 && r.height > 0;
                  };
                  const simple = (el) => {
                    const r = el.getBoundingClientRect();
                    const attrs = {};
                    for (const a of el.attributes || []) {
                      if (/^(href|target|rel|data-|aria-|role|class|id)/.test(a.name)) attrs[a.name] = a.value;
                    }
                    return {
                      tag: el.tagName,
                      text: (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim(),
                      href: el.href || null,
                      visible: visible(el),
                      rect: {x:r.x, y:r.y, width:r.width, height:r.height},
                      attrs,
                    };
                  };
                  const anchors = [...document.querySelectorAll('a')].map(simple);
                  const controls = [...document.querySelectorAll('button,[role=button]')].map(simple);
                  const leaf = [...document.querySelectorAll('body *')]
                    .filter(el => el.children.length === 0)
                    .map(simple)
                    .filter(x => x.visible && x.text && x.text.length <= 160);
                  const findExact = (txt) => [...document.querySelectorAll('body *')]
                    .filter(el => el.children.length === 0 && (el.textContent || '').trim() === txt)
                    .map(simple);
                  return {
                    title: document.title,
                    url: location.href,
                    scrollHeight: document.documentElement.scrollHeight,
                    anchors,
                    controls,
                    leaf,
                    indexNodes: findExact('Index'),
                    careersNodes: findExact('Careers'),
                    loadMoreNodes: findExact('Load More'),
                  };
                }
                """
            )
            save_json(browser_dir / f"{stage}-dom.json", payload)
            return payload

        stage_payloads: list[dict] = []
        stage_payloads.append(dump("stage-00"))

        click_count = 0
        for _ in range(30):
            info = page.evaluate(
                """
                () => {
                  const visible = (el) => {
                    const s = getComputedStyle(el);
                    const r = el.getBoundingClientRect();
                    return s.display !== 'none' && s.visibility !== 'hidden' &&
                           Number(s.opacity || 1) > 0 && r.width > 0 && r.height > 0;
                  };
                  const leaves = [...document.querySelectorAll('body *')]
                    .filter(el => el.children.length === 0);
                  const index = leaves.find(el => (el.textContent || '').trim() === 'Index');
                  const careers = leaves.find(el => (el.textContent || '').trim() === 'Careers');
                  const iy = index ? index.getBoundingClientRect().top + scrollY : 0;
                  const cy = careers ? careers.getBoundingClientRect().top + scrollY : Infinity;
                  const candidates = [...document.querySelectorAll('button,[role=button],a')]
                    .filter(el => visible(el) && (el.textContent || '').trim() === 'Load More')
                    .map(el => ({
                      y: el.getBoundingClientRect().top + scrollY,
                      tag: el.tagName,
                      text: (el.textContent || '').trim(),
                    }))
                    .filter(x => x.y > iy && x.y < cy)
                    .sort((a,b) => a.y-b.y);
                  return {iy, cy, candidates};
                }
                """
            )
            save_json(browser_dir / f"click-candidates-{click_count:02d}.json", info)
            if not info["candidates"]:
                break
            target_y = info["candidates"][0]["y"]
            target = page.locator("button, [role=button], a").filter(has_text="Load More")
            chosen = None
            for i in range(target.count()):
                locator = target.nth(i)
                try:
                    if not locator.is_visible():
                        continue
                    box = locator.bounding_box()
                    if not box:
                        continue
                    abs_y = box["y"] + page.evaluate("window.scrollY")
                    if abs(abs_y - target_y) < 10:
                        chosen = locator
                        break
                except Exception:  # noqa: BLE001
                    continue
            if chosen is None:
                break
            chosen.scroll_into_view_if_needed()
            before = page.locator("body").inner_text()
            try:
                chosen.click(timeout=10_000)
            except PlaywrightTimeoutError:
                chosen.click(timeout=10_000, force=True)
            page.wait_for_timeout(2_500)
            after = page.locator("body").inner_text()
            click_count += 1
            stage_payloads.append(dump(f"stage-{click_count:02d}"))
            if after == before:
                break

        # Capture a focused portfolio section using vertical bounds between Index and Careers.
        portfolio = page.evaluate(
            """
            () => {
              const visible = (el) => {
                const s = getComputedStyle(el);
                const r = el.getBoundingClientRect();
                return s.display !== 'none' && s.visibility !== 'hidden' &&
                       Number(s.opacity || 1) > 0 && r.width > 0 && r.height > 0;
              };
              const leaves = [...document.querySelectorAll('body *')]
                .filter(el => el.children.length === 0);
              const index = leaves.find(el => (el.textContent || '').trim() === 'Index');
              const careers = leaves.find(el => (el.textContent || '').trim() === 'Careers');
              const iy = index ? index.getBoundingClientRect().top + scrollY : 0;
              const cy = careers ? careers.getBoundingClientRect().top + scrollY : Infinity;
              const nodes = [...document.querySelectorAll('body *')]
                .filter(el => visible(el))
                .map(el => {
                  const r = el.getBoundingClientRect();
                  return {
                    tag: el.tagName,
                    text: (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim(),
                    href: el.href || null,
                    className: typeof el.className === 'string' ? el.className : '',
                    id: el.id || '',
                    y: r.top + scrollY,
                    height: r.height,
                    childCount: el.children.length,
                  };
                })
                .filter(x => x.y >= iy && x.y < cy && x.text && x.text.length <= 240);
              return {indexY: iy, careersY: cy, nodes};
            }
            """
        )
        save_json(browser_dir / "portfolio-visible-nodes.json", portfolio)
        save_json(browser_dir / "network-index.json", network_records)
        browser.close()

    return {
        "load_more_clicks": click_count,
        "network_response_count": len(network_records),
        "stage_count": len(stage_payloads),
        "final_scroll_height": stage_payloads[-1]["scrollHeight"] if stage_payloads else None,
        "final_anchor_count": len(stage_payloads[-1]["anchors"]) if stage_payloads else None,
    }


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    RAW.mkdir(parents=True)

    static = [static_capture(1), static_capture(2)]

    types = sanity_query("types", "array::unique(*[]._type)")
    type_names = sorted(types.get("result") or []) if isinstance(types, dict) else []
    interesting_types = [
        value
        for value in type_names
        if any(token in value.casefold() for token in ("portfolio", "company", "investment", "home", "page"))
    ]
    save_json(OUT / "sanity-types-summary.json", {"types": type_names, "interesting": interesting_types})

    projection = r'''*[]{
      _id,_type,_createdAt,_updatedAt,
      name,title,label,url,website,link,href,externalUrl,
      slug,category,categories,sector,sectors,stage,status,
      "keys": keys(@)
    }'''
    sanity_query("all-docs-projection", projection)
    for type_name in interesting_types:
        safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", type_name)
        sanity_query(f"type-{safe}", f'*[_type == {json.dumps(type_name)}]')

    browser = capture_browser()
    summary = {
        "target": TARGET,
        "static": static,
        "static_hashes_equal": static[0]["sha256"] == static[1]["sha256"],
        "sanity_project": SANITY_PROJECT,
        "sanity_dataset": SANITY_DATASET,
        "sanity_types": type_names,
        "interesting_types": interesting_types,
        "browser": browser,
        "generated_at_epoch": time.time(),
    }
    save_json(OUT / "discovery-summary.json", summary)

    manifest = {"files": {}}
    for path in sorted(OUT.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            manifest["files"][str(path.relative_to(OUT))] = {
                "bytes": path.stat().st_size,
                "sha256": sha256(path.read_bytes()),
            }
    save_json(OUT / "manifest.json", manifest)
    print("DISCOVERY_SUMMARY_START")
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    print("DISCOVERY_SUMMARY_END")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        OUT.mkdir(parents=True, exist_ok=True)
        save_json(
            OUT / "fatal-error.json",
            {"error": repr(exc), "traceback": traceback.format_exc()},
        )
        raise
