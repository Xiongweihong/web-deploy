from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
import traceback
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

TARGET = "https://www.joinef.com/portfolio/"
AJAX = "https://www.joinef.com/wp-admin/admin-ajax.php"
OUT = Path("artifact")
UA = "Mozilla/5.0 (compatible; joinef-evidence-crawler/1.3)"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def save(path: Path, response: requests.Response, request_data: dict | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(response.content)
    path.with_suffix(path.suffix + ".meta.json").write_text(
        json.dumps(
            {
                "requested_url": response.request.url,
                "final_url": response.url,
                "method": response.request.method,
                "request_data": request_data,
                "status": response.status_code,
                "content_type": response.headers.get("content-type"),
                "date": response.headers.get("date"),
                "bytes": len(response.content),
                "sha256": sha256(response.content),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def get(url: str) -> requests.Response:
    response = requests.get(url, timeout=90, headers={"User-Agent": UA, "Accept": "*/*"})
    response.raise_for_status()
    return response


def post(data: dict) -> requests.Response:
    response = requests.post(
        AJAX,
        data=data,
        timeout=120,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
            "Origin": "https://www.joinef.com",
            "Referer": TARGET,
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    response.raise_for_status()
    return response


def parse_php_variables(html: bytes) -> dict:
    soup = BeautifulSoup(html, "lxml")
    script = soup.find("script", id="main-js-extra")
    text = script.get_text() if script else ""
    match = re.search(r"var php_variables = (\{.*?\});\s*var filter_settings", text, re.S)
    if not match:
        raise RuntimeError("php_variables not found")
    return json.loads(match.group(1))


def tile_rows(html: bytes) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    rows = []
    for node in soup.select(".tile--company .tile__link[data-companyslug]"):
        tile = node.find_parent(class_=lambda value: value and "tile--company" in value)
        rows.append(
            {
                "name": str(node.get("data-companyname") or node.get_text(" ", strip=True)).strip(),
                "slug": str(node.get("data-companyslug") or "").strip(),
                "index": str(node.get("data-index") or "").strip(),
                "featured": "tile--company--featured" in (tile.get("class") or []) if tile else False,
            }
        )
    return rows


def links(html: bytes) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    return [
        {
            "text": a.get_text(" ", strip=True),
            "href": urljoin(TARGET, a.get("href")) if a.get("href") else None,
            "class": a.get("class"),
            "target": a.get("target"),
            "rel": a.get("rel"),
            "attrs": dict(a.attrs),
        }
        for a in soup.find_all("a")
    ]


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    page = get(TARGET)
    save(OUT / "portfolio.html", page)
    variables = parse_php_variables(page.content)
    (OUT / "php-variables.json").write_text(
        json.dumps(variables, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )

    summary = {
        "current_page": variables.get("current_page"),
        "max_page": variables.get("max_page"),
        "initial_featured": tile_rows(page.content)[:24],
        "initial_all": tile_rows(page.content)[24:],
        "probes": {},
    }

    for page_number in (1, 2, 19, 20, 21):
        data = {
            "action": "loadmore",
            "query": variables["posts"],
            "page": str(page_number),
            "format": "default",
        }
        response = post(data)
        save(OUT / "loadmore" / f"page-{page_number:03d}.html", response, data)
        rows = tile_rows(response.content)
        summary["probes"][f"loadmore-{page_number}"] = {
            "bytes": len(response.content),
            "row_count": len(rows),
            "rows": rows,
            "prefix": response.text[:300],
        }

    for name, slug, featured, index in (
        ("Automata", "automata", "false", "0"),
        ("Tractable", "tractable", "true", "0"),
    ):
        data = {
            "action": "getcompany",
            "company": slug,
            "index": index,
            "featured": featured,
        }
        response = post(data)
        save(OUT / "company" / f"{slug}.html", response, data)
        summary["probes"][f"company-{slug}"] = {
            "bytes": len(response.content),
            "links": links(response.content),
            "prefix": response.text[:2000],
        }

    for sitemap_url in (
        "https://www.joinef.com/wp-sitemap.xml",
        "https://www.joinef.com/wp-sitemap-posts-company-1.xml",
        "https://www.joinef.com/company-sitemap.xml",
    ):
        key = sitemap_url.rsplit("/", 1)[-1]
        try:
            response = get(sitemap_url)
            save(OUT / "sitemaps" / key, response)
            summary["probes"][f"sitemap-{key}"] = {
                "status": response.status_code,
                "bytes": len(response.content),
                "prefix": response.text[:500],
            }
        except Exception as exc:  # noqa: BLE001
            summary["probes"][f"sitemap-{key}"] = {"error": repr(exc)}

    (OUT / "ajax-probe-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    print("AJAX_PROBE_SUMMARY_START")
    print(json.dumps({
        "current_page": summary["current_page"],
        "max_page": summary["max_page"],
        "initial_featured_count": len(summary["initial_featured"]),
        "initial_all_count": len(summary["initial_all"]),
        "probe_counts": {key: value.get("row_count") for key, value in summary["probes"].items() if key.startswith("loadmore-")},
        "automata_links": summary["probes"]["company-automata"]["links"],
    }, ensure_ascii=False, indent=2, sort_keys=True))
    print("AJAX_PROBE_SUMMARY_END")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "fatal-error.json").write_text(
            json.dumps({"error": repr(exc), "traceback": traceback.format_exc()}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        raise
