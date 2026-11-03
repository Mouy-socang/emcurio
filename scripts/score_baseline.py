"""为当前全部新货（拣选状态 new）回填规则基线分（CAP-4，幂等）。

已有且未过期的 AI 分保留；规则分与当前基线一致时不改写，所以第二次运行写入 0。不调用 AI。

用法：uv run python scripts/score_baseline.py [数据库路径]
运行前请先备份：sqlite3 data/curio.db ".backup data/backups/curio-before-scores-<UTC时间>.db"
"""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from curio.config import ROOT
from curio.store import Store,refresh_baseline

def main(argv=None):
    ap=argparse.ArgumentParser(description='为全部新货回填规则基线分')
    ap.add_argument('db',nargs='?',default=str(ROOT/'data/curio.db'))
    args=ap.parse_args(argv)
    if not Path(args.db).is_file():
        raise SystemExit('数据库不存在：'+args.db)
    store=Store(args.db)
    with store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        ids=[r[0] for r in c.execute("SELECT item_id FROM triage WHERE state='new'")]
        written=sum(refresh_baseline(c,iid) for iid in ids)
        sources=dict(c.execute("SELECT s.source,count(*) FROM scores s JOIN triage t ON t.item_id=s.item_id WHERE t.state='new' GROUP BY s.source").fetchall())
        missing=c.execute("SELECT count(*) FROM triage t LEFT JOIN scores s ON s.item_id=t.item_id WHERE t.state='new' AND s.item_id IS NULL").fetchone()[0]
    result=dict(new_items=len(ids),written=written,by_source=sources,missing=missing)
    print(json.dumps(result,ensure_ascii=False,indent=1))
    return result

if __name__=='__main__':main()
