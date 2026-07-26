import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor,as_completed
from pathlib import Path
from urllib.parse import urlparse
import requests

SRC=Path('artifact/json/snapshot-02/all.json');OUT=Path('artifact/website-checks.json')
UA='Mozilla/5.0 (compatible; flyvc-audit/1.2)'
def root(u):
 p=urlparse(u);h=p.netloc.lower().removeprefix('www.');return f'https://{h}' if h else ''
def check(row):
 u=str(row.get('link') or '').strip();name=row.get('title')
 if not u:return {'name':name,'source_url':'','status':None,'final_url':'','final_root':'','history':[],'error':'missing_source_link'}
 try:
  r=requests.get(u,timeout=25,allow_redirects=True,stream=True,headers={'User-Agent':UA,'Accept':'text/html,application/xhtml+xml,*/*;q=0.8'})
  out={'name':name,'source_url':u,'source_root':root(u),'status':r.status_code,'final_url':r.url,'final_root':root(r.url),'history':[{'status':x.status_code,'url':x.url,'location':x.headers.get('location')} for x in r.history],'content_type':r.headers.get('content-type'),'error':''};r.close();return out
 except Exception as e:return {'name':name,'source_url':u,'source_root':root(u),'status':None,'final_url':'','final_root':'','history':[],'error':repr(e)}

def main():
 rows=json.load(open(SRC));results=[]
 with ThreadPoolExecutor(max_workers=10) as ex:
  fs={ex.submit(check,x):x for x in rows}
  for f in as_completed(fs):results.append(f.result())
 results.sort(key=lambda x:x['name'].casefold());OUT.parent.mkdir(parents=True,exist_ok=True);OUT.write_text(json.dumps(results,ensure_ascii=False,indent=2,sort_keys=True),encoding='utf-8')
 summary={'count':len(results),'status_counts':dict(Counter(str(x['status']) if x['status'] is not None else 'ERROR' for x in results)),'redirected_roots':[x for x in results if x.get('source_root') and x.get('final_root') and x['source_root']!=x['final_root']],'errors':[x for x in results if x.get('error')]}
 Path('artifact/website-checks-summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2,sort_keys=True),encoding='utf-8');print(json.dumps({'count':summary['count'],'status_counts':summary['status_counts'],'redirected_count':len(summary['redirected_roots']),'error_count':len(summary['errors'])},indent=2))
if __name__=='__main__':main()
