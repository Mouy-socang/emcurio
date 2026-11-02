"""Evidence-backed local archive candidates; no external model calls."""
import json
from .store import now
FIELDS=('品牌','年代','功能','器型','图案','是否带金边','品相','数量','价格')
def init(store):
 with store.connect() as c:c.executescript('''CREATE TABLE IF NOT EXISTS archive_classifications(id INTEGER PRIMARY KEY,batch_id TEXT,snapshot_id INTEGER,item_id TEXT,created_at TEXT,data TEXT,UNIQUE(batch_id,snapshot_id));
 CREATE TABLE IF NOT EXISTS archive_classification_updates(id INTEGER PRIMARY KEY,classification_id INTEGER,created_at TEXT,data TEXT);
 CREATE TABLE IF NOT EXISTS archive_classification_reviews(id INTEGER PRIMARY KEY,classification_id INTEGER,created_at TEXT,fields TEXT,note TEXT);''')
def page(store):
 with store.connect() as c:
  rows=c.execute('SELECT a.*,s.data snapshot,(SELECT data FROM archive_classification_updates u WHERE u.classification_id=a.id ORDER BY id DESC LIMIT 1) update_data,(SELECT fields FROM archive_classification_reviews r WHERE r.classification_id=a.id ORDER BY id DESC LIMIT 1) reviewed_fields FROM archive_classifications a JOIN product_snapshots s ON s.id=a.snapshot_id ORDER BY a.id LIMIT 100').fetchall()
 results=[]
 for r in rows:
  data=json.loads(r['data'])
  if r['update_data']:
   update=json.loads(r['update_data'])
   data['fields'].update(update['fields'])
   data['classification_axes']=update['classification_axes']
   data['classification_rule']=update['classification_rule']
   data['knowledge_references']=data.get('knowledge_references',[])+update['knowledge_references']
  results.append(dict(id=r['id'],data=data,snapshot=json.loads(r['snapshot']),reviewed_fields=json.loads(r['reviewed_fields']) if r['reviewed_fields'] else None))
 return {'results':results}

def review(store,record_id,fields,note):
 if type(record_id) is not int or not isinstance(fields,dict) or set(fields)!=set(FIELDS) or any(not isinstance(v,str) or len(v)>4000 for v in fields.values()) or not isinstance(note,str) or len(note)>2000:raise ValueError('审核字段格式无效')
 with store.connect() as c:
  if not c.execute('SELECT 1 FROM archive_classifications WHERE id=?',(record_id,)).fetchone():raise ValueError('资料不存在')
  # Append-only user review, original candidate and source evidence stay immutable.
  rid=c.execute('INSERT INTO archive_classification_reviews(classification_id,created_at,fields,note) VALUES(?,?,?,?)',(record_id,now(),json.dumps(fields,ensure_ascii=False),note)).lastrowid
 return rid
