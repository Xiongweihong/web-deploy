from __future__ import annotations

import hashlib
import json
import re
import time
import traceback
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

TARGET_URL = "https://www.magarac.vc/portfolio"
OUT = Path("artifact")
RAW = OUT / "raw"
USER_AGENT = "Mozilla/5.0 (compatible; magarac-evidence-crawler/1.0)"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def save_bytes(path: Path, data: bytes, meta: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    payload = {**meta, "bytes": len(data), "sha256": digest(data)}
    path.with_suffix(path.suffix + ".meta.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )


def fetch(session: requests.Session, url: str, attempts: int = 5) -> requests.Response:
    error = None
    for attempt in range(1, attempts + 1):
        try:
            response = session.get(url, timeout=60, allow_redirects=True)
            if response.status_code in {408, 429} or response.status_code >= 500:
                raise RuntimeError(f"transient HTTP {response.status_code}")
            response.raise_for_status()
            return response
        except Exception as exc:
            error = exc
            if attempt < attempts:
                time.sleep(min(20, 2**attempt))
    raise RuntimeError(f"failed to fetch {url}: {error}")


def filename_hint(src: str) -> str:
    path = unquote(urlparse(src).path)
    name = path.rsplit("/", 1)[-1]
    name = re.sub(r"\.(?:png|jpe?g|webp|svg|gif)(?:\?.*)?$", "", name, flags=re.I)
    # Webflow asset names often begin with an opaque/hash-like identifier.
    name = re.sub(r"^[0-9a-f]{10,}[_ -]*", "", name, flags=re.I)
    return clean(name.replace("_", " ").replace("-", " "))


def is_external_company_href(href: str) -> bool:
    parsed = urlparse(href)
    host = parsed.netloc.casefold().removeprefix("www.")
    if parsed.scheme not in {"http", "https"} or not host:
        return False
    excluded = {
        "magarac.vc",
        "magaracventures.altareturn.com",
        "facebook.com",
        "linkedin.com",
        "twitter.com",
        "x.com",
    }
    return not any(host == value or host.endswith("." + value) for value in excluded)


def element_classes(element) -> list[str]:
    values = []
    node = element
    for _ in range(5):
        if node is None or not getattr(node, "attrs", None):
            break
        classes = node.get("class") or []
        values.extend(str(item) for item in classes)
        node = node.parent
    return values


def parse_snapshot(raw: bytes, base_url: str) -> dict:
    soup = BeautifulSoup(raw, "lxml")
    dyn_items = soup.select(".w-dyn-item")
    inventory = []
    for index, item in enumerate(dyn_items):
        inventory.append(
            {
                "index": index,
                "text": clean(item.get_text(" ", strip=True)),
                "classes": item.get("class") or [],
                "anchors": [
                    {
                        "href": urljoin(base_url, anchor.get("href")),
                        "text": clean(anchor.get_text(" ", strip=True)),
                        "class": anchor.get("class") or [],
                        "title": clean(anchor.get("title")),
                        "aria_label": clean(anchor.get("aria-label")),
                    }
                    for anchor in item.find_all("a", href=True)
                ],
                "images": [
                    {
                        "src": urljoin(base_url, image.get("src") or ""),
                        "alt": clean(image.get("alt")),
                        "class": image.get("class") or [],
                        "title": clean(image.get("title")),
                    }
                    for image in item.find_all("img")
                ],
                "html": str(item)[:20000],
            }
        )

    candidates = []
    seen = set()
    for anchor_index, anchor in enumerate(soup.find_all("a", href=True)):
        href = urljoin(base_url, anchor.get("href"))
        if not is_external_company_href(href):
            continue
        images = anchor.find_all("img")
        classes = element_classes(anchor)
        class_text = " ".join(classes).casefold()
        # Portfolio cards contain images and live in the portfolio/CMS collection area.
        likely = bool(images) or "portfolio" in class_text or "company" in class_text
        if not likely:
            continue
        key = href.rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        image_rows = []
        for image in images:
            src = urljoin(base_url, image.get("src") or "")
            image_rows.append(
                {
                    "src": src,
                    "alt": clean(image.get("alt")),
                    "title": clean(image.get("title")),
                    "filename_hint": filename_hint(src),
                    "class": image.get("class") or [],
                }
            )
        candidates.append(
            {
                "anchor_index": anchor_index,
                "source_url": href,
                "source_host": urlparse(href).netloc.casefold().removeprefix("www."),
                "anchor_text": clean(anchor.get_text(" ", strip=True)),
                "title": clean(anchor.get("title")),
                "aria_label": clean(anchor.get("aria-label")),
                "classes": classes,
                "images": image_rows,
                "parent_text": clean(anchor.parent.get_text(" ", strip=True)) if anchor.parent else "",
                "outer_html": str(anchor)[:20000],
            }
        )

    all_images = []
    for index, image in enumerate(soup.find_all("img")):
        src = urljoin(base_url, image.get("src") or "")
        parent_anchor = image.find_parent("a", href=True)
        all_images.append(
            {
                "index": index,
                "src": src,
                "alt": clean(image.get("alt")),
                "title": clean(image.get("title")),
                "filename_hint": filename_hint(src),
                "class": image.get("class") or [],
                "parent_href": urljoin(base_url, parent_anchor.get("href")) if parent_anchor else "",
                "ancestor_classes": element_classes(image),
            }
        )

    buttons = []
    for element in soup.find_all(["button", "a"]):
        text = clean(element.get_text(" ", strip=True))
        aria = clean(element.get("aria-label"))
        combined = f"{text} {aria}".casefold()
        if any(term in combined for term in ("load more", "show more", "view more", "next")):
            buttons.append(
                {
                    "tag": element.name,
                    "text": text,
                    "aria_label": aria,
                    "href": urljoin(base_url, element.get("href")) if element.get("href") else "",
                    "class": element.get("class") or [],
                }
            )

    return {
        "title": clean(soup.title.get_text(" ", strip=True)) if soup.title else "",
        "dynamic_item_count": len(dyn_items),
        "inventory": inventory,
        "candidate_count": len(candidates),
        "candidates": candidates,
        "all_image_count": len(all_images),
        "all_images": all_images,
        "pagination_controls": buttons,
        "script_sources": [urljoin(base_url, script.get("src")) for script in soup.find_all("script", src=True)],
    }


def main() -> None:
    if OUT.exists():
        import shutil

        shutil.rmtree(OUT)
    RAW.mkdir(parents=True)

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})

    snapshots = []
    for number in (1, 2):
        response = fetch(session, TARGET_URL)
        raw_path = RAW / f"portfolio-{number:02d}.html"
        meta = {
            "requested_url": TARGET_URL,
            "final_url": response.url,
            "status": response.status_code,
            "content_type": response.headers.get("content-type"),
            "date": response.headers.get("date"),
            "etag": response.headers.get("etag"),
            "last_modified": response.headers.get("last-modified"),
            "fetched_at_epoch": time.time(),
        }
        save_bytes(raw_path, response.content, meta)
        parsed = parse_snapshot(response.content, response.url)
        (RAW / f"parsed-{number:02d}.json").write_text(
            json.dumps(parsed, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )
        snapshots.append({"sha256": digest(response.content), "parsed": parsed, "meta": meta})
        time.sleep(2)

    comparison = {
        "html_hash_equal": snapshots[0]["sha256"] == snapshots[1]["sha256"],
        "candidate_counts": [item["parsed"]["candidate_count"] for item in snapshots],
        "candidate_urls_equal": [row["source_url"] for row in snapshots[0]["parsed"]["candidates"]]
        == [row["source_url"] for row in snapshots[1]["parsed"]["candidates"]],
        "dynamic_item_counts": [item["parsed"]["dynamic_item_count"] for item in snapshots],
        "pagination_controls": snapshots[-1]["parsed"]["pagination_controls"],
    }
    (OUT / "snapshot-comparison.json").write_text(
        json.dumps(comparison, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    (OUT / "candidate-records.json").write_text(
        json.dumps(snapshots[-1]["parsed"]["candidates"], ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    summary = {
        "target_url": TARGET_URL,
        "title": snapshots[-1]["parsed"]["title"],
        "candidate_count": snapshots[-1]["parsed"]["candidate_count"],
        "dynamic_item_count": snapshots[-1]["parsed"]["dynamic_item_count"],
        "all_image_count": snapshots[-1]["parsed"]["all_image_count"],
        "comparison": comparison,
    }
    (OUT / "discovery-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    print("DISCOVERY_SUMMARY_START")
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    print("CANDIDATES_START")
    for row in snapshots[-1]["parsed"]["candidates"]:
        print(json.dumps(row, ensure_ascii=False, sort_keys=True))
    print("CANDIDATES_END")
    print("DISCOVERY_SUMMARY_END")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        OUT.mkdir(parents=True, exist_ok=True)
        error = {"error": repr(exc), "traceback": traceback.format_exc()}
        (OUT / "fatal-error.json").write_text(
            json.dumps(error, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )
        print(error["traceback"])
        # Keep a zero exit so the evidence artifact is always uploaded.
