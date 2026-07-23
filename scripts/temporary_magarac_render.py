from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

from playwright.sync_api import sync_playwright

TARGET_URL = "https://www.magarac.vc/portfolio"
OUT = Path("artifact/rendered")
OUT.mkdir(parents=True, exist_ok=True)


def clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def hint(src: str) -> str:
    name = unquote(urlparse(src).path.rsplit("/", 1)[-1])
    name = re.sub(r"\.(?:png|jpe?g|webp|svg|gif)$", "", name, flags=re.I)
    name = re.sub(r"^[0-9a-f]{10,}[_ -]*", "", name, flags=re.I)
    return clean(name.replace("_", " ").replace("-", " "))


def card_rows(page):
    rows = []
    cards = page.locator("a.company-link")
    for index in range(cards.count()):
        card = cards.nth(index)
        image = card.locator("img").first
        src = image.get_attribute("src") if image.count() else ""
        pane = card.locator("xpath=ancestor::*[contains(concat(' ',normalize-space(@class),' '),' w-tab-pane ')][1]")
        grid = card.locator("xpath=ancestor::*[contains(concat(' ',normalize-space(@class),' '),' grid-8 ')][1]")
        rows.append(
            {
                "index": index,
                "visible": card.is_visible(),
                "href": card.get_attribute("href") or "",
                "resolved_href": card.evaluate("el => el.href"),
                "text": clean(card.inner_text()),
                "image_src": src or "",
                "image_alt": clean(image.get_attribute("alt")) if image.count() else "",
                "image_hint": hint(src or ""),
                "tab": pane.get_attribute("data-w-tab") if pane.count() else "",
                "inside_grid_8": bool(grid.count()),
                "bbox": card.bounding_box(),
            }
        )
    return rows


def visible_summary(rows):
    return [row for row in rows if row["visible"]]


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(viewport={"width": 1600, "height": 1000}, locale="en-US")
    page = context.new_page()
    page.goto(TARGET_URL, wait_until="networkidle", timeout=120000)
    page.wait_for_timeout(3000)
    page.screenshot(path=str(OUT / "initial-full.png"), full_page=True)
    (OUT / "body.txt").write_text(page.locator("body").inner_text(), encoding="utf-8")

    stages = {}
    rows = card_rows(page)
    stages["initial"] = {"all": rows, "visible": visible_summary(rows)}

    navs = page.locator(".w-tab-link")
    nav_info = []
    for index in range(navs.count()):
        nav = navs.nth(index)
        label = clean(nav.inner_text())
        tab = nav.get_attribute("data-w-tab") or ""
        nav_info.append({"index": index, "label": label, "tab": tab, "visible": nav.is_visible()})
        if nav.is_visible():
            nav.click(force=True)
            page.wait_for_timeout(800)
            stage_rows = card_rows(page)
            stages[f"tab_{index}_{label}"] = {"all": stage_rows, "visible": visible_summary(stage_rows)}
            safe = re.sub(r"[^a-z0-9]+", "-", label.casefold()).strip("-") or str(index)
            page.screenshot(path=str(OUT / f"tab-{index}-{safe}.png"), full_page=True)

    report = {
        "url": page.url,
        "title": page.title(),
        "navs": nav_info,
        "stages": stages,
        "initial_all_count": len(rows),
        "initial_visible_count": len(visible_summary(rows)),
    }
    (OUT / "render-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    print("RENDER_REPORT_START")
    print(json.dumps({
        "url": report["url"],
        "title": report["title"],
        "navs": report["navs"],
        "stage_visible": {
            name: [(r["image_hint"], r["href"], r["inside_grid_8"], r["tab"]) for r in value["visible"]]
            for name, value in stages.items()
        },
    }, ensure_ascii=False, indent=2, sort_keys=True))
    print("RENDER_REPORT_END")
    browser.close()
