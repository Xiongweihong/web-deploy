from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
import traceback
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

TARGET = "https://www.kleinerperkins.com/partnerships/"
OUT = Path("artifact")
RAW = OUT / "raw"
UA = "Mozilla/5.0 (compatible; kp-evidence-crawler/1.0)"

def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()

def write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

def fetch(url: str, attempts: int = 6) -> tuple[bytes, dict]:
    last = None
    for attempt in range(1, attempts + 1):
        try:
            r = requests.get(
                url,
                timeout=120,
                allow_redirects=True,
                headers={
                    "User-Agent": UA,
                    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                },
            )
            r.raise_for_status()
            return r.content, {
                "requested_url": r.request.url,
                "final_url": r.url,
                "status": r.status_code,
                "content_type": r.headers.get("content-type"),
                "date": r.headers.get("date"),
                "etag": r.headers.get("etag"),
                "last_modified": r.headers.get("last-modified"),
                "fetched_at_epoch": time.time(),
                "attempt": attempt,
                "bytes": len(r.content),
                "sha256": sha256(r.content),
            }
        except Exception as exc:
            last = exc
            if attempt < attempts:
                time.sleep(min(30, 2 ** attempt))
    raise RuntimeError(f"GET failed after {attempts}: {url}: {last!r}")

def save(path: Path, raw: bytes, meta: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    write_json(path.with_suffix(path.suffix + ".meta.json"), meta)

def ancestor_chain(node, depth: int = 7):
    out = []
    cur = node
    for _ in range(depth):
        if cur is None:
            break
        attrs = {}
        for k, v in getattr(cur, "attrs", {}).items():
            if k == "class":
                attrs[k] = v
            elif k.startswith("data-") or k in {"id", "href", "role", "aria-label", "aria-controls"}:
                attrs[k] = v
        out.append({"tag": getattr(cur, "name", None), "attrs": attrs, "text_prefix": clean(cur.get_text(" ", strip=True))[:240]})
        cur = getattr(cur, "parent", None)
    return out

def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    RAW.mkdir(parents=True)

    captures = []
    for i in (1, 2):
        raw, meta = fetch(TARGET)
        save(RAW / f"snapshot-{i:02d}" / "partnerships.html", raw, meta)
        captures.append((raw, meta))
        time.sleep(2)

    raw = captures[-1][0]
    soup = BeautifulSoup(raw, "lxml")

    headings = []
    heading_counts = Counter()
    for tag in soup.find_all(re.compile(r"^h[1-6]$")):
        text = clean(tag.get_text(" ", strip=True))
        if text:
            heading_counts[tag.name] += 1
            headings.append({
                "tag": tag.name,
                "text": text,
                "id": tag.get("id"),
                "class": tag.get("class"),
                "data": {k: v for k, v in tag.attrs.items() if k.startswith("data-")},
            })

    anchors = []
    website_anchors = []
    partnership_links = []
    external_hosts = Counter()
    source_host = urlparse(TARGET).netloc.casefold()
    for a in soup.find_all("a", href=True):
        href = clean(a.get("href"))
        text = clean(a.get_text(" ", strip=True))
        parsed = urlparse(href if "://" in href else "")
        if parsed.netloc and parsed.netloc.casefold() != source_host:
            external_hosts[parsed.netloc.casefold()] += 1
        item = {
            "text": text,
            "href": href,
            "class": a.get("class"),
            "id": a.get("id"),
            "target": a.get("target"),
            "rel": a.get("rel"),
            "data": {k: v for k, v in a.attrs.items() if k.startswith("data-")},
        }
        anchors.append(item)
        if text.casefold() == "website" or "website" in text.casefold():
            prev_heading = a.find_previous(re.compile(r"^h[1-6]$"))
            website_anchors.append({
                **item,
                "previous_heading": clean(prev_heading.get_text(" ", strip=True)) if prev_heading else None,
                "previous_heading_tag": prev_heading.name if prev_heading else None,
                "ancestor_chain": ancestor_chain(a),
            })
        if "partnership" in href.casefold():
            partnership_links.append(item)

    class_counts = Counter()
    data_attr_counts = Counter()
    interesting_nodes = []
    for node in soup.find_all(True):
        for cls in node.get("class") or []:
            class_counts[str(cls)] += 1
        for key in node.attrs:
            if key.startswith("data-"):
                data_attr_counts[key] += 1
        blob = " ".join(node.get("class") or []) + " " + str(node.get("id") or "")
        if any(token in blob.casefold() for token in ("company", "partnership", "portfolio", "archive", "modal", "card")):
            interesting_nodes.append({
                "tag": node.name,
                "id": node.get("id"),
                "class": node.get("class"),
                "data": {k: v for k, v in node.attrs.items() if k.startswith("data-")},
                "text_prefix": clean(node.get_text(" ", strip=True))[:300],
            })

    scripts = []
    for script in soup.find_all("script"):
        scripts.append({
            "src": script.get("src"),
            "type": script.get("type"),
            "id": script.get("id"),
            "text_prefix": clean(script.string or script.get_text(" ", strip=True))[:1000],
            "text_length": len(script.string or script.get_text() or ""),
        })

    pairs = []
    for item in website_anchors:
        name = item.get("previous_heading")
        href = item["href"]
        if name and href:
            pairs.append({"name": name, "website": href})

    detail_candidates = []
    for a in soup.find_all("a", href=True):
        if clean(a.get_text(" ", strip=True)).casefold() != "website":
            continue
        selected = None
        for parent in a.parents:
            if getattr(parent, "name", None) in {"article", "section", "li", "div"}:
                hs = parent.find_all(["h2", "h3", "h4"], recursive=True)
                websites = [
                    x for x in parent.find_all("a", href=True, recursive=True)
                    if clean(x.get_text(" ", strip=True)).casefold() == "website"
                ]
                if len(websites) == 1 and hs:
                    selected = parent
                    break
        if selected:
            hs = [clean(x.get_text(" ", strip=True)) for x in selected.find_all(["h2", "h3", "h4"]) if clean(x.get_text(" ", strip=True))]
            detail_candidates.append({
                "headings": hs[:10],
                "website": clean(a.get("href")),
                "container_tag": selected.name,
                "container_id": selected.get("id"),
                "container_class": selected.get("class"),
                "container_data": {k: v for k, v in selected.attrs.items() if k.startswith("data-")},
                "text_prefix": clean(selected.get_text(" ", strip=True))[:800],
            })

    write_json(OUT / "headings.json", headings)
    write_json(OUT / "anchors.json", anchors)
    write_json(OUT / "website-anchors.json", website_anchors)
    write_json(OUT / "website-pairs.json", pairs)
    write_json(OUT / "detail-candidates.json", detail_candidates)
    write_json(OUT / "partnership-links.json", partnership_links)
    write_json(OUT / "scripts.json", scripts)
    write_json(OUT / "interesting-nodes.json", interesting_nodes[:5000])
    write_json(OUT / "class-counts.json", class_counts.most_common())
    write_json(OUT / "data-attr-counts.json", data_attr_counts.most_common())

    summary = {
        "capture_count": len(captures),
        "capture_hashes": [meta["sha256"] for _, meta in captures],
        "capture_hashes_equal": captures[0][1]["sha256"] == captures[1][1]["sha256"],
        "html_bytes": len(raw),
        "title": clean(soup.title.get_text(" ", strip=True)) if soup.title else None,
        "heading_counts": dict(heading_counts),
        "heading_total": len(headings),
        "unique_heading_texts": len({x["text"] for x in headings}),
        "anchor_total": len(anchors),
        "website_anchor_count": len(website_anchors),
        "unique_website_href_count": len({x["href"] for x in website_anchors}),
        "website_pair_count": len(pairs),
        "unique_website_pair_name_count": len({x["name"].casefold() for x in pairs}),
        "detail_candidate_count": len(detail_candidates),
        "partnership_link_count": len(partnership_links),
        "unique_partnership_href_count": len({x["href"] for x in partnership_links}),
        "script_count": len(scripts),
        "top_external_hosts": external_hosts.most_common(30),
        "top_classes": class_counts.most_common(80),
        "top_data_attrs": data_attr_counts.most_common(50),
        "sample_website_pairs": pairs[:20],
        "sample_partnership_links": partnership_links[:20],
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
    except Exception as exc:
        OUT.mkdir(parents=True, exist_ok=True)
        write_json(OUT / "fatal-error.json", {"error": repr(exc), "traceback": traceback.format_exc()})
        raise
