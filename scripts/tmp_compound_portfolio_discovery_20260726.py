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
from playwright.sync_api import sync_playwright

TARGET = "https://www.compound.vc/portfolio"
OUT = Path("artifact")
RAW = OUT / "raw"
UA = "Mozilla/5.0 (compatible; compound-portfolio-evidence-crawler/1.0)"
SECTION_NAMES = ["Automation", "Healthcare & Biology", "Crypto", "Other"]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def save_bytes(path: Path, data: bytes, meta: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    write_json(path.with_suffix(path.suffix + ".meta.json"), {**meta, "bytes": len(data), "sha256": sha256(data)})


def fetch(index: int) -> dict:
    response = requests.get(
        TARGET,
        timeout=90,
        allow_redirects=True,
        headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"},
    )
    response.raise_for_status()
    path = RAW / f"static-{index:02d}" / "portfolio.html"
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
            "last_modified": response.headers.get("last-modified"),
            "fetched_at_epoch": time.time(),
        },
    )
    soup = BeautifulSoup(response.content, "lxml")
    anchors = [
        {
            "text": re.sub(r"\\s+", " ", a.get_text(" ", strip=True)).strip(),
            "href": a.get("href"),
            "class": a.get("class"),
            "target": a.get("target"),
            "rel": a.get("rel"),
        }
        for a in soup.find_all("a")
    ]
    headings = [
        {
            "tag": h.name,
            "text": re.sub(r"\\s+", " ", h.get_text(" ", strip=True)).strip(),
            "class": h.get("class"),
        }
        for h in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])
    ]
    write_json(path.parent / "anchors.json", anchors)
    write_json(path.parent / "headings.json", headings)
    return {
        "sha256": sha256(response.content),
        "bytes": len(response.content),
        "anchor_count": len(anchors),
        "heading_count": len(headings),
    }


def browser_capture() -> dict:
    root = RAW / "browser"
    root.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=UA, viewport={"width": 1440, "height": 1100}, locale="en-US")
        page = context.new_page()
        page.goto(TARGET, wait_until="networkidle", timeout=120_000)
        page.wait_for_timeout(3000)
        page.screenshot(path=str(root / "full-page.png"), full_page=True)
        html = page.content().encode("utf-8")
        save_bytes(root / "rendered.html", html, {"url": page.url, "fetched_at_epoch": time.time()})
        payload = page.evaluate(
            """
            (sectionNames) => {
              const visible = (el) => {
                const s = getComputedStyle(el);
                const r = el.getBoundingClientRect();
                return s.display !== 'none' && s.visibility !== 'hidden' &&
                       Number(s.opacity || 1) > 0 && r.width > 0 && r.height > 0;
              };
              const simple = (el) => {
                const r = el.getBoundingClientRect();
                return {
                  tag: el.tagName,
                  text: (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim(),
                  href: el.href || null,
                  className: typeof el.className === 'string' ? el.className : '',
                  id: el.id || '',
                  visible: visible(el),
                  rect: {x:r.x, y:r.y + scrollY, width:r.width, height:r.height},
                  childCount: el.children.length,
                };
              };
              const all = [...document.querySelectorAll('body *')].map(simple);
              const leaves = all.filter(x => x.visible && x.childCount === 0 && x.text && x.text.length <= 240);
              const anchors = [...document.querySelectorAll('a')].map(simple).filter(x => x.visible);
              const headings = [...document.querySelectorAll('h1,h2,h3,h4,h5,h6')].map(simple).filter(x => x.visible);
              const sectionMarkers = {};
              for (const name of sectionNames) {
                sectionMarkers[name] = all.filter(x => x.visible && x.text === name);
              }
              return {
                title: document.title,
                url: location.href,
                scrollHeight: document.documentElement.scrollHeight,
                leaves,
                anchors,
                headings,
                sectionMarkers,
              };
            }
            """,
            SECTION_NAMES,
        )
        write_json(root / "dom.json", payload)
        browser.close()
    return {
        "visible_anchor_count": len(payload["anchors"]),
        "visible_leaf_count": len(payload["leaves"]),
        "visible_heading_count": len(payload["headings"]),
        "scroll_height": payload["scrollHeight"],
    }


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    RAW.mkdir(parents=True)
    static = [fetch(1), fetch(2)]
    browser = browser_capture()
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
            manifest["files"][str(path.relative_to(OUT))] = {"bytes": path.stat().st_size, "sha256": sha256(path.read_bytes())}
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
