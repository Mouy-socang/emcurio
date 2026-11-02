"""Freeze today's local additions and retrieve listing evidence, without changing owner decisions."""
import argparse
import concurrent.futures
import datetime as dt
import html
import json
from pathlib import Path
import re
import sqlite3
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from curio.curio_monitor import EbayAPI, ka_curl, blocked

EXCLUDE = [
 (r'\b(?:AK|Postkarten?|Ansichtskarte|Steindruck-AK|Künstler-AK)\b|Fliegeraufnahme|Luftbild|^Meissen Elbe Sachsen|\b(?:ngl|gl1902)\b|01662 O8250|Lithographie Meissen|^Foto - Schiff|Historische Ansicht.*Radierung', '地名相关明信片或风景印刷品'),
 (r'\b(?:FDC|BRIEF|SST|SSt|EXHIBITION.COVER|ERINNERUNGSBLATT|Schwarzdruck|Reko.Luftpost|Landpost.Stempel)\b|DDR 1971 MEISSEN SCHACH|\b\d+ I{1,2} Meissener Porzellan \d+ Pf', '邮票、邮封或集邮品'),
 (r'\b(?:m/w/d|Postbote|Reinigungskraft|NEBENJOB|Security|Praxisanleiter|Maschinenbediener)\b', '招聘信息'),
 (r'Wohnung|Mietwohnung|Wohnwagen|Waldflächen|Laubmischwald|Grundstück|grundstück|Zweifamilienhaus|Parkplatz|verpassen Sie diese Gelegenheit', '房产、土地或车辆'),
 (r'\b(?:MEISSER|Ballenzange|BALLENSPIES|DUNGGABEL|Prismawalze)\b', '农业设备'),
 (r'Stern.Combo|\bAmiga\b|Taschenbuch|\bBuch\b|Sonderheft|Musterbuch|Weigelt|Das Phantom', '书籍、图录或音像（非本轮瓷器候选）'),
 (r'Leere Kartonage|Adidas|Schützenscheibe|Schießscheibe|Blech Türschild|\bProgramme?\b|Nachbarschaftshilfe|Nachmittagstreff|Saxony meets Taiwan|Kohlebergwerk|3 Reichsmark|K\. Holze', '其他非目标商品或活动'),
 (r'\bSUCHE\b|\bAnkauf\b', '求购广告'),
]

def off_topic(title):
    return next((reason for pattern, reason in EXCLUDE if re.search(pattern, title, re.I)), None)

def clean(raw):
    raw = re.sub(r'<(?:script|style)\b[^>]*>[\s\S]*?</(?:script|style)>', ' ', raw or '', flags=re.I)
    return re.sub(r'\s+', ' ', html.unescape(re.sub('<[^>]+>', ' ', raw))).strip()

def main():
    p=argparse.ArgumentParser();p.add_argument('--date',required=True);p.add_argument('--fetch',action='store_true');a=p.parse_args()
    out=ROOT/'data'/('daily_v2_'+a.date.replace('-',''));out.mkdir(parents=True,exist_ok=True)
    snapshot=out/'source_snapshot.json'
    if not snapshot.exists():
        start=dt.datetime.fromisoformat(a.date).replace(tzinfo=ZoneInfo('Europe/Berlin'))
        end=start+dt.timedelta(days=1)
        live=sqlite3.connect(f'file:{ROOT/"data/curio.db"}?mode=ro',uri=True);c=sqlite3.connect(':memory:');live.backup(c);live.close();c.row_factory=sqlite3.Row
        rows=[dict(r) for r in c.execute('SELECT * FROM items') if start <= dt.datetime.fromisoformat(r['first_seen']) < end]
        manual={r['item_id']:dict(r) for r in c.execute('SELECT * FROM cases ORDER BY id')}
        runs=[dict(r) for r in c.execute('SELECT * FROM collection_runs ORDER BY id DESC LIMIT 3')]
        snapshot.write_text(json.dumps(dict(date=a.date,timezone='Europe/Berlin',captured_at=dt.datetime.now(dt.timezone.utc).isoformat(),items=rows,manual=manual,runs=runs),ensure_ascii=False,indent=2))
    d=json.loads(snapshot.read_text());print('Snapshot:',len(d['items']),flush=True)
    if not a.fetch:return
    cache=out/'details';cache.mkdir(exist_ok=True)
    api=EbayAPI(ROOT);api._authorize()
    def fetch(i):
        path=cache/(re.sub(r'[^a-zA-Z0-9_-]','_',i['item_id'])+'.json')
        if path.exists():return 'cached'
        ev=dict(item_id=i['item_id'],retrieved_at=dt.datetime.now(dt.timezone.utc).isoformat(),url=i['url'])
        try:
            if i['platform']=='ebay':
                raw=api.detail(i['item_id'])
                if raw is None:ev['status']='gone'
                else:
                    ev.update(status='ok',title=raw.get('title'),description=clean(raw.get('description') or raw.get('shortDescription')),aspects=raw.get('localizedAspects',[]),condition=raw.get('condition'),availability=raw.get('estimatedAvailabilities',[]),price=raw.get('currentBidPrice') or raw.get('price'),buying=raw.get('buyingOptions',[]),images=[(raw.get('image') or {}).get('imageUrl')]+[x.get('imageUrl') for x in raw.get('additionalImages',[])])
            else:
                status,raw,_,err=ka_curl(i['url'])
                if status in (404,410) or re.search('Anzeige ist nicht mehr verfügbar|Anzeige wurde gelöscht',raw):ev['status']='gone'
                elif err or status!=200 or blocked(raw):ev.update(status='unavailable',http_status=status)
                else:
                    m=re.search(r'id="viewad-description-text"[^>]*>([\s\S]*?)</(?:p|div)>',raw)
                    ev.update(status='ok' if m else 'description_missing',description=clean(m.group(1)) if m else '',aspects=[],images=[i['image']])
        except Exception:ev['status']='fetch_error'
        path.write_text(json.dumps(ev,ensure_ascii=False,indent=2));return ev['status']
    candidates=[i for i in d['items'] if not off_topic(i['title'])]
    counts={}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for n,status in enumerate(pool.map(fetch,candidates),1):
            counts[status]=counts.get(status,0)+1
            if n%25==0:print(n,'/',len(candidates),counts,flush=True)
    print(json.dumps(counts),flush=True)

if __name__=='__main__':main()
