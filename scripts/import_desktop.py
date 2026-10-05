"""Read-only import from the known Desktop Curio schema. Never imports credentials."""
import argparse,sqlite3,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from curio.config import ROOT,load
from curio.store import Store
from curio.price import money

def migrate(source,store,config):
    source=Path(source).resolve()
    if source==store.path.resolve():raise ValueError('源库和目标库不能相同')
    old=sqlite3.connect(source.as_uri()+'?mode=ro',uri=True);old.row_factory=sqlite3.Row
    counts={'imported':0,'unsupported':0,'reviews':0};known=store.known()
    try:
        for raw in old.execute('SELECT * FROM items'):
            row=dict(raw)
            if row.get('platform') not in ('ebay','kleinanzeigen'):
                counts['unsupported']+=1;continue
            if row['item_id'] in known:continue
            if row['platform']=='kleinanzeigen' and row.get('price')==0 and 'verschenken' not in (row.get('price_text') or '').lower():
                row['price']=None
            store.ingest(row,config)
            with store.connect() as c:
                c.execute('UPDATE items SET first_seen=?,last_seen=? WHERE item_id=?',
                    (row.get('first_seen'),row.get('last_seen'),row['item_id']))
            counts['imported']+=1
        if old.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='decisions'").fetchone():
            for raw in old.execute('SELECT * FROM decisions'):
                row=dict(raw)
                if row['item_id'] not in store.known() or row['decision'] not in ('want','probe','skip'):continue
                try:fair=money(row.get('fair_price'))
                except ValueError:fair=None
                reason=row.get('reason') or '旧版审核记录（原记录未填原因）'
                if fair is None and row.get('fair_price'):reason+='；原心理价：'+str(row['fair_price'])
                with store.connect() as c:
                    product=c.execute('SELECT * FROM items WHERE item_id=?',(row['item_id'],)).fetchone()
                    cur=c.execute('''INSERT OR IGNORE INTO cases(item_id,decision,reason,fair_price,currency,agent_decision,
                      agent_reason,listed_price,total_price,rule_version,decided_at,request_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                      (row['item_id'],row['decision'],reason,fair,product['currency'],row.get('agent_decision'),row.get('agent_reason'),
                       None,None,None,row.get('decided_at'),'desktop-import:'+row['item_id']))
                    counts['reviews']+=cur.rowcount
        store.set('desktop_import_note','桌面旧版只读导入；商品在售状态尚未重新核验，旧审核记录缺失的价格快照留空。')
        return counts
    finally:old.close()

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('source',type=Path);args=parser.parse_args()
    print(migrate(args.source,Store(ROOT/'data/curio.db'),load()))
