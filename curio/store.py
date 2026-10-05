"""One SQLite for items, rules, screening, collection state and owner reviews."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from .price import prices, money
from .rules import evaluate
from screening.pipeline import process


def now():return datetime.now(timezone.utc).isoformat(timespec='seconds')

class Store:
    def __init__(self,path):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS items(
                item_id TEXT PRIMARY KEY, platform TEXT NOT NULL, title TEXT NOT NULL,
                description TEXT, price REAL, listed_price REAL, total_price REAL,currency TEXT,
                price_text TEXT,buying TEXT,url TEXT,image TEXT,created TEXT,ends TEXT,
                condition TEXT,country TEXT,first_seen TEXT,last_seen TEXT,checked_at TEXT,
                status TEXT DEFAULT 'active',gone_at TEXT,agent_decision TEXT,agent_reason TEXT,
                fair_price TEXT,rule_version INTEGER DEFAULT 1);
            CREATE TABLE IF NOT EXISTS price_history(item_id TEXT,price REAL,total_price REAL,seen_at TEXT);
            CREATE TABLE IF NOT EXISTS price_drops(item_id TEXT,old_price REAL,new_price REAL,pct REAL,seen_at TEXT);
            CREATE TABLE IF NOT EXISTS cases(
                id INTEGER PRIMARY KEY AUTOINCREMENT,item_id TEXT NOT NULL,decision TEXT NOT NULL,
                reason TEXT NOT NULL,fair_price REAL,currency TEXT,agent_decision TEXT,agent_reason TEXT,
                listed_price REAL,total_price REAL,rule_version INTEGER,decided_at TEXT,
                request_id TEXT NOT NULL UNIQUE);
            CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY,v TEXT);
            CREATE INDEX IF NOT EXISTS review_order ON items(status,agent_decision,created,first_seen);
            CREATE INDEX IF NOT EXISTS cases_item ON cases(item_id);
            ''')
    @contextmanager
    def connect(self):
        c=sqlite3.connect(self.path,timeout=30);c.row_factory=sqlite3.Row
        try:
            yield c;c.commit()
        except Exception:
            c.rollback();raise
        finally:c.close()
    def known(self):
        with self.connect() as c:return {r[0] for r in c.execute('SELECT item_id FROM items')}
    def get(self,key,default=None):
        with self.connect() as c:r=c.execute('SELECT v FROM kv WHERE k=?',(key,)).fetchone()
        return r[0] if r else default
    def set(self,key,value):
        with self.connect() as c:c.execute('INSERT OR REPLACE INTO kv VALUES(?,?)',(key,str(value)))
    def ingest(self,item,config):
        # Only explicit public product fields are persisted; no response blobs or credentials.
        fields=('item_id','platform','title','description','currency','price_text','buying','url','image','created','ends','condition','country')
        p={k:item.get(k,'') or '' for k in fields}
        if not p['item_id'] or p['platform'] not in ('ebay','kleinanzeigen'):raise ValueError('来源商品编号无效')
        listed,total=prices(item,config['auction_fee_rate']);p.update(price=listed,listed_price=listed,total_price=total)
        ts=now()
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('SELECT * FROM items WHERE item_id=?',(p['item_id'],)).fetchone()
            changed=old is None or old['listed_price']!=listed or old['currency']!=p['currency']
            if changed:
                result=evaluate(p,config)
                p.update(agent_decision=result['decision'],agent_reason=result['reason'],fair_price=result.get('fair',''))
                p['rule_version']=old['rule_version']+1 if old else 1
                process(c,p,result,config['daily_budget'])
                c.execute('INSERT INTO price_history VALUES(?,?,?,?)',(p['item_id'],listed,total,ts))
                if old and old['currency']==p['currency'] and old['listed_price'] and listed is not None and 0<=listed<old['listed_price'] and 'AUCTION' not in p['buying']:
                    pct=(old['listed_price']-listed)/old['listed_price']*100
                    if pct>=10:c.execute('INSERT INTO price_drops VALUES(?,?,?,?,?)',(p['item_id'],old['listed_price'],listed,pct,ts))
            p.update(last_seen=ts,checked_at=ts,status='active',gone_at=None)
            if old:
                # Preserve known creation date if a detail lookup omits it.
                if not p['created']:p['created']=old['created']
                c.execute('UPDATE items SET '+','.join(k+'=?' for k in p if k!='item_id')+' WHERE item_id=?',
                          [v for k,v in p.items() if k!='item_id']+[p['item_id']])
            else:
                p['first_seen']=ts
                c.execute('INSERT INTO items('+','.join(p)+') VALUES('+','.join('?' for _ in p)+')',list(p.values()))
        return 'new' if old is None else 'changed' if changed else 'unchanged'
    def mark_gone(self,iid):
        with self.connect() as c:c.execute("UPDATE items SET status='gone',gone_at=?,checked_at=? WHERE item_id=? AND status='active'",(now(),now(),iid))
    def archive(self):
        # Missing/withdrawn is never asserted to be sold. Archive in SQLite only.
        with self.connect() as c:c.execute("UPDATE items SET status='archived' WHERE status='gone' AND julianday(gone_at)<julianday('now','-3 days')")
    def review(self,iid,decision,reason,fair_price,request_id,version):
        if decision not in ('want','probe','skip'):raise ValueError('判断无效')
        if not isinstance(reason,str) or len(reason)>2000:raise ValueError('原因格式无效')
        reason=reason.strip()
        if decision=='skip' and not reason:raise ValueError('选择不要时必须填写原因')
        fair=money(fair_price)
        if not isinstance(request_id,str) or not 1<=len(request_id)<=100:raise ValueError('请求编号无效')
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            prev=c.execute('SELECT * FROM cases WHERE request_id=?',(request_id,)).fetchone()
            if prev:
                if (prev['item_id'],prev['decision'],prev['reason'],prev['fair_price'])!=(iid,decision,reason,fair):raise ValueError('重复请求内容不同')
                return prev['id']
            row=c.execute('SELECT * FROM items WHERE item_id=?',(iid,)).fetchone()
            if not row:raise ValueError('商品不存在')
            if type(version) is not int or row['rule_version']!=version:raise ValueError('商品价格或规则已更新，请刷新后审核')
            cur=c.execute('''INSERT INTO cases(item_id,decision,reason,fair_price,currency,agent_decision,agent_reason,
                listed_price,total_price,rule_version,decided_at,request_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                (iid,decision,reason,fair,row['currency'],row['agent_decision'],row['agent_reason'],row['listed_price'],row['total_price'],version,now(),request_id))
            return cur.lastrowid
    def page(self,tab='pending',page=1,size=30):
        if tab not in ('pending','excluded','reviewed'):raise ValueError('标签无效')
        if page<1 or not 1<=size<=100:raise ValueError('分页参数无效')
        clauses={
          'pending':"i.status='active' AND i.agent_decision IN ('want','probe') AND NOT EXISTS(SELECT 1 FROM cases c WHERE c.item_id=i.item_id)",
          'excluded':"i.agent_decision='skip'",
          'reviewed':"EXISTS(SELECT 1 FROM cases c WHERE c.item_id=i.item_id)"}
        with self.connect() as c:
            count=c.execute('SELECT COUNT(*) FROM items i WHERE '+clauses[tab]).fetchone()[0]
            rows=c.execute('''SELECT i.*,(SELECT decision FROM cases c WHERE c.item_id=i.item_id ORDER BY id DESC LIMIT 1) owner_decision
                FROM items i WHERE '''+clauses[tab]+" ORDER BY COALESCE(NULLIF(created,''),first_seen) DESC,item_id LIMIT ? OFFSET ?",(size,(page-1)*size)).fetchall()
        return dict(items=[dict(r) for r in rows],total=count,page=page,size=size)
