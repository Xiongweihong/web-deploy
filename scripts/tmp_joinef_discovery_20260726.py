from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
import traceback
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

TARGET = "https://www.joinef.com/portfolio/"
ORIGIN = "https://www.joinef.com"
OUT = Path("artifact")
RAW = OUT / "raw"
UA = "Mozilla/5.0 (compatible; joinef-evidence-crawler/1.0)"


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


def get(url: str, *, params: dict | None = None, attempts: int = 5) -> requests.Response:
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(
                url,
                params=params,
                timeout=90,
                allow_redirects=True,
                headers={"User-Agent": UA, "Accept": "*/*"},
            )
            response.raise_for_status()
            return response
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < attempts:
                time.sleep(min(20, 2**attempt))
    raise RuntimeError(f"GET failed: {url}: {last}")


def capture_static(index: int) -> dict:
    response = get(TARGET)
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
                "src": urljoin(response.url, script.get("src")) if script.get("src") else None,
                "type": script.get("type"),
                "id": script.get("id"),
                "text_prefix": (script.string or script.get_text(" ", strip=True))[:1000],
            }
        )
    links = [
        {
            "text": a.get_text(" ", strip=True),
            "href": urljoin(response.url, a.get("href")) if a.get("href") else None,
            "class": a.get("class"),
            "rel": a.get("rel"),
            "target": a.get("target"),
            "data": {k: v for k, v in a.attrs.items() if str(k).startswith("data-")},
        }
        for a in soup.find_all("a")
    ]
    headings = [
        {"tag": h.name, "text": h.get_text(" ", strip=True), "class": h.get("class")}
        for h in soup.find_all(re.compile(r"^h[1-6]$"))
    ]
    load_more = [
        {
            "tag": node.name,
            "text": node.get_text(" ", strip=True),
            "href": node.get("href"),
            "class": node.get("class"),
            "attrs": dict(node.attrs),
            "parent_html": str(node.parent)[:5000] if node.parent else None,
        }
        for node in soup.find_all(string=re.compile(r"^\s*Load more\s*$", re.I))
        if getattr(node, "parent", None)
        for node in [node.parent]
    ]
    save_json(root / "scripts.json", scripts)
    save_json(root / "links.json", links)
    save_json(root / "headings.json", headings)
    save_json(root / "load-more-nodes.json", load_more)
    return {
        "sha256": sha256(response.content),
        "bytes": len(response.content),
        "script_count": len(scripts),
        "link_count": len(links),
        "heading_count": len(headings),
        "load_more_count": len(load_more),
    }


def capture_rest() -> dict:
    result: dict = {}
    for name, url in {
        "wp-json-root": f"{ORIGIN}/wp-json/",
        "wp-v2-types": f"{ORIGIN}/wp-json/wp/v2/types",
        "wp-v2-search": f"{ORIGIN}/wp-json/wp/v2/search?per_page=100",
    }.items():
        try:
            response = get(url)
            save_bytes(
                RAW / "rest" / f"{name}.json",
                response.content,
                {
                    "url": response.url,
                    "status": response.status_code,
                    "content_type": response.headers.get("content-type"),
                },
            )
            try:
                result[name] = response.json()
            except Exception:
                result[name] = {"non_json": True, "prefix": response.text[:500]}
        except Exception as exc:  # noqa: BLE001
            result[name] = {"error": repr(exc)}

    types = result.get("wp-v2-types")
    if isinstance(types, dict):
        for type_name, spec in types.items():
            if type_name in {"post", "page", "attachment", "nav_menu_item", "wp_block", "wp_template", "wp_template_part", "wp_navigation", "wp_font_family", "wp_font_face", "user_request"}:
                continue
            rest_base = (spec or {}).get("rest_base") or type_name
            for page in range(1, 11):
                url = f"{ORIGIN}/wp-json/wp/v2/{rest_base}"
                try:
                    response = get(url, params={"per_page": 100, "page": page, "context": "view"})
                except Exception as exc:  # noqa: BLE001
                    save_json(RAW / "rest" / f"type-{type_name}-page-{page:02d}-error.json", {"error": repr(exc), "url": url})
                    break
                path = RAW / "rest" / f"type-{type_name}-page-{page:02d}.json"
                save_bytes(
                    path,
                    response.content,
                    {
                        "url": response.url,
                        "status": response.status_code,
                        "x_wp_total": response.headers.get("x-wp-total"),
                        "x_wp_totalpages": response.headers.get("x-wp-totalpages"),
                    },
                )
                try:
                    payload = response.json()
                except Exception:
                    break
                if not isinstance(payload, list) or not payload:
                    break
                total_pages = int(response.headers.get("x-wp-totalpages") or 1)
                if page >= total_pages:
                    break
    return result


def capture_scripts(static_summary: dict) -> list[dict]:
    scripts_path = RAW / "static-01" / "scripts.json"
    scripts = json.loads(scripts_path.read_text(encoding="utf-8"))
    records: list[dict] = []
    for index, item in enumerate(scripts, start=1):
        src = item.get("src")
        if not src or not src.startswith("http"):
            continue
        try:
            response = get(src)
            suffix = ".js" if "javascript" in (response.headers.get("content-type") or "") or src.endswith(".js") else ".bin"
            path = RAW / "scripts" / f"{index:03d}{suffix}"
            save_bytes(
                path,
                response.content,
                {"url": response.url, "status": response.status_code, "content_type": response.headers.get("content-type")},
            )
            text = response.text if suffix == ".js" else ""
            hits = sorted(
                set(
                    re.findall(
                        r"[^\"'\s]{0,80}(?:admin-ajax|wp-json|pagenum|load_more|load-more|portfolio)[^\"'\s]{0,140}",
                        text,
                        re.I,
                    )
                )
            )[:300]
            records.append({"src": src, "path": str(path), "bytes": len(response.content), "hits": hits})
        except Exception as exc:  # noqa: BLE001
            records.append({"src": src, "error": repr(exc)})
    save_json(RAW / "scripts" / "index.json", records)
    return records


def capture_browser() -> dict:
    root = RAW / "browser"
    root.mkdir(parents=True, exist_ok=True)
    network: list[dict] = []
    response_number = 0

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(user_agent=UA, viewport={"width": 1440, "height": 1100}, locale="en-US")
        page = context.new_page()

        def on_response(response) -> None:
            nonlocal response_number
            url = response.url
            if not (
                url.startswith(ORIGIN)
                or any(token in url for token in ("wp-json", "admin-ajax", "portfolio", "pagenum"))
            ):
                return
            response_number += 1
            entry = {
                "number": response_number,
                "url": url,
                "status": response.status,
                "content_type": response.headers.get("content-type"),
                "method": response.request.method,
                "post_data": response.request.post_data,
                "resource_type": response.request.resource_type,
                "fetched_at_epoch": time.time(),
            }
            try:
                body = response.body()
                suffix = ".json" if "json" in (entry["content_type"] or "") else ".bin"
                safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", urlparse(url).path.strip("/") or "root")[:100]
                path = root / "network" / f"{response_number:04d}_{safe}{suffix}"
                save_bytes(path, body, entry)
                entry["path"] = str(path)
                entry["bytes"] = len(body)
            except Exception as exc:  # noqa: BLE001
                entry["body_error"] = repr(exc)
            network.append(entry)

        page.on("response", on_response)
        page.goto(TARGET, wait_until="domcontentloaded", timeout=120_000)
        page.wait_for_timeout(5000)

        # Best-effort cookie dismissal.
        for pattern in (r"Accept all", r"Accept", r"Allow all"):
            locator = page.get_by_role("button", name=re.compile(pattern, re.I))
            try:
                if locator.count() and locator.first.is_visible():
                    locator.first.click(timeout=3000)
                    page.wait_for_timeout(800)
                    break
            except Exception:
                pass

        def dump(stage: str) -> dict:
            html = page.content().encode("utf-8")
            save_bytes(root / f"{stage}.html", html, {"url": page.url, "fetched_at_epoch": time.time()})
            (root / f"{stage}.txt").write_text(page.locator("body").inner_text(), encoding="utf-8")
            page.screenshot(path=str(root / f"{stage}.png"), full_page=True)
            payload = page.evaluate(
                """
                () => {
                  const visible = el => {
                    const s = getComputedStyle(el), r = el.getBoundingClientRect();
                    return s.display !== 'none' && s.visibility !== 'hidden' && Number(s.opacity || 1) > 0 && r.width > 0 && r.height > 0;
                  };
                  const simple = el => {
                    const r = el.getBoundingClientRect();
                    const attrs = {};
                    for (const a of el.attributes || []) if (/^(href|class|id|role|aria-|data-)/.test(a.name)) attrs[a.name] = a.value;
                    return {tag:el.tagName,text:(el.innerText||el.textContent||'').replace(/\s+/g,' ').trim(),href:el.href||null,visible:visible(el),rect:{x:r.x,y:r.y+scrollY,width:r.width,height:r.height},attrs,childCount:el.children.length};
                  };
                  const all = [...document.querySelectorAll('body *')];
                  const headings = [...document.querySelectorAll('h1,h2,h3,h4,h5,h6')].map(simple);
                  const anchors = [...document.querySelectorAll('a')].map(simple);
                  const controls = [...document.querySelectorAll('button,[role=button],a')].map(simple).filter(x => /load more/i.test(x.text));
                  const exact = name => all.filter(el => (el.textContent||'').trim()===name).map(el => ({...simple(el),outerHTML:el.outerHTML.slice(0,3000),parentHTML:el.parentElement?.outerHTML.slice(0,10000)}));
                  const classCounts = {};
                  for (const el of all) {
                    const cls = typeof el.className === 'string' ? el.className : '';
                    if (/company|portfolio|card|modal/i.test(cls)) classCounts[cls] = (classCounts[cls] || 0) + 1;
                  }
                  return {
                    title:document.title,url:location.href,scrollHeight:document.documentElement.scrollHeight,
                    headings,anchors,controls,classCounts,
                    samples:{Automata:exact('Automata'),Tractable:exact('Tractable'),Tuza:exact('Tuza')}
                  };
                }
                """
            )
            save_json(root / f"{stage}-dom.json", payload)
            return payload

        stages: list[dict] = []
        stages.append(dump("stage-00"))
        clicks = 0
        no_growth = 0
        previous_body = page.locator("body").inner_text()
        previous_heading_count = len(stages[-1]["headings"])

        for _ in range(60):
            candidates = page.locator("a,button,[role=button]").filter(has_text=re.compile(r"^\s*Load more\s*$", re.I))
            chosen = None
            for index in range(candidates.count()):
                candidate = candidates.nth(index)
                try:
                    if candidate.is_visible() and candidate.is_enabled():
                        chosen = candidate
                        break
                except Exception:
                    continue
            if chosen is None:
                break
            chosen.scroll_into_view_if_needed()
            try:
                chosen.click(timeout=10_000)
            except PlaywrightTimeoutError:
                chosen.click(force=True, timeout=10_000)
            page.wait_for_timeout(2500)
            clicks += 1
            stage = dump(f"stage-{clicks:02d}")
            stages.append(stage)
            body = page.locator("body").inner_text()
            heading_count = len(stage["headings"])
            if body == previous_body and heading_count == previous_heading_count:
                no_growth += 1
            else:
                no_growth = 0
            previous_body = body
            previous_heading_count = heading_count
            if no_growth >= 2:
                break

        # Dump candidate card structures after exhaustion.
        cards = page.evaluate(
            """
            () => {
              const visible = el => {
                const s=getComputedStyle(el),r=el.getBoundingClientRect();
                return s.display!=='none'&&s.visibility!=='hidden'&&Number(s.opacity||1)>0&&r.width>0&&r.height>0;
              };
              const allHeadings=[...document.querySelectorAll('h2,h3,h4,h5,h6')].filter(visible);
              const start=allHeadings.find(h=>/^all companies$/i.test((h.textContent||'').trim()));
              const end=[...document.querySelectorAll('body *')].filter(visible).find(el=>/^There are no companies matching these filters$/i.test((el.textContent||'').trim()));
              const sy=start ? start.getBoundingClientRect().top+scrollY : 0;
              const ey=end ? end.getBoundingClientRect().top+scrollY : Infinity;
              const inRange=allHeadings.filter(h=>{const y=h.getBoundingClientRect().top+scrollY;return y>sy&&y<ey});
              return inRange.map(h=>{
                let cur=h;
                const ancestors=[];
                for(let i=0;i<6&&cur;i++,cur=cur.parentElement){
                  ancestors.push({tag:cur.tagName,className:typeof cur.className==='string'?cur.className:'',id:cur.id||'',text:(cur.innerText||'').replace(/\s+/g,' ').trim().slice(0,1000),links:[...cur.querySelectorAll(':scope a')].map(a=>({text:(a.innerText||a.textContent||'').trim(),href:a.href,attrs:Object.fromEntries([...a.attributes].map(x=>[x.name,x.value]))})),html:cur.outerHTML.slice(0,20000)});
                }
                return {heading:(h.textContent||'').trim(),tag:h.tagName,className:typeof h.className==='string'?h.className:'',y:h.getBoundingClientRect().top+scrollY,ancestors};
              });
            }
            """
        )
        save_json(root / "final-heading-card-structures.json", cards)
        save_json(root / "network-index.json", network)
        browser.close()

    return {
        "click_count": clicks,
        "stage_count": len(stages),
        "network_response_count": len(network),
        "final_heading_count": len(stages[-1]["headings"]),
        "final_anchor_count": len(stages[-1]["anchors"]),
        "final_scroll_height": stages[-1]["scrollHeight"],
        "visible_load_more_controls_final": sum(1 for item in stages[-1]["controls"] if item["visible"]),
    }


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    RAW.mkdir(parents=True)

    static = [capture_static(1), capture_static(2)]
    rest = capture_rest()
    scripts = capture_scripts(static[0])
    browser = capture_browser()

    summary = {
        "target": TARGET,
        "static": static,
        "static_hashes_equal": static[0]["sha256"] == static[1]["sha256"],
        "rest_keys": sorted(rest),
        "downloaded_script_count": len(scripts),
        "browser": browser,
        "generated_at_epoch": time.time(),
    }
    save_json(OUT / "discovery-summary.json", summary)
    manifest = {"files": {}}
    for path in sorted(OUT.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            manifest["files"][str(path.relative_to(OUT))] = {"bytes": path.stat().st_size, "sha256": sha256(path.read_bytes())}
    save_json(OUT / "manifest.json", manifest)
    print("DISCOVERY_SUMMARY_START")
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    print("DISCOVERY_SUMMARY_END")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        OUT.mkdir(parents=True, exist_ok=True)
        save_json(OUT / "fatal-error.json", {"error": repr(exc), "traceback": traceback.format_exc()})
        raise
