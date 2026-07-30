import json
import requests

urls = [
    "https://r.jina.ai/http://www.indexventures.com/companies/1stdibs/",
    "https://r.jina.ai/http://www.indexventures.com/companies/abacusai/",
]
rows=[]
for url in urls:
    r=requests.get(url,headers={"User-Agent":"Mozilla/5.0","Accept":"text/plain"},timeout=120)
    rows.append({"url":url,"status":r.status_code,"bytes":len(r.content),"text":r.text[:6000]})
print("JINA_INDEX_TEST_START")
print(json.dumps(rows,ensure_ascii=False,indent=2))
print("JINA_INDEX_TEST_END")
if any(row["status"]!=200 or "Website" not in row["text"] for row in rows):
    raise SystemExit(2)
