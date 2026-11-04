"""综合评分参数对比（故事 27）：调任何参数之前，先看排序差异与符合度变化。只读，从不写库。

用法：
    uv run python scripts/score_compare.py --set value=35
    uv run python scripts/score_compare.py --set momo=50,value=30 --set trend_pattern=2

可改的参数名：momo、value、trend、customer（权重）、trend_brand、trend_pattern、trend_function、
customer_k、customer_max、smoothing、ai_share、min_peers、half_life_days。未知参数名报错退出。

输出（当前参数 config/scoring.yaml 对比 --set 之后的参数）：
1. 回测：已有标签（墨墨留下 / 去掉或看过没选）的货里，综合分前 100 的命中数（留下数）与随机基线（100 × 留下率）。
   口味用留一法，一件货自己的标签不计入自己的分数。
2. 当前 R12 候选（商品池里能上 R12 的货）前 12、商品池前 20 的名次变化。
3. 各已封存日按两组参数重算 07 进池符合度（同一份封存列表、同样的 K 与 V，只换排序；封存本身不动）。

真实库以 immutable=1 只读打开；GoodCustomer 订单只读。
"""
import argparse
import sqlite3
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from curio import agreement, publish, scoring  # noqa: E402
from curio.config import scoring_override, scoring_rules_for  # noqa: E402

TOP_BACKTEST = 100
TOP_RANK = publish.RANK_SIZE
TOP_POOL = 20
READ_TRIES = 3          # immutable 读正在被采集写入的库偶尔报"database disk image is malformed"：整次读取重试
READ_PAUSE = 1.0


def parse_sets(values):
    out = {}
    for chunk in values or []:
        for part in chunk.split(','):
            part = part.strip()
            if not part:
                continue
            if '=' not in part:
                raise ValueError('参数格式应为 名=值：' + part)
            name, raw = (x.strip() for x in part.split('=', 1))
            try:
                out[name] = float(raw)
            except ValueError:
                raise ValueError('参数值必须是数：' + part) from None
    if not out:
        raise ValueError('请用 --set 名=值 给出要对比的参数')
    return out


def connect_readonly(path):
    c = sqlite3.connect('file:%s?immutable=1' % Path(path).resolve(), uri=True)
    c.row_factory = sqlite3.Row
    return c


def backtest(data, top=TOP_BACKTEST):
    labelled = [it for it in data['items'] if it['label'] in (0, 1)]
    if not labelled:
        return dict(n=0, hits=0, top=0, baseline=0.0)
    scores = data['scores']
    ranked = sorted(labelled, key=lambda it: (-scores[it['item_id']]['score'], it['item_id']))[:top]
    rate = sum(it['label'] for it in labelled) / len(labelled)
    return dict(n=len(labelled), hits=sum(it['label'] for it in ranked), top=len(ranked), baseline=rate * len(ranked))


def _first_seen(c):
    return {r[0]: r[1] or '' for r in c.execute('SELECT item_id, first_seen FROM items')}


def order_by(ids, scores, first_seen):
    """封存口径的排序（同 agreement 的 SQL）：分数高到低（没分的在后），同分 COALESCE(first_seen,'') 新到旧
    （空的在最后），再按 item_id。用稳定的多趟排序，从最次要的键排起。"""
    out = sorted(ids)
    out.sort(key=lambda i: first_seen.get(i) or '', reverse=True)     # reverse 仍是稳定排序
    out.sort(key=lambda i: (scores.get(i) is None, -(scores[i]['score'] if scores.get(i) else 0)))
    return out


def candidates(c, sql):
    return [r[0] for r in c.execute(sql)]


def names(c):
    out = {}
    for r in c.execute('SELECT i.item_id, i.title, s.std_name, s.status FROM items i LEFT JOIN std_names s ON s.item_id=i.item_id'):
        out[r[0]] = r[2] if r[3] == 'named' and r[2] else r[1] or r[0]
    return out


def rank_changes(ids, a, b, first_seen, top):
    """[(新名次, 旧名次或 None, item_id)]：按新参数的前 top 件；旧名次为当前参数下在同一候选里的名次。"""
    old = {i: n for n, i in enumerate(order_by(ids, a, first_seen), 1)}
    new = order_by(ids, b, first_seen)[:top]
    return [(n, old.get(i), i) for n, i in enumerate(new, 1)]


def sealed_agreement(c, day, scores, first_seen):
    """用给定分数重排当天封存的 pool 列表，按 07 口径算进池符合度。scores 为 None 时用封存时的原排序。"""
    pred = agreement._prediction(c, day, 'pool')
    if pred is None:
        return None
    order = pred['items'] if scores is None else order_by(pred['items'], scores, first_seen)
    pos = set(order)
    lo, hi = agreement.bounds(day)
    start = max(lo, pred['sealed_at'])
    kept = {i for i in agreement._kept(c, start, hi) if i in pos}
    seen = {i for i in agreement._seen(c, start, hi) if i in pos} | kept
    n = len(kept)
    if not n:
        return dict(n=0)
    picked = [i for i in order if i in seen][:n]
    hits = len(kept & set(picked))
    return dict(n=n, seen=len(seen), hits=hits, agreement=hits / n, baseline=n / len(seen))


def _fmt_rank(old):
    return '—' if old is None else str(old)


WEIGHT_LABELS = (('momo', '口味'), ('value', '性价比'), ('trend', '客群'), ('customer', '客人'))


def shares(params):
    w = params['weights']
    total = sum(w.values())
    return ' · '.join('%s %.1f%%' % (zh, 100 * w[k] / total) for k, zh in WEIGHT_LABELS)


def report(c, cur, new, sets, out=print):
    out('综合评分对比：当前参数 %s  vs  %s' % (cur['version'], ', '.join('%s=%g' % kv for kv in sets.items())))
    out('权重是相对的（各自 ÷ 权重和）。实际占比：')
    for label, data in (('当前', cur), ('对比', new)):
        out('  %s：%s' % (label, shares(data['params'])))
    if not cur['demand_ok']:
        out('（GoodCustomer 订单读不了：客群趋势两边都是 50）')
    out('')
    out('== 1. 回测（已有标签的货里综合分前 %d）==' % TOP_BACKTEST)
    for label, data in (('当前', cur), ('对比', new)):
        b = backtest(data)
        lift = b['hits'] / b['baseline'] if b['baseline'] else 0
        out('%s：前 %d 命中 %d 件，随机基线 %.1f 件（%.1f 倍；有标签 %d 件）' % (label, b['top'], b['hits'], b['baseline'], lift, b['n']))
    fs, nm = _first_seen(c), names(c)
    for title, sql, top in (('2a. R12 候选前 %d' % TOP_RANK, publish.ELIGIBLE_SQL, TOP_RANK),
                            ("2b. 商品池前 %d" % TOP_POOL, "SELECT item_id FROM triage WHERE state='kept'", TOP_POOL)):
        out('')
        out('== %s（新名次 ← 当前名次）==' % title)
        ids = candidates(c, sql)
        if not ids:
            out('（没有货）')
            continue
        for n, old, iid in rank_changes(ids, cur['scores'], new['scores'], fs, top):
            mark = '' if old == n else ('  新进' if old is None or old > top else '  ↑' if old > n else '  ↓')
            out('%2d ← %3s  %5.1f → %5.1f  %s%s' % (n, _fmt_rank(old), cur['scores'][iid]['score'],
                                                 new['scores'][iid]['score'], nm.get(iid, iid)[:60], mark))
    out('')
    out('== 3. 已封存日重算的 07 进池符合度（封存时 / 当前参数 / 对比参数）==')
    out('（注意：重算的两栏用的是今天的标签，已含当天及之后的挑选，比"封存时"偏乐观；只拿"当前参数"与"对比参数"两栏互相比较。）')
    days = [date.fromisoformat(r[0]) for r in c.execute("SELECT day FROM daily_predictions WHERE kind='pool' ORDER BY day")]
    if not days:
        out('（还没有封存的日子）')
    for d in days:
        parts = []
        for scores in (None, cur['scores'], new['scores']):
            r = sealed_agreement(c, d, scores, fs)
            parts.append('无数据' if not r or not r['n'] else '%.0f%%' % (100 * r['agreement']))
        r = sealed_agreement(c, d, None, fs)
        extra = '（留下 %d、看到 %d，随机基线 %.0f%%）' % (r['n'], r['seen'], 100 * r['baseline']) if r and r['n'] else ''
        out('%s：%s / %s / %s %s' % (d.isoformat(), *parts, extra))


def main(argv=None, out=print):
    ap = argparse.ArgumentParser(description='综合评分参数只读对比（故事 27）')
    ap.add_argument('--set', action='append', required=True, help='要对比的参数，如 momo=50,value=30')
    ap.add_argument('--db', default=str(ROOT / 'data/curio.db'), help='curio.db 路径（只读打开）')
    ap.add_argument('--gc', default=None, help='GoodCustomer 资料库路径（默认读 config/settings.yaml）')
    ap.add_argument('--root', default=str(ROOT), help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    try:
        sets = parse_sets(args.set)
        base = scoring_rules_for(args.root)
        other = scoring_override(base, sets)
    except ValueError as e:
        ap.exit(2, '参数错误：%s\n' % e)
    if not Path(args.db).is_file():
        ap.exit(2, '找不到 curio.db：%s\n' % args.db)
    today = agreement.today()
    for attempt in range(1, READ_TRIES + 1):
        lines = []              # 整次读取成功后才输出，重试不会重复打印
        c = None
        try:
            c = connect_readonly(args.db)
            cur = scoring.compute(c, args.root, params=base, gc_db=args.gc, today=today)
            new = scoring.compute(c, args.root, params=other, gc_db=args.gc, today=today)
            report(c, cur, new, sets, lines.append)
            break
        except sqlite3.DatabaseError as e:
            if attempt == READ_TRIES:
                ap.exit(1, '读取 curio.db 失败（已重试 %d 次，可能正在采集写入，请稍后再试）：%s\n' % (READ_TRIES, e))
            time.sleep(READ_PAUSE)
        finally:
            if c is not None:
                c.close()
    for line in lines:
        out(line)
    return 0


if __name__ == '__main__':
    sys.exit(main())
