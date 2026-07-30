from __future__ import annotations

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


def fetch(url: str) -> requests.Response:
    return requests.get(url, headers={"User-Agent": UA, "Accept": "text/html,application/json;q=0.9,*/*;q=0.8"}, timeout=120)


def sig(el) -> dict:
    return {
        "tag": el.name,
        "id": el.get("id"),
        "class": el.get("class", []),
        "attrs": {k: v for k, v in el.attrs.items() if k not in {"class", "style"}},
        "text": " ".join(el.get_text(" ", strip=True).split())[:800],
        "html": str(el)[:5000],
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
        snapshots.append({"index": idx, "status": r.status_code, "final_url": r.url, "bytes": len(raw), "sha256": sha256(raw), "headers": dict(r.headers)})
        time.sleep(2)

    soup = BeautifulSoup(htmls[0], "lxml")
    links = []
    for a in soup.find_all("a", href=True):
        href = urljoin(TARGET, a["href"])
        links.append({
            "href": href,
            "text": " ".join(a.get_text(" ", strip=True).split()),
            "class": a.get("class", []),
            "parent_class": a.parent.get("class", []) if a.parent else [],
        })

    company_links = [x for x in links if re.search(r"/companies?/[^/?#]+/?(?:[?#].*)?$", x["href"])]
    externals = [x for x in links if urlparse(x["href"]).netloc not in {"", "sequoiacap.com", "www.sequoiacap.com"}]

    classes = collections.Counter(c for el in soup.find_all(True) for c in el.get("class", []))
    anchor_lineages = []
    for needle in ["[24]7.ai", "100 Thieves", "Abby Care", "AdMob", "Agency", "Catch", "Zoom", "Zum"]:
        found = soup.find(string=lambda s: isinstance(s, str) and needle in s)
        if found:
            chain = []
            cur = found.parent
            for _ in range(10):
                if not cur:
                    break
                chain.append(sig(cur))
                cur = cur.parent
            anchor_lineages.append({"needle": needle, "lineage": chain})

    scripts = []
    for idx, script in enumerate(soup.find_all("script")):
        text = script.string or script.get_text("", strip=False) or ""
        if any(key in text.lower() for key in ["facetwp", "wp-json", "our-companies", "company", "ajax"]):
            scripts.append({"index": idx, "attrs": dict(script.attrs), "text": text[:50000]})

    probes = {}
    for url in [
        "https://sequoiacap.com/wp-json/",
        "https://sequoiacap.com/wp-json/wp/v2/types",
        "https://sequoiacap.com/wp-json/wp/v2/search?search=Airbnb&per_page=10",
        "https://sequoiacap.com/wp-json/wp/v2/company?per_page=1",
        "https://sequoiacap.com/wp-json/wp/v2/companies?per_page=1",
        "https://sequoiacap.com/wp-json/wp/v2/portfolio?per_page=1",
    ]:
        try:
            r = fetch(url)
            probes[url] = {"status": r.status_code, "content_type": r.headers.get("content-type"), "headers": dict(r.headers), "body": r.text[:50000]}
        except Exception as exc:
            probes[url] = {"error": repr(exc)}

    summary = {
        "snapshots": snapshots,
        "snapshot_hash_equal": snapshots[0]["sha256"] == snapshots[1]["sha256"],
        "all_anchor_count": len(links),
        "company_links_raw": len(company_links),
        "company_links_unique_href": len({x["href"] for x in company_links}),
        "company_links_unique_text": len({x["text"] for x in company_links if x["text"]}),
        "external_links_raw": len(externals),
        "top_classes": classes.most_common(200),
        "company_links_first": company_links[:150],
        "company_links_last": company_links[-150:],
        "external_links_first": externals[:200],
        "anchor_lineages": anchor_lineages,
        "matching_scripts": scripts,
        "probe_statuses": {k: {kk: vv for kk, vv in v.items() if kk != "body"} for k, v in probes.items()},
    }
    dump_json(OUT / "discovery-summary.json", summary)
    dump_json(OUT / "all-links.json", links)
    dump_json(OUT / "company-links.json", company_links)
    dump_json(OUT / "external-links.json", externals)
    dump_json(OUT / "wp-probes.json", probes)

    print("SEQUOIA_DISCOVERY_START")
    print(json.dumps({
        "snapshots": snapshots,
        "all_anchor_count": len(links),
        "company_links_raw": len(company_links),
        "company_links_unique_href": len({x["href"] for x in company_links}),
        "company_links_unique_text": len({x["text"] for x in company_links if x["text"]}),
        "external_links_raw": len(externals),
        "top_classes": classes.most_common(35),
        "probe_statuses": {k: v.get("status") for k, v in probes.items()},
    }, ensure_ascii=False, indent=2))
    print("SEQUOIA_DISCOVERY_END")


if __name__ == "__main__":
    main()
