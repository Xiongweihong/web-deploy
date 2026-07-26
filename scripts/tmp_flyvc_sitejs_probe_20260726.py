import json,re
from pathlib import Path
import requests

URL='https://fly.vc/assets/js/site.js?v=11:42-26.07'
OUT=Path('artifact/sitejs');OUT.mkdir(parents=True,exist_ok=True)
r=requests.get(URL,timeout=120,headers={'User-Agent':'Mozilla/5.0','Accept':'*/*'})
r.raise_for_status();(OUT/'site.js').write_bytes(r.content)
s=r.text
hits=[]
for pat in ('getPortfolio','portfolio','fetch(','axios','XMLHttpRequest','/api/','json'):
 for m in re.finditer(re.escape(pat),s,re.I):
  hits.append({'pattern':pat,'offset':m.start(),'context':s[max(0,m.start()-500):m.start()+1500]})
(OUT/'hits.json').write_text(json.dumps(hits,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'status':r.status_code,'bytes':len(r.content),'hit_count':len(hits)},indent=2))
