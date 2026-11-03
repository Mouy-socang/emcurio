"""全量重判（故事 5，CAP-3）：用当前规则重判全部货、刷新标准名称，幂等可重跑。

与 Store.block 共用同一重判循环（curio.store.rejudge）：判断结论或理由变化的货更新 agent_decision /
agent_reason、rule_version+1；拣选状态只对未经人工决定的货在 新货 / 自动移出 间移动（AD-26），
人工决定、cases、market_decisions 不变。不调用 AI。

用法：uv run python scripts/rejudge.py [数据库路径]
运行前请先备份：sqlite3 data/curio.db ".backup data/backups/curio-before-rejudge-<UTC时间>.db"
"""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from curio.config import ROOT,load
from curio.store import Store
from curio import triage

def main(argv=None):
    ap=argparse.ArgumentParser(description='全量重判并刷新标准名称')
    ap.add_argument('db',nargs='?',default=str(ROOT/'data/curio.db'))
    args=ap.parse_args(argv)
    if not Path(args.db).is_file():
        raise SystemExit('数据库不存在：'+args.db)
    store=Store(args.db)
    result=store.rejudge_all(load())
    with store.connect() as c:
        result['states']=triage.counts(c)
        result['overpaint']=c.execute('SELECT count(*) FROM std_names WHERE overpaint=1').fetchone()[0]
    print(json.dumps(result,ensure_ascii=False,indent=1))
    return result

if __name__=='__main__':main()
