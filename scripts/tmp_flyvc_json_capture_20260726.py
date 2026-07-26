import hashlib,json,shutil,time
from pathlib import Path
import requests

URLS={
 'all':'https://fly.vc/portfolio.json',
 'ai':'https://fly.vc/portfolio.json/category:ai',
 'developer-tools':'https://fly.vc/portfolio.json/category:developer-tools',
 'industrial-automation':'https://fly.vc/portfolio.json/category:industrial-automation',
 'techbio':'https://fly.vc/portfolio.json/category:techbio',
}
OUT=Path('artifact/json');UA='Mozilla/5.0 (compatible; flyvc-audit/1.1)'
def h(b):return hashlib.sha256(b).hexdigest()
def savej(p,x):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(x,ensure_ascii=False,indent=2,sort_keys=True),encoding='utf-8')

def main():
 if OUT.exists():shutil.rmtree(OUT)
 summary={}
 for snap in (1,2):
  for key,url in URLS.items():
   r=requests.get(url,timeout=120,headers={'User-Agent':UA,'Accept':'application/json'})
   r.raise_for_status();raw=r.content;data=r.json()
   p=OUT/f'snapshot-{snap:02d}'/f'{key}.json';p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(raw)
   savej(p.with_suffix('.json.meta.json'),{'url':r.url,'status':r.status_code,'date':r.headers.get('date'),'content_type':r.headers.get('content-type'),'bytes':len(raw),'sha256':h(raw),'fetched_at_epoch':time.time()})
   summary[f'{snap}:{key}']={'count':len(data) if isinstance(data,list) else None,'sha256':h(raw),'type':type(data).__name__,'sample':data[:2] if isinstance(data,list) else data}
 savej(OUT/'summary.json',summary);print(json.dumps(summary,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
