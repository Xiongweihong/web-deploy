from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
import traceback
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

TARGET = "https://www.trueventures.com/portfolio"
OUT = Path("artifact-exits")
UA = "Mozilla/5.0 (compatible; trueventures-evidence-crawler/1.1)"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def save_bytes(path: Path, data: bytes, meta: dict | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    payload = dict(meta or {})
    payload.update({"bytes": len(data), "sha256": sha256(data)})
    write_json(path.with_suffix(path.suffix + ".meta.json"), payload)


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    network: list[dict] = []
    response_counter = 0

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1200}, user_agent=UA, locale="en-US")

        def on_response(response) -> None:
            nonlocal response_counter
            content_type = response.headers.get("content-type") or ""
            if not ("text/x-component" in content_type or "json" in content_type.casefold()):
                return
            try:
                body = response.body()
            except Exception:
                return
            response_counter += 1
            suffix = ".json" if "json" in content_type.casefold() else ".bin"
            safe = re.sub(r"[^A-Za-z0-9._-]+", "_", urlparse(response.url).path.strip("/") or "root")[-120:]
            path = OUT / "network" / f"{response_counter:04d}_{safe}{suffix}"
            meta = {
                "url": response.url,
                "status": response.status,
                "content_type": content_type,
                "request_method": response.request.method,
                "request_post_data": response.request.post_data,
                "fetched_at_epoch": time.time(),
            }
            save_bytes(path, body, meta)
            network.append({**meta, "path": str(path)})

        page.on("response", on_response)
        page.goto(TARGET, wait_until="domcontentloaded", timeout=120_000)
        page.wait_for_timeout(4000)
        exits = page.get_by_text("Exits", exact=True)
        clicked = False
        for index in range(exits.count()):
            control = exits.nth(index)
            if not control.is_visible():
                continue
            control.click(timeout=10_000)
            clicked = True
            break
        if not clicked:
            raise RuntimeError("Visible Exits control not found")
        page.wait_for_timeout(3000)

        stable = 0
        heights = []
        for iteration in range(1, 20):
            before = page.evaluate("document.documentElement.scrollHeight")
            page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
            page.wait_for_timeout(1200)
            after = page.evaluate("document.documentElement.scrollHeight")
            heights.append({"iteration": iteration, "before": before, "after": after})
            stable = stable + 1 if before == after else 0
            if stable >= 3:
                break

        html = page.content().encode("utf-8")
        save_bytes(OUT / "exits.html", html, {"url": page.url, "fetched_at_epoch": time.time()})
        page.screenshot(path=str(OUT / "exits.png"), full_page=True)
        payload = page.evaluate(
            """
            () => {
              const rows = [...document.querySelectorAll('a,div')]
                .filter(el => {
                  const c = new Set(el.classList || []);
                  return c.has('border-b') && c.has('py-6') && c.has('md:flex-row');
                })
                .map((el, index) => ({
                  position: index + 1,
                  tag: el.tagName,
                  text: (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim(),
                  href: el.href || '',
                  ariaLabel: el.getAttribute('aria-label') || '',
                  classes: [...el.classList],
                }));
              return {
                title: document.title,
                url: location.href,
                rows,
                bodyText: document.body.innerText,
                scrollHeight: document.documentElement.scrollHeight,
              };
            }
            """
        )
        write_json(OUT / "exits-dom.json", payload)
        write_json(OUT / "scroll-heights.json", heights)
        write_json(OUT / "network-index.json", network)
        browser.close()

    summary = {
        "clicked": clicked,
        "row_count": len(payload["rows"]),
        "linked_count": sum(bool(row["href"]) for row in payload["rows"]),
        "unlinked_count": sum(not row["href"] for row in payload["rows"]),
        "scroll_iterations": len(heights),
        "final_scroll_height": payload["scrollHeight"],
    }
    write_json(OUT / "summary.json", summary)
    manifest = {"files": {}}
    for path in sorted(OUT.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            manifest["files"][str(path.relative_to(OUT))] = {"bytes": path.stat().st_size, "sha256": sha256(path.read_bytes())}
    write_json(OUT / "manifest.json", manifest)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        OUT.mkdir(parents=True, exist_ok=True)
        write_json(OUT / "fatal-error.json", {"error": repr(exc), "traceback": traceback.format_exc()})
        raise
