from __future__ import annotations

# Temporary public-source discovery script for the Sequoia company directory.
import collections
import hashlib
import json
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

TARGET = "https://sequoiacap.com/our-companies/"
OUT = Path("artifact")
UA = "Mozilla/5.0 (compatible; SequoiaPortfolioAudit/1.0)"


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def dump_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def fetch(url: str, *, params=None) -> requests.Response:
    r = requests.get(url, params=params, headers={"User-Agent": UA, "Accept": "text/html,application/json;q=0.9,*/*;q=0.8"}, timeout=120)
    return r


def element_signature(el) -> dict:
    return {
        "tag": el.name,
        "id": el.get("id"),
        "class": el.get("class", []),
        "attrs": {k: v for k, v in el.attrs.items() if k not in {"class", "style"}},
        "text": " ".join(el.get_text(" ", strip=True).split())[:500],
        "html": str(el)[:3000],
    }


def main() -> None:
    OUT.mkdir(exist_ok=True)
    snapshots = []
    htmls = []
    for idx in (1, 2):
        r = fetch(TARGET)
        r.raise_for_status()
        raw = r.content
        htmls.append(raw)
        (OUT / f"sequoia-page-{idx}.html").write_bytes(raw)
        snapshots.append({
            "index": idx,
            "status": r.status_code,
            "requested_url": TARGET,
            "final_url": r.url,
            "bytes": len(raw),
            "sha256": sha256(raw),
            "headers": dict(r.headers),
        })
        time.sleep(2)

    soup = BeautifulSoup(htmls[0], "lxml")
    all_links = []
    for a in soup.find_all("a", href=True):
        href = urljoin(TARGET, a["href"])
        text = " ".join(a.get_text(" ", strip=True).split())
        all_links.append({"href": href, "text": text, "class": a.get("class", []), "rel": a.get("rel", [])})

    company_links = [x for x in all_links if re.search(r"/companies?/[^/?#]+/?(?:[?#].*)?$", x["href"])]
    external_links = [x for x in all_links if urlparse(x["href"]).netloc and urlparse(x["href"]).netloc not in {"sequoiacap.com", "www.sequoiacap.com"}]

    class_counts = collections.Counter()
    for el in soup.find_all(True):
        for cls in el.get("class", []):
            class_counts[cls] += 1

    anchors = []
    for needle in ["[24]7.ai", "100 Thieves", "Abby Care", "AdMob", "Agency", "Catch", "Zoom", "Zum"]:
        found = soup.find(string=lambda s: isinstance(s, str) and needle in s)
        if found:
            lineage = []
            cur = found.parent
            for _ in range(8):
                if not cur:
                    break
                lineage.append(element_signature(cur))
                cur = cur.parent
            anchors.append({"needle": needle, "lineage": lineage})

    scripts = []
    for idx, script in enumerate(soup.find_all("script")):
        text = script.string or script.get_text("", strip=False) or ""
        attrs = dict(script.attrs)
        if any(k in text.lower() for k in ["facetwp", "company", "wp-json", "ajax", "rest_url", "our-companies"]):
            scripts.append({"index": idx, "attrs": attrs, "text": text[:20000]})

    probes = {}
    probe_urls = [
        "https://sequoiacap.com/wp-json/",
        "https://sequoiacap.com/wp-json/wp/v2/types",
        "https://sequoiacap.com/wp-json/wp/v2/search?search=Airbnb&per_page=10",
        "https://sequoiacap.com/wp-json/wp/v2/company?per_page=1",
        "https://sequoiacap.com/wp-json/wp/v2/companies?per_page=1",
        "https://sequoiacap.com/wp-json/wp/v2/portfolio?per_page=1",
    ]
    for url in probe_urls:
        try:
            r = fetch(url)
            probes[url] = {
                "status": r.status_code,
                "content_type": r.headers.get("content-type"),
                "headers": dict(r.headers),
                "body_preview": r.text[:10000],
            }
        except Exception as exc:
            probes[url] = {"error": repr(exc)}

    summary = {
        "snapshots": snapshots,
        "snapshot_hash_equal": snapshots[0]["sha256"] == snapshots[1]["sha256"],
        "title": soup.title.get_text(strip=True) if soup.title else None,
        "all_anchor_count": len(all_links),
        "company_link_count_raw": len(company_links),
        "company_link_count_unique_href": len({x["href"] for x in company_links}),
        "company_link_count_unique_text": len({x["text"] for x in company_links if x["text"]}),
        "external_link_count_raw": len(external_links),
        "top_classes": class_counts.most_common(200),
        "company_links_first_100": company_links[:100],
        "company_links_last_100": company_links[-100:],
        "external_links_first_100": external_links[:100],
        "anchor_lineages": anchors,
        "matching_scripts": scripts,
        "probe_summary": {k: {kk: vv for kk, vv in v.items() if kk != "body_preview"} for k, v in probes.items()},
    }

    dump_json(OUT / "discovery-summary.json", summary)
    dump_json(OUT / "all-links.json", all_links)
    dump_json(OUT / "company-links.json", company_links)
    dump_json(OUT / "external-links.json", external_links)
    dump_json(OUT / "wp-probes.json", probes)

    print("SEQUOIA_DISCOVERY_START")
    print(json.dumps({
        "snapshots": snapshots,
        "all_anchor_count": len(all_links),
        "company_link_count_raw": len(company_links),
        "company_link_count_unique_href": len({x["href"] for x in company_links}),
        "company_link_count_unique_text": len({x["text"] for x in company_links if x["text"]}),
        "external_link_count_raw": len(external_links),
        "top_classes": class_counts.most_common(30),
        "probe_statuses": {k: v.get("status") for k, v in probes.items()},
    }, ensure_ascii=False, indent=2))
    print("SEQUOIA_DISCOVERY_END")


if __name__ == "__main__":
    main()
