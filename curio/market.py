"""Market catalog backed by the production Curio database."""
import json,sqlite3,argparse,secrets
from datetime import datetime,timezone
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import urlparse,parse_qs
ROOT=Path(__file__).resolve().parents[1]
DB=ROOT/'data/curio.db'
from .product_v2 import FORMS
from . import triage
TYPE_LABELS={code:label for code,family,label,pattern in FORMS}
TYPE_LABELS.update(cup_set='杯碟／杯组',cup='单杯',saucer='单碟／杯托',mixed_set='混合套组')
LABELS={f['field']:f['label'] for f in json.loads((ROOT/'docs/product-fields-v2.json').read_text())['fields']}
def init(store):
 global DB
 DB=store.path.resolve()
 with store.connect() as c:
  c.executescript("""
  CREATE TABLE IF NOT EXISTS pattern_trials(item_id TEXT PRIMARY KEY,result_json TEXT,created_at TEXT);
  CREATE TABLE IF NOT EXISTS market_decisions(platform TEXT,item_id TEXT,irrelevant INTEGER NOT NULL,reason TEXT,updated_at TEXT,PRIMARY KEY(platform,item_id));
  CREATE TABLE IF NOT EXISTS market_events(id INTEGER PRIMARY KEY,platform TEXT,item_id TEXT,irrelevant INTEGER,reason TEXT,created_at TEXT,legacy_id INTEGER UNIQUE);
  CREATE TABLE IF NOT EXISTS market_analysis(item_id TEXT PRIMARY KEY,title TEXT,description TEXT,candidate_json TEXT);
  """)
  columns=[r[1] for r in c.execute('PRAGMA table_info(items)')]
  raw='json_object('+','.join("'"+x+"',i."+x for x in columns)+')'
  c.execute('DROP VIEW IF EXISTS market_items')
  c.execute('CREATE VIEW market_items AS SELECT i.platform,i.item_id,i.title,i.url,i.status,i.checked_at,'+raw+' raw_json,a.candidate_json FROM items i JOIN market_analysis a ON a.item_id=i.item_id')
 refresh()
def refresh():
 from .product_v2 import extract
 with connect() as c:
  rows=c.execute("SELECT i.* FROM items i LEFT JOIN market_analysis a ON a.item_id=i.item_id WHERE a.item_id IS NULL OR i.title IS NOT a.title OR coalesce(i.description,'') IS NOT coalesce(a.description,'')").fetchall()
  for r in rows:
   a=extract(dict(r))
   c.execute('INSERT OR REPLACE INTO market_analysis VALUES(?,?,?,?)',(r['item_id'],r['title'],r['description'],json.dumps(a,ensure_ascii=False)))
def migrate_legacy(path):
 if not path.exists():return {'events':0,'decisions':0}
 with sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True) as old,connect() as c:
  before=c.total_changes
  for r in old.execute('SELECT id,platform,item_id,irrelevant,reason,created_at FROM events ORDER BY id'):
   c.execute('INSERT OR IGNORE INTO market_events(platform,item_id,irrelevant,reason,created_at,legacy_id) VALUES(?,?,?,?,?,?)',(*r[1:],r[0]))
  events=c.total_changes-before
  for r in old.execute('SELECT platform,item_id,irrelevant,reason,updated_at FROM decisions'):
   if not c.execute('SELECT 1 FROM items WHERE platform=? AND item_id=?',r[:2]).fetchone():raise ValueError('迁移商品缺失')
   c.execute('INSERT INTO market_decisions VALUES(?,?,?,?,?) ON CONFLICT(platform,item_id) DO UPDATE SET irrelevant=excluded.irrelevant,reason=excluded.reason,updated_at=excluded.updated_at WHERE excluded.updated_at>market_decisions.updated_at',r)
  decisions=c.execute('SELECT count(*) FROM market_decisions').fetchone()[0]
 return dict(events=events,decisions=decisions)
def set_decision(platform,item_id,irrelevant,reason):
 if type(irrelevant) is not bool or not isinstance(reason,str) or len(reason)>1000:raise ValueError('参数无效')
 with connect() as c:
  if not c.execute('select 1 from market_items where platform=? and item_id=?',(platform,item_id)).fetchone():raise ValueError('商品不存在')
 stamp=datetime.now(timezone.utc).isoformat()
 with connect() as c:
  # AD-26：无关 → 去掉（来源 market）；取消无关 → 只撤销来自市场页的那次去掉，没有则不动。
  # 状态已变化而无法撤销时抛中文错误。只在 market_items、不在 items 的货跳过拣选。
  c.execute('BEGIN IMMEDIATE')
  if c.execute('select 1 from items where item_id=?',(item_id,)).fetchone():
   if irrelevant:triage.remove(c,item_id,reason or '无关',at=stamp,source='market')
   else:triage.undo_market(c,item_id)
  c.execute('insert into market_decisions values(?,?,?,?,?) on conflict(platform,item_id) do update set irrelevant=excluded.irrelevant,reason=excluded.reason,updated_at=excluded.updated_at',(platform,item_id,int(irrelevant),reason,stamp))
  c.execute('insert into market_events(platform,item_id,irrelevant,reason,created_at) values(?,?,?,?,?)',(platform,item_id,int(irrelevant),reason,stamp))
def connect():
 c=sqlite3.connect(DB,timeout=30);c.row_factory=sqlite3.Row;return c
BASE="url NOT LIKE '%/demo/%' AND item_id NOT LIKE '%test%' AND url NOT LIKE '%example.%'"
def filters(q):
 terms=[BASE];args=[]
 effective="COALESCE((SELECT CASE WHEN julianday(d.updated_at)>=COALESCE((SELECT julianday(decided_at) FROM cases WHERE item_id=market_items.item_id ORDER BY id DESC LIMIT 1),0) THEN d.irrelevant END FROM market_decisions d WHERE d.platform=market_items.platform AND d.item_id=market_items.item_id),COALESCE((SELECT decision FROM cases WHERE item_id=market_items.item_id ORDER BY id DESC LIMIT 1)='irrelevant',0))"
 terms.append(effective+'=1' if q.get('scope',[''])[0]=='irrelevant' else effective+'=0')
 if q.get('scope',['candidate'])[0]=='candidate':terms.append("coalesce(json_extract(raw_json,'$.agent_decision'),'') in ('want','probe')")
 for key,path in [('maker','maker'),('type','object_type')]:
  if key=='type' and q.get(key,[''])[0]=='__unknown__':
   terms.append("json_extract(candidate_json,'$.candidate_product.object_type') IS NULL");continue
  if q.get(key,[''])[0]:terms.append("json_extract(candidate_json,'$.candidate_product."+path+"')=?");args.append(q[key][0])
 if q.get('scope',[''])[0]=='pattern_trial':terms.append('EXISTS(SELECT 1 FROM pattern_trials pt WHERE pt.item_id=market_items.item_id)')
 status=q.get('status',[''])[0]
 if status=='active':terms.append("status='active'")
 if status=='delisted':terms.append("status in ('gone','archived')")
 if q.get('q',[''])[0]:terms.append("(title LIKE ? OR json_extract(raw_json,'$.description') LIKE ?)");args.extend(['%'+q['q'][0]+'%']*2)
 if q.get('platform',[''])[0]:terms.append('platform=?');args.append(q['platform'][0])
 return ' AND '.join(terms),args
def query(q):
 refresh()
 where,args=filters(q);page=max(1,int(q.get('page',['1'])[0]));size=24
 with connect() as c:
  total=c.execute('select count(*) from market_items where '+where,args).fetchone()[0]
  rows=c.execute('select * from market_items where '+where+" order by json_extract(raw_json,'$.first_seen') desc,item_id limit ? offset ?",args+[size,(page-1)*size]).fetchall()
  items=[]
  for r in rows:
   raw=json.loads(r['raw_json']);a=json.loads(r['candidate_json']);p=a.get('candidate_product',{})
   items.append(dict(id=r['item_id'],platform=r['platform'],title=r['title'],status=r['status'],price=raw.get('listed_price'),currency=raw.get('currency'),buying=raw.get('buying'),image=raw.get('image'),checked=r['checked_at'],maker=p.get('maker'),type=a.get('object_label_zh'),preview=a.get('title_preview_zh')))
  types=[dict(label=TYPE_LABELS.get(r[0],r[0] or '器型待识别'),count=r[1],code=r[0] or '__unknown__') for r in c.execute("select json_extract(candidate_json,'$.candidate_product.object_type'),count(*) from market_items where "+where+' group by 1 order by 2 desc',args)]
  counts=dict(c.execute('select status,count(*) from market_items where '+where+' group by status',args).fetchall())
  known=c.execute("select count(*) from market_items where "+where+" AND json_extract(candidate_json,'$.candidate_product.object_type') IS NOT NULL",args).fetchone()[0]
  prices=[r[0] for r in c.execute("select json_extract(raw_json,'$.listed_price') from market_items where "+where+" AND json_extract(raw_json,'$.currency')='EUR' AND json_extract(raw_json,'$.listed_price')>0 AND coalesce(json_extract(raw_json,'$.buying'),'') NOT LIKE '%AUCTION%' order by 1",args)]
  n=len(prices);median=(prices[(n-1)//2]+prices[n//2])/2 if n else None
  bins=[dict(label=k,count=sum(lo<=v<hi for v in prices)) for k,lo,hi in [('€0–50',0,50),('€50–100',50,100),('€100–250',100,250),('€250–500',250,500),('€500+',500,float('inf'))]]
 return dict(total=total,page=page,pages=max(1,(total+size-1)//size),items=items,types=types,counts=counts,known=known,median=median,price_count=n,bins=bins)

def options():
 refresh()
 with connect() as c:
  opts={k:[r[0] for r in c.execute("select distinct json_extract(candidate_json,'$.candidate_product."+k+"') from market_items where "+BASE+" and json_extract(candidate_json,'$.candidate_product."+k+"') is not null order by 1")] for k in ('maker','object_type')}
  opts['type_labels']=TYPE_LABELS
  opts['object_type'].append('__unknown__');opts['type_labels']=dict(TYPE_LABELS,__unknown__='器型待识别')
 return opts
def detail(q):
 refresh()
 with connect() as c:
  r=c.execute('select * from market_items where '+BASE+' and platform=? and item_id=?',(q.get('platform',[''])[0],q.get('id',[''])[0])).fetchone()
  if not r:raise ValueError('商品不存在')
  decision=c.execute('select irrelevant,reason,updated_at from market_decisions where platform=? and item_id=?',(r['platform'],r['item_id'])).fetchone()
  trial=c.execute('SELECT result_json FROM pattern_trials WHERE item_id=?',(r['item_id'],)).fetchone()
  owner=c.execute('select decision,reason,decided_at from cases where item_id=? order by id desc limit 1',(r['item_id'],)).fetchone()
 return dict(raw=json.loads(r['raw_json']),analysis=json.loads(r['candidate_json']),labels=LABELS,specific_pattern=json.loads(trial[0]) if trial else None,irrelevant=bool(decision[0] if decision and (not owner or decision[2]>=owner[2]) else owner and owner[0]=='irrelevant'),manual_reason=decision[1] if decision else owner[1] if owner and owner[0]=='irrelevant' else '')
