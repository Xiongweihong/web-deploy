from __future__ import annotations

import hashlib
import html as htmlmod
import json
import re
import shutil
import unicodedata
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import requests
import tldextract
from bs4 import BeautifulSoup

OUT = Path("artifact")
RAW = OUT / "raw"
UA = "Mozilla/5.0 (compatible; A16ZPortfolioAudit/2.0)"
PORTFOLIO_URL = "https://a16z.com/portfolio/"
JOBS_URL = "https://jobs.a16z.com/companies"
JOBS_API = "https://jobs.a16z.com/api-boards/search-companies"
MARKER = "未公开（a16z来源未提供有效公司网站）"
EXTRACT = tldextract.TLDExtract(suffix_list_urls=None)
GENERIC_DOMAINS = {
    "a16z.com", "consider.com", "linkedin.com", "facebook.com", "instagram.com",
    "twitter.com", "x.com", "youtube.com", "github.com", "greenhouse.io", "lever.co",
    "ashbyhq.com", "workable.com", "smartrecruiters.com", "cloudfront.net",
}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def dump(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "text/html,application/json,*/*"})
    return s


def clean_url(value: object) -> str:
    if not isinstance(value, str):
        return ""
    value = htmlmod.unescape(value).strip()
    if not value or value.lower() in {"none", "null", "n/a", "#"}:
        return ""
    if value.startswith("//"):
        value = "https:" + value
    if not re.match(r"^https?://", value, re.I):
        value = "https://" + value.lstrip("/")
    try:
        p = urlparse(value)
    except Exception:
        return ""
    if p.scheme.lower() not in {"http", "https"} or not p.netloc:
        return ""
    host = p.netloc.lower().strip().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    scheme = "https" if p.scheme.lower() in {"http", "https"} else p.scheme.lower()
    path = re.sub(r"/{2,}", "/", p.path or "")
    normalized = urlunparse((scheme, host, path.rstrip("/") if path != "/" else "", "", "", ""))
    return normalized.rstrip("/")


def registrable_domain(url: str) -> str:
    if not url:
        return ""
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return ""
    host = host.lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    ext = EXTRACT(host)
    domain = ".".join(x for x in [ext.domain, ext.suffix] if x)
    return domain or host


def normalize_name(name: str) -> str:
    name = unicodedata.normalize("NFKC", htmlmod.unescape(name or "")).casefold().strip()
    name = name.replace("&", " and ")
    name = re.sub(r"\b(incorporated|corporation|company|limited|holdings|technologies|technology|labs?|inc|corp|co|llc|ltd|plc)\b\.?", " ", name)
    name = re.sub(r"[^\w]+", "", name, flags=re.UNICODE)
    return name


class DSU:
    def __init__(self, n: int):
        self.p = list(range(n))
        self.r = [0] * n
    def find(self, x: int) -> int:
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x
    def union(self, a: int, b: int) -> None:
        a, b = self.find(a), self.find(b)
        if a == b:
            return
        if self.r[a] < self.r[b]:
            a, b = b, a
        self.p[b] = a
        if self.r[a] == self.r[b]:
            self.r[a] += 1


def fetch_portfolio(s: requests.Session, snapshot: int) -> list[dict]:
    r = s.get(PORTFOLIO_URL, timeout=180)
    r.raise_for_status()
    RAW.mkdir(parents=True, exist_ok=True)
    (RAW / f"portfolio-{snapshot}.html").write_bytes(r.content)
    soup = BeautifulSoup(r.content, "lxml")
    el = soup.find(attrs={"data-companies": True})
    if not el:
        raise RuntimeError("a16z portfolio data-companies attribute not found")
    companies = json.loads(el["data-companies"])
    dump(RAW / f"portfolio-{snapshot}.json", companies)
    return companies


def fetch_jobs(s: requests.Session, snapshot: int) -> tuple[list[dict], int, int]:
    r = s.get(JOBS_URL, timeout=120)
    r.raise_for_status()
    (RAW / f"jobs-page-{snapshot}.html").write_bytes(r.content)
    m = re.search(r"window\.serverInitialData\s*=\s*(\{.*?\});", r.text, re.S)
    if not m:
        raise RuntimeError("jobs serverInitialData not found")
    initial = json.loads(m.group(1))
    declared = int(initial["parents"]["items"]["andreessen-horowitz"]["numCompanies"])
    headers = {
        "X-CSRF-Token": initial["csrfToken"],
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Referer": JOBS_URL,
    }
    payload = {"query": {}, "meta": {"size": 1000}, "board": initial["board"]}
    api = s.post(JOBS_API, headers=headers, json=payload, timeout=180)
    api.raise_for_status()
    body = api.json()
    companies = body.get("companies") or []
    total = int(body.get("total", -1))
    dump(RAW / f"jobs-api-{snapshot}.json", body)
    return companies, total, declared


def portfolio_record(x: dict) -> dict:
    website = clean_url(x.get("company_url") or x.get("external_url") or x.get("url"))
    return {
        "source": "portfolio",
        "source_id": str(x.get("id") or ""),
        "name": str(x.get("name") or x.get("post_title") or "").strip(),
        "website": website,
        "domain": registrable_domain(website),
        "permalink": x.get("permalink"),
        "status": x.get("status"),
        "source_raw": x,
    }


def jobs_record(x: dict) -> dict:
    w = x.get("website") or {}
    website = clean_url(w.get("url") if isinstance(w, dict) else w)
    if not website and x.get("domain"):
        website = clean_url(str(x["domain"]))
    return {
        "source": "jobs",
        "source_id": str(x.get("slug") or x.get("id") or ""),
        "name": str(x.get("name") or x.get("id") or "").strip(),
        "website": website,
        "domain": registrable_domain(website),
        "permalink": f"https://jobs.a16z.com/companies/{x.get('slug')}" if x.get("slug") else None,
        "status": "hiring_directory",
        "source_raw": x,
    }


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    RAW.mkdir(parents=True)
    s = session()

    p1 = fetch_portfolio(s, 1)
    p2 = fetch_portfolio(s, 2)
    j1, jt1, jd1 = fetch_jobs(s, 1)
    j2, jt2, jd2 = fetch_jobs(s, 2)

    pmap1 = {str(x.get("id")): (x.get("name"), clean_url(x.get("company_url") or x.get("external_url") or x.get("url"))) for x in p1}
    pmap2 = {str(x.get("id")): (x.get("name"), clean_url(x.get("company_url") or x.get("external_url") or x.get("url"))) for x in p2}
    jmap1 = {str(x.get("slug") or x.get("id")): (x.get("name"), clean_url((x.get("website") or {}).get("url") if isinstance(x.get("website"), dict) else x.get("website"))) for x in j1}
    jmap2 = {str(x.get("slug") or x.get("id")): (x.get("name"), clean_url((x.get("website") or {}).get("url") if isinstance(x.get("website"), dict) else x.get("website"))) for x in j2}

    records = [portfolio_record(x) for x in p2] + [jobs_record(x) for x in j2]
    for r in records:
        r["name_key"] = normalize_name(r["name"])

    dsu = DSU(len(records))
    by_name: dict[str, list[int]] = defaultdict(list)
    by_domain: dict[str, list[int]] = defaultdict(list)
    for i, r in enumerate(records):
        if r["name_key"]:
            by_name[r["name_key"]].append(i)
        if r["domain"] and r["domain"] not in GENERIC_DOMAINS:
            by_domain[r["domain"]].append(i)
    for group in list(by_name.values()) + list(by_domain.values()):
        for i in group[1:]:
            dsu.union(group[0], i)

    components: dict[int, list[dict]] = defaultdict(list)
    for i, r in enumerate(records):
        components[dsu.find(i)].append(r)

    merged = []
    conflicts = []
    crosswalk = []
    for comp in components.values():
        port = [r for r in comp if r["source"] == "portfolio"]
        jobs = [r for r in comp if r["source"] == "jobs"]
        candidates = port or jobs
        canonical = sorted(candidates, key=lambda r: (len(r["name"]), r["name"].casefold()))[0]
        urls = []
        for r in port + jobs:
            if r["website"] and r["website"] not in urls:
                urls.append(r["website"])
        preferred = next((r["website"] for r in port if r["website"]), "") or next((r["website"] for r in jobs if r["website"]), "")
        domains = sorted({r["domain"] for r in comp if r["domain"]})
        if len(domains) > 1:
            conflicts.append({"canonical_name": canonical["name"], "domains": domains, "urls": urls, "records": [{k: r[k] for k in ["source", "source_id", "name", "website", "domain"]} for r in comp]})
        row = {
            "name": canonical["name"],
            "website": preferred or MARKER,
            "sources": sorted({r["source"] for r in comp}),
            "portfolio_records": [{"id": r["source_id"], "name": r["name"], "website": r["website"], "permalink": r["permalink"]} for r in port],
            "jobs_records": [{"id": r["source_id"], "name": r["name"], "website": r["website"], "permalink": r["permalink"]} for r in jobs],
            "all_names": sorted({r["name"] for r in comp}, key=str.casefold),
            "all_websites": urls,
            "domains": domains,
        }
        merged.append(row)
        if port and jobs:
            crosswalk.append(row)

    merged.sort(key=lambda x: (x["name"].casefold(), x["website"]))
    output_lines = [f"{x['name']} + {x['website']}" for x in merged]
    txt = OUT / f"a16z_{len(merged)}_deduplicated_companies_verified.txt"
    md = OUT / f"a16z_{len(merged)}_deduplicated_companies_verified.md"
    js = OUT / f"a16z_{len(merged)}_deduplicated_companies_verified.json"
    txt.write_text("\n".join(output_lines) + "\n", encoding="utf-8")
    md.write_text("# a16z Portfolio + Jobs 去重公司清单\n\n" + "\n".join(f"- {line}" for line in output_lines) + "\n", encoding="utf-8")
    dump(js, merged)

    portfolio_only = sum(x["sources"] == ["portfolio"] for x in merged)
    jobs_only = sum(x["sources"] == ["jobs"] for x in merged)
    both = sum(x["sources"] == ["jobs", "portfolio"] for x in merged)
    missing = [x for x in merged if x["website"] == MARKER]
    duplicate_output_names = {k: v for k, v in Counter(normalize_name(x["name"]) for x in merged).items() if k and v > 1}

    comparison = {
        "portfolio_snapshot_equal": pmap1 == pmap2,
        "jobs_snapshot_equal": jmap1 == jmap2,
        "portfolio_snapshot_1_count": len(p1),
        "portfolio_snapshot_2_count": len(p2),
        "jobs_snapshot_1_count": len(j1),
        "jobs_snapshot_2_count": len(j2),
        "jobs_api_total_1": jt1,
        "jobs_api_total_2": jt2,
        "jobs_page_declared_1": jd1,
        "jobs_page_declared_2": jd2,
        "portfolio_map_sha256_1": sha256_bytes(json.dumps(pmap1, ensure_ascii=False, sort_keys=True).encode()),
        "portfolio_map_sha256_2": sha256_bytes(json.dumps(pmap2, ensure_ascii=False, sort_keys=True).encode()),
        "jobs_map_sha256_1": sha256_bytes(json.dumps(jmap1, ensure_ascii=False, sort_keys=True).encode()),
        "jobs_map_sha256_2": sha256_bytes(json.dumps(jmap2, ensure_ascii=False, sort_keys=True).encode()),
    }
    dump(OUT / "a16z_source_snapshot_comparison.json", comparison)
    dump(OUT / "a16z_source_overlap_crosswalk.json", crosswalk)
    dump(OUT / "a16z_website_conflicts.json", conflicts)
    dump(OUT / "a16z_missing_websites.json", missing)
    dump(OUT / "a16z_source_records_portfolio.json", [portfolio_record(x) | {"source_raw": x} for x in p2])
    dump(OUT / "a16z_source_records_jobs.json", [jobs_record(x) | {"source_raw": x} for x in j2])

    validation = {
        "status": "PASS" if all([
            len(p1) == len(p2) == len(pmap1) == len(pmap2),
            len(j1) == len(j2) == jt1 == jt2 == jd1 == jd2 == len(jmap1) == len(jmap2),
            pmap1 == pmap2,
            jmap1 == jmap2,
            len(output_lines) == len(merged),
            not duplicate_output_names,
            portfolio_only + jobs_only + both == len(merged),
        ]) else "FAIL",
        "portfolio_source_count": len(p2),
        "portfolio_unique_ids": len(pmap2),
        "portfolio_unique_names": len({normalize_name(x.get("name") or "") for x in p2}),
        "jobs_source_count": len(j2),
        "jobs_api_total": jt2,
        "jobs_page_declared_count": jd2,
        "jobs_unique_slugs": len(jmap2),
        "jobs_unique_names": len({normalize_name(x.get("name") or "") for x in j2}),
        "union_before_dedupe": len(records),
        "deduplicated_output_count": len(merged),
        "overlap_components": both,
        "portfolio_only_components": portfolio_only,
        "jobs_only_components": jobs_only,
        "missing_website_count": len(missing),
        "website_conflict_component_count": len(conflicts),
        "duplicate_output_name_keys": duplicate_output_names,
        "txt_line_count": len(txt.read_text(encoding="utf-8").splitlines()),
        "txt_sha256": sha256_file(txt),
        "first_10": merged[:10],
        "last_10": merged[-10:],
    }
    dump(OUT / "a16z_validation.json", validation)

    contract = {
        "portfolio_source": PORTFOLIO_URL,
        "portfolio_locator": "HTML element data-companies attribute",
        "jobs_source": JOBS_URL,
        "jobs_api": JOBS_API,
        "jobs_api_payload": {"query": {}, "meta": {"size": 1000}, "board": {"id": "andreessen-horowitz", "isParent": True}},
        "identity_rules": ["same normalized company name", "same non-generic registrable website domain"],
        "website_precedence": ["portfolio website", "jobs website", "explicit missing marker"],
        "normalization": "Unicode NFKC, casefold, punctuation removal, common legal suffix removal",
    }
    dump(OUT / "a16z_source_contract.json", contract)

    # Independent reconstruction from merged JSON.
    independent = json.loads(js.read_text(encoding="utf-8"))
    rebuilt = "\n".join(f"{x['name']} + {x['website']}" for x in independent) + "\n"
    audit = {
        "status": "PASS" if rebuilt.encode() == txt.read_bytes() else "FAIL",
        "json_rows": len(independent),
        "txt_lines": len(txt.read_text(encoding="utf-8").splitlines()),
        "rebuilt_sha256": sha256_bytes(rebuilt.encode()),
        "txt_sha256": sha256_file(txt),
    }
    dump(OUT / "a16z_independent_audit.json", audit)

    manifest = {}
    for path in sorted(OUT.rglob("*")):
        if path.is_file():
            manifest[str(path.relative_to(OUT))] = {"bytes": path.stat().st_size, "sha256": sha256_file(path)}
    dump(OUT / "a16z_manifest.json", manifest)

    evidence_zip = OUT / f"a16z_{len(merged)}_companies_evidence.zip"
    with zipfile.ZipFile(evidence_zip, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for path in sorted(OUT.rglob("*")):
            if path.is_file() and path != evidence_zip:
                z.write(path, path.relative_to(OUT))

    result = {
        **validation,
        "independent_audit": audit["status"],
        "txt_path": str(txt),
        "md_path": str(md),
        "json_path": str(js),
        "validation_path": str(OUT / "a16z_validation.json"),
        "evidence_zip": str(evidence_zip),
        "evidence_zip_sha256": sha256_file(evidence_zip),
    }
    print("A16Z_FINAL_RESULT_START")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("A16Z_FINAL_RESULT_END")
    if validation["status"] != "PASS" or audit["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
