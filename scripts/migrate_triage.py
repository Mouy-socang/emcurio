"""一次性迁移：把 cases / market_decisions 的人工决定落到拣选状态机（AD-26），幂等可重跑。

用法：uv run python scripts/migrate_triage.py [数据库路径]
运行前请先备份：sqlite3 data/curio.db ".backup data/backups/curio-<时间>.db"
"""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from curio.config import ROOT
from curio.store import Store
from curio import triage

def main(argv=None):
    ap=argparse.ArgumentParser(description='迁移拣选状态与货号')
    ap.add_argument('db',nargs='?',default=str(ROOT/'data/curio.db'))
    args=ap.parse_args(argv)
    store=Store(args.db)
    with store.connect() as c:
        added=triage.migrate(c)
    with store.connect() as c:
        report=dict(new_events=added['events'],new_codes=added['codes'],
            states=triage.counts(c),
            backlog=c.execute('SELECT count(*) FROM triage WHERE backlog=1').fetchone()[0],
            queue=c.execute("SELECT count(*) FROM triage WHERE state='new' AND backlog=0").fetchone()[0],
            items=c.execute('SELECT count(*) FROM items').fetchone()[0],
            triage_rows=c.execute('SELECT count(*) FROM triage').fetchone()[0],
            codes=c.execute('SELECT count(*) FROM item_codes').fetchone()[0],
            migrated_at=c.execute("SELECT v FROM kv WHERE k='triage_migrated_at'").fetchone()[0])
    print(json.dumps(report,ensure_ascii=False,indent=1))
    return report

if __name__=='__main__':main()
