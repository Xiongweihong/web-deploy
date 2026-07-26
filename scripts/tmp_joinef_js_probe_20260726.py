from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
import traceback
from pathlib import Path

import requests
from bs4 import BeautifulSoup

TARGET = "https://www.joinef.com/portfolio/"
MAIN_JS = "https://www.joinef.com/wp-content/themes/joinef2023/js/dist/main.min.js?ver=1770013020"
OUT = Path("artifact")
UA = "Mozilla/5.0 (compatible; joinef-evidence-crawler/1.2)"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def get(url: str) -> requests.Response:
    response = requests.get(url, timeout=90, headers={"User-Agent": UA, "Accept": "*/*"})
    response.raise_for_status()
    return response


def save(path: Path, response: requests.Response) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(response.content)
    path.with_suffix(path.suffix + ".meta.json").write_text(
        json.dumps(
            {
                "url": response.url,
                "status": response.status_code,
                "content_type": response.headers.get("content-type"),
                "bytes": len(response.content),
                "sha256": sha256(response.content),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    page = get(TARGET)
    js = get(MAIN_JS)
    save(OUT / "portfolio.html", page)
    save(OUT / "main.min.js", js)

    soup = BeautifulSoup(page.content, "lxml")
    inline = soup.find("script", id="main-js-extra")
    inline_text = inline.get_text() if inline else ""
    (OUT / "main-js-extra.js").write_text(inline_text, encoding="utf-8")

    text = js.text
    needles = ["ajax_url", "paging__loadmore", "btn--loadmore", "loadmore-container", "pagenum", "php_variables", "featured_posts", "current_page", "max_page", "company-search", "admin-ajax", "action:", "action="]
    contexts = {}
    for needle in needles:
        found = []
        for match in re.finditer(re.escape(needle), text, re.I):
            start = max(0, match.start() - 1000)
            end = min(len(text), match.end() + 2500)
            found.append(text[start:end])
            if len(found) >= 20:
                break
        contexts[needle] = found
    (OUT / "contexts.json").write_text(
        json.dumps(contexts, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )

    # Pretty-ish splits around semicolons to make manual inspection easier.
    relevant = []
    for chunk in re.split(r"(?<=;)", text):
        if any(needle.casefold() in chunk.casefold() for needle in needles):
            relevant.append(chunk)
    (OUT / "relevant-chunks.txt").write_text("\n\n---\n\n".join(relevant), encoding="utf-8")

    summary = {
        "page_bytes": len(page.content),
        "main_js_bytes": len(js.content),
        "inline_extra_bytes": len(inline_text.encode("utf-8")),
        "context_counts": {key: len(value) for key, value in contexts.items()},
        "generated_at_epoch": time.time(),
    }
    (OUT / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


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
