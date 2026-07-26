import hashlib, json, re, shutil, time
from pathlib import Path
import requests
from bs4 import BeautifulSoup

URLS = {
  'portfolio':'https://fly.vc/portfolio',
  'ai':'https://fly.vc/category/ai',
  'developer-tools':'https://fly.vc/category/developer-tools',
  'industrial-automation':'https://fly.vc/category/industrial-automation',
  'techbio':'https://fly.vc/category/techbio',
}
OUT=Path('artifact')
UA='Mozilla/5.0 (compatible; flyvc-audit/1.0)'

def h(b): return hashlib.sha256(b).hexdigest()
def savej(p,x):
 p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(x,ensure_ascii=False,indent=2,sort_keys=True),encoding='utf-8')

def main():
 if OUT.exists(): shutil.rmtree(OUT)
 result={}
 for key,url in URLS.items():
  rows=[]
  for n in (1,2):
   r=requests.get(url,timeout=120,headers={'User-Agent':UA,'Accept':'text/html,*/*'})
   r.raise_for_status(); raw=r.content
   base=OUT/key/f'snapshot-{n:02d}'
   base.mkdir(parents=True,exist_ok=True)
   (base/'page.html').write_bytes(raw)
   soup=BeautifulSoup(raw,'lxml')
   anchors=[{'text':re.sub(r'\s+',' ',a.get_text(' ',strip=True)).strip(),'href':a.get('href'),'class':a.get('class')} for a in soup.find_all('a')]
   headings=[{'tag':x.name,'text':re.sub(r'\s+',' ',x.get_text(' ',strip=True)).strip(),'class':x.get('class')} for x in soup.find_all(re.compile('^h[1-6]$'))]
   scripts=[{'src':s.get('src'),'type':s.get('type'),'id':s.get('id'),'text_prefix':(s.string or s.get_text(' ',strip=True))[:2000]} for s in soup.find_all('script')]
   savej(base/'anchors.json',anchors);savej(base/'headings.json',headings);savej(base/'scripts.json',scripts)
   rows.append({'sha256':h(raw),'bytes':len(raw),'anchors':len(anchors),'headings':len(headings),'scripts':len(scripts)})
  result[key]=rows
 savej(OUT/'summary.json',result)
 print(json.dumps(result,indent=2))

if __name__=='__main__': main()
