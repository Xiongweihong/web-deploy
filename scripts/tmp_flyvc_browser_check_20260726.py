import json,re,time
from pathlib import Path
from playwright.sync_api import sync_playwright

URLS={'all':'https://fly.vc/portfolio','ai':'https://fly.vc/category/ai','developer-tools':'https://fly.vc/category/developer-tools','industrial-automation':'https://fly.vc/category/industrial-automation','techbio':'https://fly.vc/category/techbio'}
OUT=Path('artifact/browser');OUT.mkdir(parents=True,exist_ok=True)
def savej(p,x):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(x,ensure_ascii=False,indent=2,sort_keys=True),encoding='utf-8')

def main():
 result={}
 with sync_playwright() as pw:
  browser=pw.chromium.launch(headless=True)
  ctx=browser.new_context(viewport={'width':1440,'height':1000},locale='en-US')
  for key,url in URLS.items():
   page=ctx.new_page();page.goto(url,wait_until='domcontentloaded',timeout=120000)
   page.wait_for_function("document.querySelectorAll('li.portfolio_item').length > 0",timeout=120000)
   page.wait_for_timeout(3000);page.evaluate('window.scrollTo(0,document.documentElement.scrollHeight)');page.wait_for_timeout(1500)
   cards=page.eval_on_selector_all('li.portfolio_item',"""els=>els.map((el,i)=>{const a=el.querySelector('a');const h=el.querySelector('h3');const p=el.querySelector('p');const img=el.querySelector('img');const r=el.getBoundingClientRect();return{position:i+1,name:(h?.innerText||'').trim(),description:(p?.innerText||'').trim(),href:a?.href||'',image_alt:img?.alt||'',visible:r.width>0&&r.height>0}})""")
   base=OUT/key;base.mkdir(parents=True,exist_ok=True)
   (base/'rendered.html').write_text(page.content(),encoding='utf-8');savej(base/'cards.json',cards)
   page.screenshot(path=str(base/'full-page.png'),full_page=True)
   result[key]={'count':len(cards),'visible_count':sum(bool(x['visible']) for x in cards),'names':[x['name'] for x in cards]}
   page.close()
  browser.close()
 savej(OUT/'summary.json',result);print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
