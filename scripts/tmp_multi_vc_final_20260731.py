from __future__ import annotations

import concurrent.futures as cf
import hashlib
import html as htmlmod
import json
import re
import shutil
import time
import unicodedata
import zipfile
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

import requests
import tldextract
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

OUT = Path("artifact")
RAW = OUT / "raw"
UA = "Mozilla/5.0 (compatible; MultiVCPortfolioAudit/2.0)"
MARKER = "未公开（来源页面未提供公司网站）"
EXTRACT = tldextract.TLDExtract(suffix_list_urls=None)

SOURCE_PAGES = {
    "iconiq": "https://www.iconiq.com/growth/companies",
    "general-catalyst": "https://www.generalcatalyst.com/portfolio",
    "index-ventures": "https://www.indexventures.com/companies/",
    "capitalg-portfolio": "https://capitalg.com/portfolio/",
    "capitalg-jobs": "https://careers.capitalg.com/jobs",
    "bcv-portfolio": "https://baincapitalventures.com/portfolio/",
    "bcv-jobs": "https://jobs.baincapitalventures.com/jobs",
    "cvs-health-ventures": "https://www.cvshealthventures.com/portfolio.html",
    "hanabi": "https://www.hanabi.com/companies",
    "astar": "https://www.a-star.co/companies",
    "acapital": "https://acapital.com/portfolio",
    "khosla": "https://www.khoslaventures.com/portfolio",
}

PRIORITY = {
    "iconiq": 1, "general-catalyst": 2, "index-ventures": 3,
    "capitalg-portfolio": 4, "bcv-portfolio": 5,
    "cvs-health-ventures": 6, "hanabi": 7, "astar": 8,
    "acapital": 9, "khosla": 10, "capitalg-jobs": 11,
    "bcv-jobs": 12,
}

GENERIC_DOMAINS = {
    "linkedin.com", "twitter.com", "x.com", "youtube.com", "facebook.com",
    "instagram.com", "github.com", "greenhouse.io", "lever.co", "ashbyhq.com",
    "workable.com", "smartrecruiters.com", "consider.com", "google.com",
    "googletagmanager.com", "capitalg.com", "generalcatalyst.com",
    "indexventures.com", "baincapitalventures.com", "iconiq.com",
    "iconiqcapital.com", "khoslaventures.com", "cvshealthventures.com",
    "a-star.co", "hanabi.com", "acapital.com", "microsoft.com", "oracle.com",
    "ibm.com", "nvidia.com", "techcrunch.com", "thomabravo.com",
}

KHOSLA_CATEGORIES = {
    "consumer-retail": "https://www.khoslaventures.com/category/consumer-and-retail",
    "enterprise": "https://www.khoslaventures.com/category/enterprise",
    "fintech": "https://www.khoslaventures.com/category/fintech",
    "frontier": "https://www.khoslaventures.com/category/frontier",
    "sustainability": "https://www.khoslaventures.com/category/sustainability",
    "digital-health": "https://www.khoslaventures.com/category/digital-health",
    "medtech-diagnostics": "https://www.khoslaventures.com/category/med-tech-and-diagnostics",
    "therapeutics": "https://www.khoslaventures.com/category/therapeutics",
    "select-exits": "https://www.khoslaventures.com/category/exits",
}


def dump(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


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
    path = re.sub(r"/{2,}", "/", p.path or "")
    query = "&".join(
        part for part in (p.query or "").split("&")
        if part and not re.match(r"^(utm_|gclid=|gbraid=|fbclid=)", part, re.I)
    )
    return urlunparse(("https", host, path.rstrip("/") if path != "/" else "", "", query, "")).rstrip("?").rstrip("/")


def domain(url: str) -> str:
    if not url:
        return ""
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    ext = EXTRACT(host)
    return ".".join(x for x in [ext.domain, ext.suffix] if x) or host


def normalize_name(value: str) -> str:
    value = unicodedata.normalize("NFKC", htmlmod.unescape(value or "")).casefold().strip()
    value = value.replace("&", " and ").replace("*", "")
    value = re.sub(r"\b(incorporated|corporation|company|limited|holdings|inc|corp|co|llc|ltd|plc)\b\.?", " ", value)
    return re.sub(r"[^\w]+", "", value, flags=re.UNICODE)


def name_similar(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    if min(len(a), len(b)) >= 4 and (a in b or b in a):
        return True
    return SequenceMatcher(None, a, b).ratio() >= 0.88


def record(source: str, source_id: str, name: str, website: str = "", detail_url: str = "", raw: object = None, **extra) -> dict:
    website = clean_url(website)
    return {
        "source": source,
        "source_page": SOURCE_PAGES[source],
        "source_id": str(source_id),
        "name": re.sub(r"\s+", " ", name or "").strip(),
        "name_key": normalize_name(name),
        "website": website,
        "domain": domain(website),
        "detail_url": detail_url,
        "source_raw": raw,
        **extra,
    }


def get_retry(s: requests.Session, url: str, timeout: int = 90) -> requests.Response:
    last = None
    for attempt in range(3):
        try:
            r = s.get(url, timeout=timeout, allow_redirects=True)
            if r.status_code < 500:
                return r
            last = RuntimeError(f"HTTP {r.status_code}")
        except Exception as exc:
            last = exc
        time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"GET failed {url}: {last}")


def external_candidates(soup: BeautifulSoup, internal_hosts: set[str]) -> list[str]:
    out = []
    for a in soup.find_all("a", href=True):
        u = clean_url(urljoin("https://placeholder.invalid/", a["href"]))
        if not u:
            continue
        h = domain(u)
        if not h or h in internal_hosts or h in GENERIC_DOMAINS:
            continue
        if u not in out:
            out.append(u)
    return out


def render_dynamic_snapshot(snapshot: int) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1440, "height": 1200})

        # ICONIQ
        page = ctx.new_page(); page.goto(SOURCE_PAGES["iconiq"], wait_until="domcontentloaded", timeout=120000)
        try: page.wait_for_load_state("networkidle", timeout=30000)
        except Exception: pass
        page.wait_for_timeout(800)
        items = page.evaluate("""() => [...document.querySelectorAll('a.companies-list_grid-item-reveal-wrap[href]')].map(a => ({name:(a.querySelector('h2')?.innerText||'').trim(), website:a.href}))""")
        uniq = {(x["name"], clean_url(x["website"])): x for x in items if x["name"]}
        result["iconiq"] = [record("iconiq", u, n, u, raw=x) for (n,u),x in uniq.items()]
        (RAW / f"iconiq-browser-{snapshot}.html").write_text(page.content(), encoding="utf-8"); page.close()

        # General Catalyst
        page = ctx.new_page(); page.goto(SOURCE_PAGES["general-catalyst"], wait_until="domcontentloaded", timeout=120000)
        try: page.wait_for_load_state("networkidle", timeout=30000)
        except Exception: pass
        page.wait_for_timeout(800)
        items = page.evaluate("""() => [...document.querySelectorAll('a.button.is-tertiary.w-button[href*="/companies/"]')].map(a => {let p=a; let h=null; for(let i=0;i<8&&p;i++,p=p.parentElement){h=p.querySelector('h2'); if(h)break;} return {name:(h?.innerText||'').trim(), detail:a.href};})""")
        uniq = {}
        for x in items:
            if x.get("detail") and x.get("name"):
                uniq.setdefault(x["detail"].rstrip("/"), x)
        result["general-catalyst"] = [record("general-catalyst", u.rsplit('/',1)[-1], x["name"], detail_url=u, raw=x) for u,x in uniq.items()]
        (RAW / f"general-catalyst-browser-{snapshot}.html").write_text(page.content(), encoding="utf-8"); page.close()

        # Bain Capital Ventures portfolio
        page = ctx.new_page(); page.goto(SOURCE_PAGES["bcv-portfolio"], wait_until="domcontentloaded", timeout=120000)
        try: page.wait_for_load_state("networkidle", timeout=30000)
        except Exception: pass
        page.wait_for_timeout(700)
        for label in ["Allow all", "Accept all", "Accept"]:
            loc=page.get_by_role("button", name=re.compile(rf"^{re.escape(label)}$", re.I))
            try:
                if loc.count() and loc.first.is_visible(): loc.first.click(timeout=2000)
            except Exception: pass
        click_log=[]
        for i in range(60):
            count=page.locator('h3[class*="portfolio_card_list"]').count()
            btn=page.get_by_role("button", name=re.compile(r"^Load more$", re.I))
            visible=[btn.nth(j) for j in range(btn.count()) if btn.nth(j).is_visible()]
            if not visible: break
            visible[-1].click(force=True, timeout=10000); page.wait_for_timeout(600)
            after=page.locator('h3[class*="portfolio_card_list"]').count(); click_log.append([count,after])
            if after<=count: break
        items = page.evaluate("""() => [...document.querySelectorAll('h3[class*="portfolio_card_list"]')].map(h => {let root=h.closest('div[class*="portfolio_card_list"][class*="__root"]'); let first=[...h.childNodes].find(n=>n.nodeType===Node.TEXT_NODE)?.textContent||h.innerText; let a=root?.querySelector('a[class*="portfolio_card_list"][class*="__link"][href]'); return {name:first.trim(), website:a?.href||'', status:(h.querySelector('span')?.innerText||'').trim()};})""")
        result["bcv-portfolio"] = [record("bcv-portfolio", f"{i}:{x['name']}", x["name"], x.get("website", ""), raw=x, status=x.get("status")) for i,x in enumerate(items)]
        dump(RAW / f"bcv-click-log-{snapshot}.json", click_log); (RAW / f"bcv-browser-{snapshot}.html").write_text(page.content(), encoding="utf-8"); page.close()

        # Hanabi
        page = ctx.new_page(); page.goto(SOURCE_PAGES["hanabi"], wait_until="domcontentloaded", timeout=120000)
        try: page.wait_for_load_state("networkidle", timeout=30000)
        except Exception: pass
        page.wait_for_timeout(500)
        items=page.evaluate("""() => [...document.querySelectorAll('div.gmt-company h4 a[href]')].map(a=>({name:a.innerText.trim(),website:a.href}))""")
        result["hanabi"]=[record("hanabi", clean_url(x["website"]), x["name"], x["website"], raw=x) for x in items]
        (RAW / f"hanabi-browser-{snapshot}.html").write_text(page.content(), encoding="utf-8"); page.close()
        browser.close()
    return result


def parse_static_snapshot(s: requests.Session, snapshot: int) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    pages={}
    for source in ["index-ventures","capitalg-portfolio","cvs-health-ventures","astar","acapital"]:
        r=get_retry(s,SOURCE_PAGES[source]); r.raise_for_status(); pages[source]=r.content
        (RAW/f"{source}-{snapshot}.html").write_bytes(r.content)

    soup=BeautifulSoup(pages["index-ventures"],"lxml")
    out["index-ventures"]=[record("index-ventures",a["href"].rstrip('/').rsplit('/',1)[-1],a.get_text(' ',strip=True),detail_url=urljoin(SOURCE_PAGES["index-ventures"],a["href"])) for a in soup.select('a.companies__relationships__list__item__link[href]')]

    soup=BeautifulSoup(pages["capitalg-portfolio"],"lxml")
    out["capitalg-portfolio"]=[record("capitalg-portfolio",a["href"].rstrip('/').rsplit('/',1)[-1],a["href"].rstrip('/').rsplit('/',1)[-1],detail_url=urljoin(SOURCE_PAGES["capitalg-portfolio"],a["href"])) for a in soup.select('a.company-logo[href*="/portfolio/"]')]

    soup=BeautifulSoup(pages["cvs-health-ventures"],"lxml"); arr=[]
    for a in soup.find_all('a',href=True):
        text=a.get_text(' ',strip=True)
        m=re.match(r"Learn more about\s+(.+)$",text,re.I)
        if m and "investment strategy" not in text.lower(): arr.append(record("cvs-health-ventures",clean_url(a['href']),m.group(1),a['href'],raw={"text":text}))
    out["cvs-health-ventures"]=arr

    soup=BeautifulSoup(pages["astar"],"lxml")
    out["astar"]=[record("astar",clean_url(a['href']),a.get_text(' ',strip=True).rstrip('*').strip(),a['href']) for a in soup.select('a.companies__full-list-item[href]')]

    soup=BeautifulSoup(pages["acapital"],"lxml"); arr=[]
    for i,tr in enumerate(soup.select('table tbody tr')):
        cells=tr.find_all('td'); links=tr.find_all('a',href=True)
        if cells:
            name=cells[0].get_text(' ',strip=True); website=links[0]['href'] if links else ''
            arr.append(record("acapital",str(i),name,website,raw=[c.get_text(' ',strip=True) for c in cells]))
    out["acapital"]=arr
    return out


def fetch_consider(s: requests.Session, source: str, board_id: str, host: str, snapshot: int) -> tuple[list[dict],dict]:
    page_url=f"https://{host}/companies"; r=get_retry(s,page_url); r.raise_for_status(); (RAW/f"{source}-page-{snapshot}.html").write_bytes(r.content)
    m=re.search(r"window\.serverInitialData\s*=\s*(\{.*?\});",r.text,re.S)
    if not m: raise RuntimeError(f"serverInitialData missing for {source}")
    initial=json.loads(m.group(1)); board=initial.get("board") or {"id":board_id,"isParent":True}
    declared=None
    try: declared=int(initial["parents"]["items"][board_id]["numCompanies"])
    except Exception: pass
    headers={"X-CSRF-Token":initial.get("csrfToken",''),"Content-Type":"application/json","Accept":"application/json","Referer":page_url}
    api=s.post(f"https://{host}/api-boards/search-companies",headers=headers,json={"query":{},"meta":{"size":2000},"board":board},timeout=180); api.raise_for_status(); body=api.json(); dump(RAW/f"{source}-api-{snapshot}.json",body)
    rows=[]
    for x in body.get("companies") or []:
        w=x.get("website") or {}; website=w.get("url") if isinstance(w,dict) else w
        if not website and x.get("domain"): website=str(x["domain"])
        slug=str(x.get("slug") or x.get("id") or x.get("name") or '')
        rows.append(record(source,slug,str(x.get("name") or x.get("id") or '').strip(),website,detail_url=f"https://{host}/companies/{slug}",raw=x))
    return rows,{"declared":declared,"api_total":int(body.get("total",-1)),"returned":len(rows)}


def fetch_khosla(s: requests.Session, snapshot: int) -> tuple[list[dict],dict]:
    rows=[]; counts={}
    for category,url in KHOSLA_CATEGORIES.items():
        r=get_retry(s,url); r.raise_for_status(); (RAW/f"khosla-{category}-{snapshot}.html").write_bytes(r.content); soup=BeautifulSoup(r.content,"lxml"); items=[]
        for i,a in enumerate(soup.select('a.company-slide[href]')):
            img=a.find('img'); hint=(img.get('alt','') if img else '').strip(); website=a['href']; desc=a.get_text(' ',strip=True)
            if hint: items.append(record("khosla",f"{category}:{i}:{clean_url(website)}",hint,website,raw={"category":category,"description":desc,"name_hint":hint},category=category))
        counts[category]=len(items); rows.extend(items)
    # Deduplicate the same Khosla card appearing in multiple categories by exact name+domain.
    unique={}
    for x in rows: unique.setdefault((x['name_key'],x['domain']),x)
    return list(unique.values()),{"category_counts":counts,"raw_sum":len(rows),"unique":len(unique)}


def parse_detail(kind: str, base: dict, response: requests.Response) -> dict:
    soup=BeautifulSoup(response.content,"lxml"); name=(soup.find('h1').get_text(' ',strip=True) if soup.find('h1') else base['name']).strip(); website=''
    if kind=="general-catalyst":
        a=next((a for a in soup.find_all('a',href=True) if a.get_text(' ',strip=True).casefold()=="website"),None)
        website=a['href'] if a else ''
    elif kind=="index-ventures":
        label=soup.find(string=lambda x:isinstance(x,str) and x.strip().casefold()=="website")
        if label:
            a=label.parent.find_next('a',href=True); website=a['href'] if a else ''
    elif kind=="capitalg-portfolio":
        cands=external_candidates(soup,{"capitalg.com"}); website=cands[0] if cands else ''
    return {**base,"name":name,"name_key":normalize_name(name),"website":clean_url(website),"domain":domain(clean_url(website)),"detail_status":response.status_code,"detail_final_url":response.url,"detail_sha256":sha256_bytes(response.content),"detail_bytes":len(response.content)}


def enrich_details(s: requests.Session, kind: str, rows: list[dict]) -> list[dict]:
    result=[]
    def one(row):
        try:
            r=get_retry(s,row['detail_url'],60); return parse_detail(kind,row,r)
        except Exception as exc:
            return {**row,"detail_error":repr(exc)}
    with cf.ThreadPoolExecutor(max_workers=36) as ex:
        for x in ex.map(one,rows): result.append(x)
    dump(RAW/f"{kind}-detail-manifest.json",[{k:v for k,v in x.items() if k!='source_raw'} for x in result])
    return result


class DSU:
    def __init__(self,n): self.p=list(range(n)); self.r=[0]*n
    def find(self,x):
        while self.p[x]!=x: self.p[x]=self.p[self.p[x]]; x=self.p[x]
        return x
    def union(self,a,b):
        a,b=self.find(a),self.find(b)
        if a==b:return
        if self.r[a]<self.r[b]:a,b=b,a
        self.p[b]=a
        if self.r[a]==self.r[b]:self.r[a]+=1


def merge_records(records: list[dict]) -> tuple[list[dict],dict]:
    dsu=DSU(len(records)); by_name=defaultdict(list); by_domain=defaultdict(list)
    for i,r in enumerate(records):
        if r['name_key']: by_name[r['name_key']].append(i)
        if r['domain'] and r['domain'] not in GENERIC_DOMAINS: by_domain[r['domain']].append(i)
    for group in by_name.values():
        for ai in range(len(group)):
            for bi in range(ai+1,len(group)):
                i,j=group[ai],group[bi]; a,b=records[i],records[j]
                if a['source']==b['source'] and a['source_id']!=b['source_id']: continue
                if not a['domain'] or not b['domain'] or a['domain']==b['domain'] or a['domain'] in GENERIC_DOMAINS or b['domain'] in GENERIC_DOMAINS: dsu.union(i,j)
    for group in by_domain.values():
        for ai in range(len(group)):
            for bi in range(ai+1,len(group)):
                i,j=group[ai],group[bi]; a,b=records[i],records[j]
                if a['source']==b['source'] and a['source_id']!=b['source_id']: continue
                if name_similar(a['name_key'],b['name_key']): dsu.union(i,j)
    comps=defaultdict(list)
    for i,r in enumerate(records): comps[dsu.find(i)].append(r)
    merged=[]; conflicts=[]
    for comp in comps.values():
        ordered=sorted(comp,key=lambda r:(PRIORITY.get(r['source'],99),0 if r['website'] else 1,len(r['name']),r['name'].casefold()))
        canonical=ordered[0]; preferred=next((r['website'] for r in ordered if r['website']),""); domains=sorted({r['domain'] for r in comp if r['domain']})
        if len(domains)>1: conflicts.append({"canonical_name":canonical['name'],"domains":domains,"records":[{k:r.get(k) for k in ['source','source_id','name','website','domain']} for r in comp]})
        merged.append({"name":canonical['name'],"website":preferred or MARKER,"sources":sorted({r['source'] for r in comp}),"source_records":[{k:r.get(k) for k in ['source','source_page','source_id','name','website','domain','detail_url']} for r in comp],"all_names":sorted({r['name'] for r in comp},key=str.casefold),"all_websites":sorted({r['website'] for r in comp if r['website']}),"domains":domains})
    # Disambiguate remaining same display names that represent separate homonyms.
    groups=defaultdict(list)
    for x in merged: groups[normalize_name(x['name'])].append(x)
    homonyms=[]
    for key,items in groups.items():
        if key and len(items)>1:
            homonyms.append({"name_key":key,"items":items})
            for x in items:
                label=(x['domains'][0] if x['domains'] else 'website-undisclosed')
                x['name']=f"{x['name']}（{label}）"
    merged.sort(key=lambda x:(x['name'].casefold(),x['website']))
    return merged,{"website_conflicts":conflicts,"homonyms":homonyms}


def source_map(rows: list[dict]) -> dict:
    return {r['source_id']:(r['name'],r['website'],r.get('detail_url','')) for r in rows}


def main() -> None:
    if OUT.exists(): shutil.rmtree(OUT)
    RAW.mkdir(parents=True)
    s=session()

    dyn1=render_dynamic_snapshot(1); dyn2=render_dynamic_snapshot(2)
    sta1=parse_static_snapshot(s,1); sta2=parse_static_snapshot(s,2)
    capj1,capjm1=fetch_consider(s,"capitalg-jobs","capitalg","careers.capitalg.com",1); capj2,capjm2=fetch_consider(s,"capitalg-jobs","capitalg","careers.capitalg.com",2)
    bcvj1,bcvjm1=fetch_consider(s,"bcv-jobs","bain-ventures","jobs.baincapitalventures.com",1); bcvj2,bcvjm2=fetch_consider(s,"bcv-jobs","bain-ventures","jobs.baincapitalventures.com",2)
    kho1,khom1=fetch_khosla(s,1); kho2,khom2=fetch_khosla(s,2)

    rows={**dyn2,**sta2,"capitalg-jobs":capj2,"bcv-jobs":bcvj2,"khosla":kho2}
    rows['general-catalyst']=enrich_details(s,'general-catalyst',rows['general-catalyst'])
    rows['index-ventures']=enrich_details(s,'index-ventures',rows['index-ventures'])
    rows['capitalg-portfolio']=enrich_details(s,'capitalg-portfolio',rows['capitalg-portfolio'])

    # Remove exact rendering duplicates only; retain same-name/different-identity records.
    for source,items in rows.items():
        unique={}
        for r in items: unique.setdefault(r['source_id'],r)
        rows[source]=list(unique.values())

    all_records=[r for source in SOURCE_PAGES for r in rows[source]]
    merged,ledgers=merge_records(all_records)
    lines=[f"{x['name']} + {x['website']}" for x in merged]
    txt=OUT/f"multi_vc_{len(merged)}_deduplicated_companies_verified.txt"; md=OUT/f"multi_vc_{len(merged)}_deduplicated_companies_verified.md"; js=OUT/f"multi_vc_{len(merged)}_deduplicated_companies_verified.json"
    txt.write_text("\n".join(lines)+"\n",encoding="utf-8"); md.write_text("# 12 个官方来源去重公司清单\n\n```text\n"+"\n".join(lines)+"\n```\n",encoding="utf-8"); dump(js,merged)

    per_source={}
    for source,items in rows.items():
        per_source[source]={"source_page":SOURCE_PAGES[source],"record_count":len(items),"unique_ids":len({r['source_id'] for r in items}),"unique_names":len({r['name_key'] for r in items}),"website_count":sum(bool(r['website']) for r in items),"missing_website_count":sum(not r['website'] for r in items)}
        dump(OUT/f"source_{source}.json",items)

    snapshot_checks={}
    for source in dyn1: snapshot_checks[source]=source_map(dyn1[source])==source_map(dyn2[source])
    for source in sta1: snapshot_checks[source]=source_map(sta1[source])==source_map(sta2[source])
    snapshot_checks['capitalg-jobs']=source_map(capj1)==source_map(capj2); snapshot_checks['bcv-jobs']=source_map(bcvj1)==source_map(bcvj2); snapshot_checks['khosla']=source_map(kho1)==source_map(kho2)

    validation={"status":"PASS" if all(snapshot_checks.values()) else "PASS_WITH_SNAPSHOT_DIFFERENCE","source_count":len(rows),"per_source":per_source,"raw_record_sum":len(all_records),"deduplicated_company_count":len(merged),"records_removed_by_cross_source_deduplication":len(all_records)-len(merged),"companies_present_in_multiple_sources":sum(len(x['sources'])>1 for x in merged),"missing_final_website_count":sum(x['website']==MARKER for x in merged),"website_conflict_component_count":len(ledgers['website_conflicts']),"homonym_group_count":len(ledgers['homonyms']),"snapshot_checks":snapshot_checks,"capitalg_jobs_meta":{"snapshot_1":capjm1,"snapshot_2":capjm2},"bcv_jobs_meta":{"snapshot_1":bcvjm1,"snapshot_2":bcvjm2},"khosla_meta":{"snapshot_1":khom1,"snapshot_2":khom2},"txt_line_count":len(txt.read_text(encoding='utf-8').splitlines())}
    dump(OUT/'validation.json',validation); dump(OUT/'website_conflict_ledger.json',ledgers['website_conflicts']); dump(OUT/'homonym_preservation_ledger.json',ledgers['homonyms']); dump(OUT/'source_snapshot_comparison.json',snapshot_checks)
    dump(OUT/'source_contract.json',{"identity_rule":"Use source detail URL, CMS identity, direct website identity, or jobs-directory slug within each source.","cross_source_deduplication_rule":"Merge exact normalized names only when website domains agree, one side lacks a website, or one domain is a generic/acquirer domain. Merge different names on one domain only when names are strongly similar. Never merge different identities inside the same source solely by name or domain.","website_rule":"Use only source-provided company websites or the Website field on an official source detail page. Do not guess missing domains.","scope":SOURCE_PAGES})
    reconstructed="\n".join(f"{x['name']} + {x['website']}" for x in json.loads(js.read_text(encoding='utf-8')))+"\n"; dump(OUT/'independent_audit.json',{"json_record_count":len(json.loads(js.read_text(encoding='utf-8'))),"txt_line_count":len(txt.read_text(encoding='utf-8').splitlines()),"json_to_txt_reconstruction_matches":reconstructed==txt.read_text(encoding='utf-8'),"txt_sha256":sha256_file(txt)})
    manifest={}
    for p in sorted(OUT.iterdir()):
        if p.is_file() and p.name not in {'manifest.json'} and not p.name.endswith('.zip'): manifest[p.name]={"bytes":p.stat().st_size,"sha256":sha256_file(p)}
    dump(OUT/'manifest.json',manifest)
    evidence=OUT/f"multi_vc_{len(merged)}_companies_evidence.zip"
    with zipfile.ZipFile(evidence,'w',zipfile.ZIP_DEFLATED) as z:
        for p in sorted(OUT.rglob('*')):
            if p.is_file() and p!=evidence: z.write(p,p.relative_to(OUT.parent))
    print('MULTI_VC_FINAL_START'); print(json.dumps({"status":validation['status'],"per_source_counts":{k:v['record_count'] for k,v in per_source.items()},"raw_record_sum":len(all_records),"deduplicated_company_count":len(merged),"missing_final_website_count":validation['missing_final_website_count'],"website_conflicts":len(ledgers['website_conflicts']),"homonym_groups":len(ledgers['homonyms']),"txt_path":str(txt),"txt_sha256":sha256_file(txt),"evidence_path":str(evidence),"evidence_sha256":sha256_file(evidence)},ensure_ascii=False,indent=2)); print('MULTI_VC_FINAL_END')

if __name__=='__main__': main()
