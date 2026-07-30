from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

BASE_RUNNER = Path(__file__).with_name("tmp_multi_vc_final_runner_20260731.py")
source = BASE_RUNNER.read_text(encoding="utf-8")
if "module.main()" not in source:
    raise RuntimeError("Base runner main call not found")
prefix, gate_suffix = source.split("module.main()", 1)
namespace: dict[str, object] = {}
exec(compile(prefix, str(BASE_RUNNER), "exec"), namespace)
module = namespace["module"]

original_render_dynamic_snapshot = module.render_dynamic_snapshot
original_enrich_details = module.enrich_details


def _gc_records_from_page(page):
    items = page.evaluate(
        """
        () => [...document.querySelectorAll('a.button.is-tertiary.w-button[href*="/companies/"]')]
          .map(a => {
            let p = a;
            let h = null;
            for (let i = 0; i < 8 && p; i++, p = p.parentElement) {
              h = p.querySelector('h2');
              if (h) break;
            }
            return {name: (h?.innerText || '').trim(), detail: a.href};
          })
        """
    )
    unique = {}
    for item in items:
        detail = str(item.get("detail") or "").rstrip("/")
        name = str(item.get("name") or "").strip()
        if detail and name:
            unique.setdefault(detail, item)
    return [
        module.record(
            "general-catalyst",
            detail.rsplit("/", 1)[-1],
            item["name"],
            detail_url=detail,
            raw=item,
        )
        for detail, item in unique.items()
    ]


def recover_general_catalyst(snapshot: int):
    with module.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 1200},
        )
        page = context.new_page()
        page.goto(
            module.SOURCE_PAGES["general-catalyst"],
            wait_until="domcontentloaded",
            timeout=120_000,
        )
        best = []
        stable_rounds = 0
        previous_count = -1
        for attempt in range(180):
            page.wait_for_timeout(400)
            records = _gc_records_from_page(page)
            if len(records) > len(best):
                best = records
            if len(records) >= 596:
                best = records
                break
            if len(records) == previous_count:
                stable_rounds += 1
            else:
                stable_rounds = 0
            previous_count = len(records)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            if stable_rounds >= 15:
                page.reload(wait_until="domcontentloaded", timeout=120_000)
                stable_rounds = 0
        (module.RAW / f"general-catalyst-browser-{snapshot}-stabilized.html").write_text(
            page.content(), encoding="utf-8"
        )
        module.dump(
            module.RAW / f"general-catalyst-stabilization-{snapshot}.json",
            {"final_count": len(best), "expected_count": 596},
        )
        browser.close()
    return best


def fixed_render_dynamic_snapshot(snapshot: int):
    result = original_render_dynamic_snapshot(snapshot)
    current = result.get("general-catalyst") or []
    if len(current) != 596:
        recovered = recover_general_catalyst(snapshot)
        if len(recovered) > len(current):
            result["general-catalyst"] = recovered
    return result


def index_website_from_page(page) -> str:
    return page.evaluate(
        """
        () => {
          const norm = s => (s || '').replace(/\s+/g, ' ').trim();
          const labels = [...document.querySelectorAll('dt,dd,li,div,p,span,strong')]
            .filter(el => norm(el.textContent).toLowerCase() === 'website');
          for (const label of labels) {
            let node = label;
            for (let depth = 0; depth < 7 && node; depth++, node = node.parentElement) {
              const links = [...node.querySelectorAll('a[href]')].filter(a => {
                try {
                  const host = new URL(a.href).hostname.replace(/^www\./, '');
                  return host && host !== 'indexventures.com';
                } catch { return false; }
              });
              if (links.length) return links[0].href;
            }
          }
          return '';
        }
        """
    )


def enrich_index_with_browser(rows):
    output = []
    with module.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        context.route(
            "**/*",
            lambda route: (
                route.abort()
                if route.request.resource_type in {"image", "media", "font"}
                else route.continue_()
            ),
        )
        page = context.new_page()
        page.goto(
            module.SOURCE_PAGES["index-ventures"],
            wait_until="domcontentloaded",
            timeout=120_000,
        )

        for position, row in enumerate(rows, start=1):
            enriched = dict(row)
            last_error = None
            for attempt in range(3):
                try:
                    response = page.goto(
                        row["detail_url"],
                        wait_until="domcontentloaded",
                        timeout=45_000,
                    )
                    status = response.status if response else None
                    if status == 403:
                        page.reload(wait_until="domcontentloaded", timeout=45_000)
                        status = 403
                    h1 = page.locator("h1").first
                    name = h1.inner_text(timeout=10_000).strip()
                    website = index_website_from_page(page)
                    enriched.update(
                        {
                            "name": name or row["name"],
                            "name_key": module.normalize_name(name or row["name"]),
                            "website": module.clean_url(website),
                            "domain": module.domain(module.clean_url(website)),
                            "detail_status": status,
                            "detail_final_url": page.url,
                            "detail_title": page.title(),
                        }
                    )
                    last_error = None
                    break
                except Exception as exc:
                    last_error = repr(exc)
                    page.wait_for_timeout(400 * (attempt + 1))
            if last_error:
                enriched["detail_error"] = last_error
            output.append(enriched)
            if position % 50 == 0:
                module.dump(
                    module.RAW / "index-browser-progress.json",
                    {
                        "processed": position,
                        "total": len(rows),
                        "website_count": sum(bool(x.get("website")) for x in output),
                        "error_count": sum(bool(x.get("detail_error")) for x in output),
                    },
                )
        browser.close()

    module.dump(
        module.RAW / "index-ventures-detail-manifest.json",
        [{k: v for k, v in item.items() if k != "source_raw"} for item in output],
    )
    return output


def fixed_enrich_details(session, kind, rows):
    if kind == "index-ventures":
        return enrich_index_with_browser(rows)
    return original_enrich_details(session, kind, rows)


module.render_dynamic_snapshot = fixed_render_dynamic_snapshot
module.enrich_details = fixed_enrich_details
module.main()
exec(compile(gate_suffix, str(BASE_RUNNER), "exec"), namespace)
