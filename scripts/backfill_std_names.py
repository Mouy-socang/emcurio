"""一次性回填：为全部 items 生成候选标准名称（CAP-16），写入 std_names，幂等可重跑。

以 item_id 为主键覆盖；结果与库里已有的一致时不改写（生成时间也保持不变）。
只读 EM 西洋瓷器知识库（config/settings.yaml 的 em_kb_path），不改判断结论、拣选状态与货号。

用法：uv run python scripts/backfill_std_names.py [数据库路径]
运行前请先备份：sqlite3 data/curio.db ".backup data/backups/curio-<时间>.db"
"""
import argparse,json,sys
from collections import Counter
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from curio.config import ROOT,load
from curio.store import Store,write_std_name,now
from curio.rules import standard_name

FIELDS=('std_name','std_key','status','brand','pattern_id','form_id','function_id','era','seller_grade','kb_status','kb_fingerprint')

def main(argv=None):
    ap=argparse.ArgumentParser(description='回填标准名称')
    ap.add_argument('db',nargs='?',default=str(ROOT/'data/curio.db'))
    args=ap.parse_args(argv)
    if not Path(args.db).is_file():
        raise SystemExit('数据库不存在：'+args.db)
    config=load()
    store=Store(args.db)
    written=unchanged=0
    ts=now()
    # 先在写锁外算好（避免长时间占用写锁挡住采集），再在一个短事务里写入。
    with store.connect() as c:
        items=c.execute('SELECT item_id,title,description FROM items ORDER BY item_id').fetchall()
    results=[(row['item_id'],standard_name(dict(row),config)) for row in items]
    with store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        old={r['item_id']:tuple(r[f] for f in FIELDS) for r in c.execute('SELECT * FROM std_names')}
        for iid,r in results:
            if old.get(iid)==tuple(r[f] for f in FIELDS):
                unchanged+=1;continue
            write_std_name(c,iid,r,ts);written+=1
    with store.connect() as c:
        items=c.execute('SELECT count(*) FROM items').fetchone()[0]
        rows=c.execute('SELECT count(*) FROM std_names').fetchone()[0]
        status=Counter(dict(c.execute('SELECT status,count(*) FROM std_names GROUP BY status').fetchall()))
        brands={}
        for brand,st,n in c.execute("SELECT COALESCE(brand,'（无品牌）'),status,count(*) FROM std_names GROUP BY 1,2"):
            brands.setdefault(brand,dict(named=0,insufficient=0))[st]=n
        meissen=brands.get('Meissen',dict(named=0,insufficient=0))
        total_m=meissen['named']+meissen['insufficient']
        kb=c.execute('SELECT kb_status,kb_fingerprint,count(*) FROM std_names GROUP BY 1,2').fetchall()
        groups=c.execute("SELECT count(DISTINCT std_key) FROM std_names WHERE status='named'").fetchone()[0]
    report=dict(items=items,std_names=rows,written=written,unchanged=unchanged,
        named=status['named'],insufficient=status['insufficient'],named_groups=groups,
        meissen_named=f"{meissen['named']}/{total_m}（{meissen['named']/total_m:.1%}）" if total_m else '0/0',
        brands=dict(sorted(brands.items(),key=lambda kv:-(kv[1]['named']+kv[1]['insufficient']))),
        kb=[dict(kb_status=a,kb_fingerprint=b,rows=n) for a,b,n in kb])
    print(json.dumps(report,ensure_ascii=False,indent=1))
    return report

if __name__=='__main__':main()
