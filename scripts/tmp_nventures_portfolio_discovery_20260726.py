from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
import traceback
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

TARGET = "https://www.nvidia.com/en-us/startups/nventures/portfolio/?ncid=no-ncid"
OUT = Path("artifact")
RAW = OUT / "raw"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"


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


def fetch(url: str, *, attempts: int = 6) -> requests.Response:
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(
                url,
                timeout=120,
                allow_redirects=True,
                headers={
                    "User-Agent": UA,
                    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )
            response.raise_for_status()
            return response
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < attempts:
                time.sleep(min(30, 2**attempt))
    raise RuntimeError(f"GET failed after {attempts} attempts: {url}: {last}")


def static_capture(index: int) -> dict:
    response = fetch(TARGET)
    root = RAW / f"static-{index:02d}"
    save_bytes(
        root / "portfolio.html",
        response.content,
        {
            "requested_url": response.request.url,
            "final_url": response.url,
            "status": response.status_code,
            "content_type": response.headers.get("content-type"),
            "date": response.headers.get("date"),
            "etag": response.headers.get("etag"),
            "last_modified": response.headers.get("last-modified"),
            "fetched_at_epoch": time.time(),
        },
    )
    soup = BeautifulSoup(response.content, "lxml")
    scripts = []
    for script in soup.find_all("script"):
        scripts.append(
            {
                "src": script.get("src"),
                "type": script.get("type"),
                "id": script.get("id"),
                "data_attrs": {
                    key: value for key, value in script.attrs.items() if str(key).startswith("data-")
                },
                "text_prefix": (script.string or script.get_text(" ", strip=True))[:1000],
            }
        )
    anchors = [
        {
            "text": a.get_text(" ", strip=True),
            "href": a.get("href"),
            "class": a.get("class"),
            "aria_label": a.get("aria-label"),
            "title": a.get("title"),
        }
        for a in soup.find_all("a")
    ]
    images = [
        {
            "alt": img.get("alt"),
            "src": img.get("src"),
            "data_src": img.get("data-src"),
            "srcset": img.get("srcset"),
            "class": img.get("class"),
        }
        for img in soup.find_all("img")
    ]
    write_json(root / "scripts.json", scripts)
    write_json(root / "anchors.json", anchors)
    write_json(root / "images.json", images)
    return {
        "sha256": sha256(response.content),
        "bytes": len(response.content),
        "script_count": len(scripts),
        "anchor_count": len(anchors),
        "image_count": len(images),
    }


def safe_name(url: str, sequence: int, suffix: str) -> str:
    parsed = urlparse(url)
    token = re.sub(r"[^a-zA-Z0-9._-]+", "_", f"{parsed.netloc}_{parsed.path}").strip("_")
    return f"{sequence:04d}_{token[:150] or 'root'}{suffix}"


def capture_browser() -> dict:
    root = RAW / "browser"
    root.mkdir(parents=True, exist_ok=True)
    network: list[dict] = []
    response_counter = 0

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=UA,
            viewport={"width": 1440, "height": 1000},
            locale="en-US",
            java_script_enabled=True,
        )
        page = context.new_page()

        def on_response(response) -> None:
            nonlocal response_counter
            url = response.url
            content_type = response.headers.get("content-type") or ""
            lower = url.casefold()
            relevant = any(
                token in lower
                for token in (
                    "portfolio",
                    "nventures",
                    "startup",
                    "graphql",
                    "/api/",
                    ".json",
                    "content/dam",
                    "nvidia.com",
                )
            )
            if not relevant:
                return
            response_counter += 1
            record = {
                "sequence": response_counter,
                "url": url,
                "status": response.status,
                "content_type": content_type,
                "request_method": response.request.method,
                "request_post_data": response.request.post_data,
                "headers": response.headers,
                "fetched_at_epoch": time.time(),
            }
            try:
                body = response.body()
                record["bytes"] = len(body)
                record["sha256"] = sha256(body)
                if len(body) <= 15_000_000 and (
                    "json" in content_type
                    or "text" in content_type
                    or "javascript" in content_type
                    or "html" in content_type
                    or url.endswith((".json", ".js"))
                ):
                    suffix = ".json" if "json" in content_type or url.endswith(".json") else ".txt"
                    path = root / "network" / safe_name(url, response_counter, suffix)
                    save_bytes(path, body, record)
                    record["path"] = str(path.relative_to(root))
            except Exception as exc:  # noqa: BLE001
                record["body_error"] = repr(exc)
            network.append(record)

        page.on("response", on_response)
        page.goto(TARGET, wait_until="domcontentloaded", timeout=180_000)
        page.wait_for_timeout(8_000)

        # Close/accept common modal and cookie controls without assuming any one implementation.
        for pattern in (
            re.compile(r"accept all", re.I),
            re.compile(r"accept cookies", re.I),
            re.compile(r"agree", re.I),
            re.compile(r"continue", re.I),
            re.compile(r"close", re.I),
        ):
            locator = page.get_by_role("button", name=pattern)
            for i in range(min(locator.count(), 5)):
                try:
                    if locator.nth(i).is_visible():
                        locator.nth(i).click(timeout=3_000)
                        page.wait_for_timeout(500)
                        break
                except Exception:  # noqa: BLE001
                    pass

        stages: list[dict] = []

        def dump(stage: str) -> dict:
            html = page.content().encode("utf-8")
            save_bytes(
                root / f"{stage}.html",
                html,
                {"url": page.url, "fetched_at_epoch": time.time()},
            )
            body_text = page.locator("body").inner_text()
            (root / f"{stage}.txt").write_text(body_text, encoding="utf-8")
            page.screenshot(path=str(root / f"{stage}.png"), full_page=True)
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
                      if (/^(href|src|alt|title|target|rel|data-|aria-|role|class|id)/.test(a.name)) {
                        attrs[a.name] = a.value;
                      }
                    }
                    return {
                      tag: el.tagName,
                      text: (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim(),
                      href: el.href || null,
                      src: el.src || null,
                      visible: visible(el),
                      rect: {x:r.x, y:r.y, width:r.width, height:r.height},
                      attrs,
                      childCount: el.children.length,
                    };
                  };
                  const anchors = [...document.querySelectorAll('a')].map(simple);
                  const images = [...document.querySelectorAll('img')].map(simple);
                  const controls = [...document.querySelectorAll('button,[role=button]')].map(simple);
                  const headings = [...document.querySelectorAll('h1,h2,h3,h4,h5,h6')].map(simple);
                  const likelyCards = [...document.querySelectorAll(
                    'article,[class*=portfolio],[class*=Portfolio],[class*=card],[class*=Card],[data-testid*=card],[data-testid*=portfolio]'
                  )].map(simple).filter(x => x.visible && (x.text || x.href || x.src));
                  const leaves = [...document.querySelectorAll('body *')]
                    .filter(el => el.children.length === 0)
                    .map(simple)
                    .filter(x => x.visible && x.text && x.text.length <= 240);
                  const resources = performance.getEntriesByType('resource').map(r => ({
                    name:r.name, initiatorType:r.initiatorType, transferSize:r.transferSize,
                    decodedBodySize:r.decodedBodySize, duration:r.duration
                  }));
                  return {
                    title: document.title,
                    url: location.href,
                    readyState: document.readyState,
                    scrollHeight: document.documentElement.scrollHeight,
                    anchors, images, controls, headings, likelyCards, leaves, resources
                  };
                }
                """
            )
            write_json(root / f"{stage}-dom.json", payload)
            return payload

        stages.append(dump("stage-00"))

        click_log = []
        for cycle in range(30):
            page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
            page.wait_for_timeout(1_500)
            clicked = False
            for text in ("Load More", "Show More", "View More", "See More", "More"):
                candidates = page.get_by_text(text, exact=True)
                for i in range(min(candidates.count(), 10)):
                    candidate = candidates.nth(i)
                    try:
                        if candidate.is_visible():
                            before = page.locator("body").inner_text()
                            candidate.scroll_into_view_if_needed()
                            candidate.click(timeout=5_000)
                            page.wait_for_timeout(2_500)
                            after = page.locator("body").inner_text()
                            click_log.append({
                                "cycle": cycle,
                                "text": text,
                                "changed": before != after,
                            })
                            clicked = True
                            break
                    except (PlaywrightTimeoutError, Exception):  # noqa: BLE001
                        continue
                if clicked:
                    break
            if clicked:
                stages.append(dump(f"stage-{len(stages):02d}"))
                continue
            previous_height = page.evaluate("document.documentElement.scrollHeight")
            page.wait_for_timeout(1_000)
            current_height = page.evaluate("document.documentElement.scrollHeight")
            if current_height == previous_height and cycle >= 3:
                break

        final = dump("final")
        write_json(root / "network-index.json", network)
        write_json(root / "click-log.json", click_log)
        browser.close()

    return {
        "network_response_count": len(network),
        "saved_network_response_count": sum(bool(item.get("path")) for item in network),
        "stage_count": len(stages),
        "click_count": len(click_log),
        "final_anchor_count": len(final["anchors"]),
        "final_image_count": len(final["images"]),
        "final_heading_count": len(final["headings"]),
        "final_likely_card_count": len(final["likelyCards"]),
        "final_scroll_height": final["scrollHeight"],
    }


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    RAW.mkdir(parents=True)

    static = [static_capture(1), static_capture(2)]
    browser = capture_browser()
    summary = {
        "target": TARGET,
        "static": static,
        "static_hashes_equal": static[0]["sha256"] == static[1]["sha256"],
        "browser": browser,
        "generated_at_epoch": time.time(),
    }
    write_json(OUT / "discovery-summary.json", summary)

    manifest = {"files": {}}
    for path in sorted(OUT.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            manifest["files"][str(path.relative_to(OUT))] = {
                "bytes": path.stat().st_size,
                "sha256": sha256(path.read_bytes()),
            }
    write_json(OUT / "manifest.json", manifest)
    print("DISCOVERY_SUMMARY_START")
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    print("DISCOVERY_SUMMARY_END")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        OUT.mkdir(parents=True, exist_ok=True)
        write_json(
            OUT / "fatal-error.json",
            {"error": repr(exc), "traceback": traceback.format_exc()},
        )
        raise
