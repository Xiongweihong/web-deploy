from __future__ import annotations

import concurrent.futures
import io
import json
import re
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont, ImageOps

URL = 'https://www.samsungnext.com/ai-portfolio'
OUT = Path('artifact/logo-evidence')
OUT.mkdir(parents=True, exist_ok=True)
HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; samsungnext-logo-audit/1.0)'}

r = requests.get(URL, headers=HEADERS, timeout=60)
r.raise_for_status()
soup = BeautifulSoup(r.content, 'lxml')
rows = []
for a in soup.select('a.gallery-grid-image-link[href]'):
    href = a.get('href','')
    if not href.startswith('http'):
        continue
    host = urlparse(href).netloc.lower().removeprefix('www.')
    if 'samsung' in host or 'typeform' in host:
        continue
    img = a.find('img')
    src = img.get('data-image') or img.get('data-src') or img.get('src')
    rows.append({'order': len(rows)+1, 'href': href, 'host': host, 'image_url': src, 'alt': img.get('alt','')})


def dl(row):
    try:
        rr = requests.get(row['image_url'], headers=HEADERS, timeout=40)
        rr.raise_for_status()
        im = Image.open(io.BytesIO(rr.content)).convert('RGBA')
        return row, im, ''
    except Exception as e:
        return row, None, repr(e)

font = ImageFont.load_default(size=14)
small = ImageFont.load_default(size=11)
cell_w, cell_h = 320, 210
cols, per_sheet = 4, 24
results = []
with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
    for row, im, err in ex.map(dl, rows):
        results.append((row, im, err))

inventory = []
for row, im, err in results:
    rec = dict(row)
    rec['error'] = err
    if im is not None:
        p = OUT / f"{row['order']:02d}_{re.sub(r'[^a-z0-9.-]+','_',row['host'])}.png"
        bg = Image.new('RGB', im.size, 'white')
        if im.mode == 'RGBA':
            bg.paste(im, mask=im.getchannel('A'))
        else:
            bg.paste(im)
        bg.save(p)
        rec['file'] = str(p)
        rec['original_size'] = list(im.size)
    inventory.append(rec)

for start in range(0, len(results), per_sheet):
    subset = results[start:start+per_sheet]
    rows_n = (len(subset)+cols-1)//cols
    sheet = Image.new('RGB', (cols*cell_w, rows_n*cell_h), 'white')
    draw = ImageDraw.Draw(sheet)
    for idx, (row, im, err) in enumerate(subset):
        x = (idx % cols)*cell_w
        y = (idx // cols)*cell_h
        draw.rectangle((x,y,x+cell_w-1,y+cell_h-1), outline='gray')
        draw.text((x+8,y+6), f"{row['order']:02d}  {row['host']}", fill='black', font=font)
        if im is not None:
            bg = Image.new('RGBA', im.size, 'white')
            bg.alpha_composite(im)
            thumb = ImageOps.contain(bg.convert('RGB'), (cell_w-20, cell_h-55))
            tx = x+(cell_w-thumb.width)//2
            ty = y+32+(cell_h-55-thumb.height)//2
            sheet.paste(thumb,(tx,ty))
        else:
            draw.text((x+8,y+50), err[:100], fill='black', font=small)
    sheet.save(OUT / f"logo-sheet-{start//per_sheet+1:02d}.jpg", quality=92)

(OUT/'inventory.json').write_text(json.dumps(inventory,ensure_ascii=False,indent=2),encoding='utf-8')
print('LOGO_COUNT',len(rows),'DOWNLOAD_OK',sum(1 for _,im,_ in results if im is not None))
