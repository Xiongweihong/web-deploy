from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

OUT = Path("artifact")
OUT.mkdir(exist_ok=True)
UA = "Mozilla/5.0 (compatible; A16ZPortfolioAudit/1.0)"
TARGETS = {
    "portfolio": "https://a16z.com/portfolio/",
    "jobs": "https://jobs.a16z.com/companies",
    "jobs_page": "https://jobs.a16z.com/jobs",
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def get(url: str) -> requests.Response:
    return requests.get(url, headers={"User-Agent": UA, "Accept": "text/html,application/json,*/*"}, timeout=120)


def dump(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> None:
    report: dict[str, object] = {}
    all_scripts: list[dict] = []
    all_inline: list[dict] = []
    for label, url in TARGETS.items():
        r = get(url)
        r.raise_for_status()
        raw = r.content
        (OUT / f"{label}.html").write_bytes(raw)
        soup = BeautifulSoup(raw, "lxml")
        scripts = []
        for i, s in enumerate(soup.find_all("script")):
            src = s.get("src")
            text = s.string or s.get_text("", strip=False) or ""
            if src:
                abs_src = urljoin(r.url, src)
                scripts.append(abs_src)
                all_scripts.append({"source": label, "url": abs_src})
            elif text.strip():
                all_inline.append({"source": label, "index": i, "attrs": dict(s.attrs), "text": text[:500000]})
        links = [{"text": " ".join(a.get_text(" ", strip=True).split()), "href": urljoin(r.url, a.get("href"))} for a in soup.find_all("a", href=True)]
        report[label] = {
            "requested_url": url,
            "final_url": r.url,
            "status": r.status_code,
            "bytes": len(raw),
            "sha256": sha256(raw),
            "title": soup.title.get_text(" ", strip=True) if soup.title else None,
            "script_count": len(scripts),
            "scripts": scripts,
            "link_count": len(links),
            "links": links[:2000],
            "text_preview": " ".join(soup.get_text(" ", strip=True).split())[:10000],
            "next_data_present": bool(soup.find("script", id="__NEXT_DATA__")),
        }

    dump(OUT / "inline-scripts.json", all_inline)
    dump(OUT / "page-report.json", report)

    # Download unique JS bundles and scan for likely endpoints/configuration.
    js_urls = []
    seen = set()
    for item in all_scripts:
        u = item["url"]
        if u not in seen:
            seen.add(u)
            js_urls.append(u)
    scans = []
    url_pattern = re.compile(r"https?://[^\"'`\\\s<>]+")
    token_pattern = re.compile(r".{0,160}(?:api|graphql|portfolio|companies|consider|algolia|wp-json|search|jobs).{0,300}", re.I)
    for idx, url in enumerate(js_urls):
        try:
            r = get(url)
            body = r.content
            path = OUT / "js" / f"{idx:03d}-{Path(urlparse(url).path).name or 'bundle.js'}"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
            text = body.decode("utf-8", errors="replace")
            urls = sorted({m.group(0)[:1000] for m in url_pattern.finditer(text) if any(k in m.group(0).lower() for k in ["api", "graphql", "consider", "a16z", "portfolio", "company", "jobs"])})
            snippets = [m.group(0)[:500] for m in token_pattern.finditer(text)][:500]
            scans.append({"url": url, "status": r.status_code, "bytes": len(body), "sha256": sha256(body), "urls": urls, "snippets": snippets})
        except Exception as exc:
            scans.append({"url": url, "error": repr(exc)})
    dump(OUT / "js-scans.json", scans)

    # Probe likely a16z WordPress REST types and common custom post types.
    probes = {}
    probe_urls = [
        "https://a16z.com/wp-json/",
        "https://a16z.com/wp-json/wp/v2/types",
        "https://a16z.com/wp-json/wp/v2/search?search=Airbnb&per_page=100",
    ]
    for post_type in ["portfolio", "company", "companies", "investment", "investments", "portfolio_company", "portfolio-companies"]:
        probe_urls.append(f"https://a16z.com/wp-json/wp/v2/{post_type}?per_page=1")
    for url in probe_urls:
        try:
            r = get(url)
            probes[url] = {"status": r.status_code, "content_type": r.headers.get("content-type"), "headers": dict(r.headers), "body_preview": r.text[:50000]}
        except Exception as exc:
            probes[url] = {"error": repr(exc)}
    dump(OUT / "probes.json", probes)

    summary = {
        "page_statuses": {k: v["status"] for k, v in report.items()},
        "page_bytes": {k: v["bytes"] for k, v in report.items()},
        "unique_script_urls": len(js_urls),
        "js_scan_count": len(scans),
        "inline_script_count": len(all_inline),
        "probe_statuses": {k: v.get("status") for k, v in probes.items()},
    }
    dump(OUT / "discovery-summary.json", summary)
    print("A16Z_DISCOVERY_START")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("A16Z_DISCOVERY_END")


if __name__ == "__main__":
    main()
