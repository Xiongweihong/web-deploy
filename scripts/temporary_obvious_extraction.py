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
from playwright.sync_api import sync_playwright

BASE = "https://obvious.com"
URL = BASE + "/portfolio/"
OUT = Path("artifact")
RAW = OUT / "raw"
NETWORK = RAW / "network"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def save_bytes(path: Path, data: bytes, meta: dict | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    metadata = {"bytes": len(data), "sha256": sha256(data)}
    if meta:
        metadata.update(meta)
    path.with_suffix(path.suffix + ".meta.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8"
    )


def request_and_save(session: requests.Session, url: str, path: Path) -> requests.Response:
    response = session.get(url, timeout=90, allow_redirects=True)
    save_bytes(
        path,
        response.content,
        {
            "requested_url": url,
            "final_url": response.url,
            "status": response.status_code,
            "content_type": response.headers.get("content-type"),
            "date": response.headers.get("date"),
            "etag": response.headers.get("etag"),
            "last_modified": response.headers.get("last-modified"),
            "x_wp_total": response.headers.get("x-wp-total"),
            "x_wp_totalpages": response.headers.get("x-wp-totalpages"),
            "fetched_at_epoch": time.time(),
        },
    )
    return response


def clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    NETWORK.mkdir(parents=True)

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (compatible; obvious-evidence-crawler/1.0)",
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.8",
        }
    )

    page_response = request_and_save(session, URL, RAW / "portfolio-initial.html")
    page_response.raise_for_status()
    soup = BeautifulSoup(page_response.content, "lxml")

    initial_report = {
        "status": page_response.status_code,
        "url": page_response.url,
        "title": clean(soup.title.get_text(" ", strip=True) if soup.title else ""),
        "html_bytes": len(page_response.content),
        "h2": [clean(x.get_text(" ", strip=True)) for x in soup.find_all("h2")],
        "h3": [clean(x.get_text(" ", strip=True)) for x in soup.find_all("h3")],
        "founded_occurrences": len(re.findall(r"Founded:\s*\d{4}", page_response.text, re.I)),
        "load_more_elements": [],
        "script_sources": [urljoin(BASE, x.get("src")) for x in soup.find_all("script", src=True)],
    }
    for element in soup.find_all(["button", "a"]):
        text = clean(element.get_text(" ", strip=True))
        if "load more" in text.casefold() or any("load" in str(k).casefold() for k in element.attrs):
            initial_report["load_more_elements"].append(
                {"tag": element.name, "text": text, "attrs": element.attrs}
            )
    (OUT / "initial-report.json").write_text(
        json.dumps(initial_report, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )

    # WordPress REST discovery.
    probe_urls = [
        BASE + "/wp-json/",
        BASE + "/wp-json/wp/v2/types",
        BASE + "/wp-json/wp/v2/search?search=Aifleet&per_page=100",
        BASE + "/wp-json/wp/v2/portfolio?per_page=100",
        BASE + "/wp-json/wp/v2/portfolios?per_page=100",
        BASE + "/wp-json/wp/v2/company?per_page=100",
        BASE + "/wp-json/wp/v2/companies?per_page=100",
        BASE + "/wp-json/wp/v2/portfolio_company?per_page=100",
        BASE + "/wp-json/wp/v2/portfolio-companies?per_page=100",
    ]
    probes = []
    for index, probe_url in enumerate(probe_urls):
        try:
            response = request_and_save(session, probe_url, RAW / "probes" / f"probe-{index:03d}.bin")
            row = {
                "url": probe_url,
                "status": response.status_code,
                "final_url": response.url,
                "content_type": response.headers.get("content-type"),
                "bytes": len(response.content),
                "x_wp_total": response.headers.get("x-wp-total"),
                "x_wp_totalpages": response.headers.get("x-wp-totalpages"),
            }
            try:
                payload = response.json()
                row["json_type"] = type(payload).__name__
                if isinstance(payload, dict):
                    row["json_keys"] = sorted(payload.keys())[:200]
                    routes = payload.get("routes") or {}
                    if isinstance(routes, dict):
                        row["candidate_routes"] = [
                            route
                            for route in sorted(routes)
                            if re.search(r"portfolio|company|load|ajax", route, re.I)
                        ][:500]
                    if probe_url.endswith("/types"):
                        row["types"] = payload
                elif isinstance(payload, list):
                    row["json_length"] = len(payload)
                    row["json_sample"] = payload[:3]
                (RAW / "probes" / f"probe-{index:03d}.json").write_text(
                    json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
                )
            except Exception:
                row["text_sample"] = response.text[:1000]
            probes.append(row)
        except Exception as exc:
            probes.append({"url": probe_url, "error": repr(exc)})
    (OUT / "wp-probes.json").write_text(
        json.dumps(probes, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )

    network_index: list[dict] = []
    load_steps: list[dict] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 1000},
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/140 Safari/537.36",
        )
        page = context.new_page()

        def capture_response(response) -> None:
            try:
                content_type = (response.headers.get("content-type") or "").casefold()
                url_lower = response.url.casefold()
                interesting = (
                    "json" in content_type
                    or "wp-json" in url_lower
                    or "admin-ajax" in url_lower
                    or "portfolio" in url_lower
                    or "ajax" in url_lower
                    or "load" in url_lower
                )
                if not interesting:
                    return
                body = response.body()
                if len(body) > 20_000_000:
                    return
                index = len(network_index)
                suffix = ".json" if "json" in content_type else ".bin"
                path = NETWORK / f"response-{index:04d}{suffix}"
                save_bytes(
                    path,
                    body,
                    {
                        "url": response.url,
                        "status": response.status,
                        "content_type": content_type,
                        "request_method": response.request.method,
                        "request_post_data": response.request.post_data,
                    },
                )
                network_index.append(
                    {
                        "index": index,
                        "url": response.url,
                        "status": response.status,
                        "content_type": content_type,
                        "bytes": len(body),
                        "sha256": sha256(body),
                        "path": str(path),
                        "request_method": response.request.method,
                        "request_post_data": response.request.post_data,
                    }
                )
            except Exception as exc:
                network_index.append({"url": getattr(response, "url", ""), "capture_error": repr(exc)})

        page.on("response", capture_response)
        page.goto(URL, wait_until="domcontentloaded", timeout=120_000)
        page.wait_for_timeout(3000)
        save_bytes(RAW / "browser-initial.html", page.content().encode("utf-8"), {"url": page.url})

        def founded_count() -> int:
            return page.evaluate(
                """() => (document.body.innerText.match(/Founded:\\s*\\d{4}/gi) || []).length"""
            )

        stagnant = 0
        for step in range(50):
            before = founded_count()
            locator = page.get_by_text("Load More", exact=True)
            visible = False
            target = None
            try:
                for i in range(locator.count()):
                    candidate = locator.nth(i)
                    if candidate.is_visible():
                        target = candidate
                        visible = True
                        break
            except Exception:
                visible = False
            if not visible or target is None:
                load_steps.append({"step": step, "before": before, "terminal": "load_more_not_visible"})
                break
            target.scroll_into_view_if_needed()
            page.wait_for_timeout(300)
            try:
                target.click(timeout=20_000)
            except Exception:
                target.click(force=True, timeout=20_000)
            increased = False
            for _ in range(40):
                page.wait_for_timeout(250)
                if founded_count() > before:
                    increased = True
                    break
            after = founded_count()
            load_steps.append({"step": step, "before": before, "after": after, "increased": increased})
            if after <= before:
                stagnant += 1
            else:
                stagnant = 0
            if stagnant >= 2:
                load_steps.append({"step": step, "terminal": "two_stagnant_clicks"})
                break
        page.wait_for_timeout(1000)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1000)

        final_html = page.content().encode("utf-8")
        save_bytes(RAW / "browser-final.html", final_html, {"url": page.url})
        page.screenshot(path=str(RAW / "browser-final.png"), full_page=True)

        dom = page.evaluate(
            """() => {
              const clean = s => (s || '').replace(/\\s+/g, ' ').trim();
              const leaves = [...document.querySelectorAll('body *')].filter(el =>
                el.children.length === 0 && /Founded:\\s*\\d{4}/i.test(el.textContent || '')
              );
              const cards = [];
              const seen = new Set();
              for (const leaf of leaves) {
                let node = leaf;
                let chosen = null;
                for (let i = 0; i < 10 && node; i++, node = node.parentElement) {
                  const text = clean(node.innerText || node.textContent || '');
                  const founded = (text.match(/Founded:\\s*\\d{4}/gi) || []).length;
                  const pillars = (text.match(/Pillar:/gi) || []).length;
                  if (founded === 1 && pillars >= 1 && text.length < 5000) {
                    chosen = node;
                  }
                  if (founded > 1) break;
                }
                if (!chosen) continue;
                const key = clean(chosen.innerText || chosen.textContent || '');
                if (seen.has(key)) continue;
                seen.add(key);
                cards.push({
                  tag: chosen.tagName,
                  className: chosen.className,
                  id: chosen.id,
                  text: key,
                  headings: [...chosen.querySelectorAll('h1,h2,h3,h4,h5,h6,[class*="title" i]')]
                    .map(el => clean(el.innerText || el.textContent || '')).filter(Boolean),
                  anchors: [...chosen.querySelectorAll('a[href]')].map(a => ({
                    text: clean(a.innerText || a.textContent || ''),
                    href: a.href,
                    className: a.className,
                    ariaLabel: a.getAttribute('aria-label')
                  })),
                  images: [...chosen.querySelectorAll('img')].map(img => ({
                    alt: img.alt, src: img.currentSrc || img.src, className: img.className
                  })),
                  dataset: {...chosen.dataset},
                  html: chosen.outerHTML.slice(0, 30000)
                });
              }
              return {
                title: document.title,
                url: location.href,
                bodyFoundedCount: (document.body.innerText.match(/Founded:\\s*\\d{4}/gi) || []).length,
                bodyPillarCount: (document.body.innerText.match(/Pillar:/gi) || []).length,
                cards,
                allHeadings: [...document.querySelectorAll('h1,h2,h3,h4,h5,h6')]
                  .map(el => ({tag: el.tagName, text: clean(el.innerText || el.textContent || ''), className: el.className})),
                loadMore: [...document.querySelectorAll('button,a')].filter(el => /load more/i.test(clean(el.innerText || el.textContent || '')))
                  .map(el => ({tag:el.tagName,text:clean(el.innerText || el.textContent || ''),className:el.className,disabled:el.disabled,hidden:el.hidden,display:getComputedStyle(el).display}))
              };
            }"""
        )
        (OUT / "browser-dom.json").write_text(
            json.dumps(dom, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
        )
        browser.close()

    (OUT / "load-steps.json").write_text(
        json.dumps(load_steps, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (OUT / "network-index.json").write_text(
        json.dumps(network_index, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    summary = {
        "initial_founded_occurrences": initial_report["founded_occurrences"],
        "final_founded_occurrences": dom["bodyFoundedCount"],
        "dom_card_count": len(dom["cards"]),
        "load_steps": load_steps,
        "network_response_count": len(network_index),
        "wp_probes": [
            {k: v for k, v in row.items() if k not in {"types", "json_sample", "text_sample"}}
            for row in probes
        ],
    }
    (OUT / "discovery-summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
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
    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    print("DISCOVERY_SUMMARY_END")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        OUT.mkdir(parents=True, exist_ok=True)
        error = {"status": "FATAL", "error": repr(exc), "traceback": traceback.format_exc()}
        (OUT / "fatal-error.json").write_text(
            json.dumps(error, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(error["traceback"])
