"""CAP-4 / FR-8 验收复算：按日比较"决定前分数最高 20 件的留下率"与"当天全部已决定货的平均留下率"。

数据来自未撤销的人工"留下 / 去掉"拣选事件（含决定当时的快照分数 triage_events.score）；
同一件货同一天多次决定只算最后一次；日期按柏林时间。只读，不改数据库。

用法：uv run python scripts/top20_keep_rate.py [数据库路径]
"""
import argparse,sqlite3,sys
from datetime import datetime,timezone
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from curio.config import ROOT
from curio.schedule import BERLIN

TOP=20

def _day(at):
    try:
        t=datetime.fromisoformat(at)
    except (TypeError,ValueError):
        return None
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).astimezone(BERLIN).date().isoformat()

def decisions(c):
    """{日期: {item_id: (留下?, 快照分数)}}：未撤销的人工留下/去掉，每天每件取最后一次。"""
    days={}
    for r in c.execute("""SELECT item_id,at,new_state,score FROM triage_events
        WHERE actor='human' AND undone_by IS NULL AND action!='undo' AND new_state IN ('kept','removed') ORDER BY id"""):
        day=_day(r[1])
        if day:days.setdefault(day,{})[r[0]]=(r[2]=='kept',r[3])
    return days

def report(c):
    """返回 dict(days=[...], total={...})。两个留下率都只在有快照分数的决定里算（同一口径）：
    每天：decided（全部决定）、unscored（无分，不参与比较）、scored、kept（有分里留下的）、avg_rate、top_n、top_kept、top_rate。
    分数相同时按 item_id 排序，结果可复现。"""
    rows=[];tot=dict(decided=0,unscored=0,scored=0,kept=0,top_n=0,top_kept=0)
    for day,items in sorted(decisions(c).items()):
        scored=sorted(((iid,v) for iid,v in items.items() if v[1] is not None),key=lambda x:(-x[1][1],x[0]))
        top=scored[:TOP]
        row=dict(day=day,decided=len(items),unscored=len(items)-len(scored),scored=len(scored),
                 kept=sum(v[0] for _,v in scored),top_n=len(top),top_kept=sum(v[0] for _,v in top))
        row['avg_rate']=row['kept']/row['scored'] if scored else None
        row['top_rate']=row['top_kept']/row['top_n'] if top else None
        rows.append(row)
        for k in tot:tot[k]+=row[k]
    tot['avg_rate']=tot['kept']/tot['scored'] if tot['scored'] else None
    tot['top_rate']=tot['top_kept']/tot['top_n'] if tot['top_n'] else None
    return dict(days=rows,total=tot)

def _pct(x):return '—' if x is None else '%.1f%%'%(x*100)

def main(argv=None):
    ap=argparse.ArgumentParser(description='复算分数前 20 的留下率')
    ap.add_argument('db',nargs='?',default=str(ROOT/'data/curio.db'))
    args=ap.parse_args(argv)
    if not Path(args.db).is_file():
        raise SystemExit('数据库不存在：'+args.db)
    c=sqlite3.connect('file:%s?mode=ro'%Path(args.db).resolve(),uri=True)
    try:
        try:result=report(c)
        except sqlite3.OperationalError:result=dict(days=[],total=dict(decided=0,scored=0,kept=0))
    finally:c.close()
    t=result['total']
    if not result['days']:
        print('暂无人工"留下 / 去掉"记录，无法复算前 20 留下率。墨墨在后台开始拣选后再运行。')
        return result
    print('（留下率只在有当时分数的决定里算；"无分"为分数上线前等无快照分数的决定，不参与比较）')
    print('日期        决定  无分  有分  留下  平均留下率  前%d件  前%d留下率'%(TOP,TOP))
    for r in result['days']:
        print('%s  %4d  %4d  %4d  %4d  %10s  %5d  %10s'%(r['day'],r['decided'],r['unscored'],r['scored'],r['kept'],_pct(r['avg_rate']),r['top_n'],_pct(r['top_rate'])))
    print('合计        %4d  %4d  %4d  %4d  %10s  %5d  %10s'%(t['decided'],t['unscored'],t['scored'],t['kept'],_pct(t['avg_rate']),t['top_n'],_pct(t['top_rate'])))
    if not t['scored']:
        print('说明：已决定的货都没有当时分数（分数上线前的决定），暂无法比较前 20 与平均留下率。')
    elif not t['kept']:
        print('说明：有分数的决定里还没有"留下"（留下率均为 0），暂无法比较前 20 与平均留下率。')
    return result

if __name__=='__main__':main()
