from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
import traceback
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

TARGET = "https://www.trueventures.com/portfolio"
ORIGIN = "https://www.trueventures.com"
OUT = Path("artifact")
RAW = OUT / "raw"
UA = "Mozilla/5.0 (compatible; trueventures-evidence-crawler/1.0)"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def save_bytes(path: Path, data: bytes, meta: dict | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    payload = dict(meta or {})
    payload.update({"bytes": len(data), "sha256": sha256(data)})
    write_json(path.with_suffix(path.suffix + ".meta.json"), payload)


def fetch(url: str, *, attempts: int = 5) -> requests.Response:
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(
                url,
                timeout=120,
                allow_redirects=True,
                headers={"User-Agent": UA, "Accept": "*/*"},
            )
            response.raise_for_status()
            return response
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < attempts:
                time.sleep(min(20, 2**attempt))
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
    for position, script in enumerate(soup.find_all("script"), start=1):
        src = script.get("src")
        scripts.append(
            {
                "position": position,
                "src": urljoin(TARGET, src) if src else None,
                "type": script.get("type"),
                "id": script.get("id"),
                "text_prefix": (script.string or script.get_text(" ", strip=True))[:1000],
            }
        )
    anchors = []
    for position, anchor in enumerate(soup.find_all("a", href=True), start=1):
        href = urljoin(TARGET, str(anchor.get("href") or ""))
        anchors.append(
            {
                "position": position,
                "text": re.sub(r"\s+", " ", anchor.get_text(" ", strip=True)).strip(),
                "href": href,
                "host": urlparse(href).netloc.casefold(),
                "class": anchor.get("class"),
                "attrs": dict(anchor.attrs),
            }
        )
    write_json(root / "scripts.json", scripts)
    write_json(root / "anchors.json", anchors)

    matched_bundles = []
    for script in scripts:
        src = script.get("src")
        if not src or urlparse(src).netloc not in {"www.trueventures.com", "trueventures.com"}:
            continue
        try:
            js_response = fetch(src, attempts=3)
        except Exception as exc:  # noqa: BLE001
            matched_bundles.append({"url": src, "error": repr(exc)})
            continue
        text = js_response.text
        tokens = [token for token in ("Aristotle", "Peloton", "portfolio", "Highlights", "Exits") if token.casefold() in text.casefold()]
        if tokens:
            safe = re.sub(r"[^A-Za-z0-9._-]+", "_", urlparse(src).path.strip("/"))[-180:]
            path = root / "matched-js" / f"{safe or 'bundle'}.js"
            save_bytes(
                path,
                js_response.content,
                {
                    "url": src,
                    "status": js_response.status_code,
                    "content_type": js_response.headers.get("content-type"),
                    "matched_tokens": tokens,
                },
            )
            matched_bundles.append({"url": src, "path": str(path), "matched_tokens": tokens})
    write_json(root / "matched-bundles.json", matched_bundles)
    return {
        "sha256": sha256(response.content),
        "bytes": len(response.content),
        "script_count": len(scripts),
        "anchor_count": len(anchors),
        "external_anchor_count": sum(
            bool(row["host"]) and not row["host"].endswith("trueventures.com")
            for row in anchors
        ),
        "matched_bundle_count": len([row for row in matched_bundles if row.get("path")]),
    }


def capture_browser() -> dict:
    root = RAW / "browser"
    root.mkdir(parents=True, exist_ok=True)
    network_records: list[dict] = []
    response_counter = 0

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 1200},
            user_agent=UA,
            locale="en-US",
        )
        page = context.new_page()

        def on_response(response) -> None:
            nonlocal response_counter
            url = response.url
            content_type = response.headers.get("content-type") or ""
            if not (
                "json" in content_type.casefold()
                or any(token in url.casefold() for token in ("api", "portfolio", "graphql", "_next/data", "content"))
            ):
                return
            try:
                body = response.body()
            except Exception:  # noqa: BLE001
                return
            response_counter += 1
            suffix = ".json" if "json" in content_type.casefold() else ".bin"
            safe = re.sub(r"[^A-Za-z0-9._-]+", "_", urlparse(url).path.strip("/") or "root")[-160:]
            path = root / "network" / f"{response_counter:04d}_{safe}{suffix}"
            meta = {
                "url": url,
                "status": response.status,
                "content_type": content_type,
                "request_method": response.request.method,
                "request_post_data": response.request.post_data,
                "fetched_at_epoch": time.time(),
            }
            save_bytes(path, body, meta)
            network_records.append({**meta, "path": str(path)})

        page.on("response", on_response)
        page.goto(TARGET, wait_until="domcontentloaded", timeout=120_000)
        page.wait_for_timeout(5_000)

        def dump(stage: str) -> dict:
            html = page.content().encode("utf-8")
            save_bytes(root / f"{stage}.html", html, {"url": page.url, "fetched_at_epoch": time.time()})
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
                  const item = (el) => {
                    const r = el.getBoundingClientRect();
                    const attrs = {};
                    for (const a of el.attributes || []) {
                      if (/^(href|class|id|role|aria-|data-)/.test(a.name)) attrs[a.name] = a.value;
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
                  return {
                    title: document.title,
                    url: location.href,
                    scrollHeight: document.documentElement.scrollHeight,
                    anchors: [...document.querySelectorAll('a')].map(item),
                    buttons: [...document.querySelectorAll('button,[role=button]')].map(item),
                    articles: [...document.querySelectorAll('article,li,[class*=card],[class*=portfolio],[data-company]')].map(item),
                    headings: [...document.querySelectorAll('h1,h2,h3,h4,h5,h6')].map(item),
                  };
                }
                """
            )
            write_json(root / f"{stage}-dom.json", payload)
            return payload

        initial = dump("initial")

        all_clicked = False
        all_controls = page.get_by_text("All", exact=True)
        for index in range(all_controls.count()):
            control = all_controls.nth(index)
            try:
                if not control.is_visible():
                    continue
                control.scroll_into_view_if_needed()
                control.click(timeout=10_000)
                all_clicked = True
                page.wait_for_timeout(3_000)
                break
            except PlaywrightTimeoutError:
                try:
                    control.click(timeout=10_000, force=True)
                    all_clicked = True
                    page.wait_for_timeout(3_000)
                    break
                except Exception:  # noqa: BLE001
                    continue
            except Exception:  # noqa: BLE001
                continue

        after_all = dump("after-all")

        heights = []
        stable_rounds = 0
        for iteration in range(1, 31):
            previous = page.evaluate("document.documentElement.scrollHeight")
            page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
            page.wait_for_timeout(1_500)
            current = page.evaluate("document.documentElement.scrollHeight")
            heights.append({"iteration": iteration, "previous": previous, "current": current})
            if current == previous:
                stable_rounds += 1
            else:
                stable_rounds = 0
            if stable_rounds >= 3:
                break
        write_json(root / "scroll-heights.json", heights)
        final = dump("final")
        write_json(root / "network-index.json", network_records)
        browser.close()

    return {
        "all_clicked": all_clicked,
        "initial_anchor_count": len(initial["anchors"]),
        "after_all_anchor_count": len(after_all["anchors"]),
        "final_anchor_count": len(final["anchors"]),
        "initial_article_candidate_count": len(initial["articles"]),
        "after_all_article_candidate_count": len(after_all["articles"]),
        "final_article_candidate_count": len(final["articles"]),
        "initial_scroll_height": initial["scrollHeight"],
        "final_scroll_height": final["scrollHeight"],
        "network_response_count": len(network_records),
        "scroll_iterations": len(heights),
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
        write_json(OUT / "fatal-error.json", {"error": repr(exc), "traceback": traceback.format_exc()})
        raise
