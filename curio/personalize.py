"""千人千面商品池（故事 18，CAP-12、FR-24；AD-5、AD-6、AD-11）：发布时为每个账号算好商品池货号顺序。

- 谁有个性化顺序：账号已关联 GC 客户（guest_links，故事 14）、账号在用（guest_accounts.status=active，且没有
  未被拒收的停用指令），并且该客户有可用记录——GoodCustomer 需求 / 喜好对上池中至少一件货（含不喜欢的对上）、
  墨墨在 04 对该客户标过"不合适"的货在池中、或该客户关联账号在行为账本（behavior_events，故事 15）里有事件。
  其余账号不下发顺序，看墨墨的顺序；停用账号的顺序因此在下次发布时自然移除。与墨墨顺序完全相同的也不下发。
- 已合并的 GC 档案顺着 merged_into_customer_id 找到存续档案的需求与喜好（只读）；找不到档案的记日志，只用浏览。
- 打分只在规则归口（curio/judge.personal_scores / order_by_scores，经 curio/rules.py）；参数在
  config/personalize.yaml，匹配词表与分级权重在 config/matching.yaml。不调用 AI。
- 读取与打分（GoodCustomer、行为账本、标准名称，各一次查询）都在写事务之外（prepare）；写事务里只重新核对
  谁在用、按算好的分数排序（orders）。行为账本只读最近 CUTOFF_HALF_LIVES 个半衰期内的事件。
- 下发的顺序只到与墨墨顺序最后一处不同的位置为止（其后与墨墨顺序相同，服务器本来就按墨墨顺序补在末尾）。
- 千人千面是"尽量做到"：准备出任何错都只记警告、本次发布不带顺序（排序一步的错误由 publish 兜住）。
"""
import json
import logging
import time
from datetime import datetime, timezone

from . import guests, matching, rules
from .config import ROOT, match_rules_for, personalize_rules_for

log = logging.getLogger(__name__)

BROWSE_TYPES = ('item_open', 'item_dwell')
CUTOFF_HALF_LIVES = 10
MERGE_DEPTH = 5


def active_customers(c):
    """{GC 客户编号: [在用的已关联账号编号…]}（账号编号升序）。"""
    out = {}
    rows = c.execute('''SELECT l.account_id, l.customer_id FROM guest_links l
        JOIN guest_accounts a ON a.account_id=l.account_id
        WHERE a.status='active' AND NOT EXISTS (
            SELECT 1 FROM guest_commands g JOIN outbox o ON o.event_id=g.event_id
            WHERE g.account_id=l.account_id AND g.kind='disable_account' AND o.failed_at IS NULL)
        ORDER BY l.customer_id, l.account_id''').fetchall()
    for account_id, customer_id in rows:
        if guests.ACCOUNT_ID.fullmatch(account_id or '') and guests.GC_ID.fullmatch(customer_id or ''):
            out.setdefault(customer_id, []).append(account_id)
    return out


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def _epoch(ts):
    return datetime.fromisoformat(ts.replace('Z', '+00:00')).timestamp()


def _profiles(gc, customer_ids):
    """{客户编号: 档案（needs、prefs）}；已合并的顺着 merged_into 找存续档案。没有档案的记日志。"""
    profs = (matching.profiles(gc) or []) if gc is not None else []
    by_id = {p['customer_id']: p for p in profs}
    merged = {}
    if gc is not None and any(cid not in by_id for cid in customer_ids):
        merged = {x['customer_id']: x.get('merged_into') for x in guests.gc_customers(gc, include_skipped=True)}
    out = {}
    for cid in customer_ids:
        cur, depth = cid, 0
        while cur not in by_id and merged.get(cur) and depth < MERGE_DEPTH:
            cur, depth = merged[cur], depth + 1
        if cur in by_id:
            out[cid] = by_id[cur]
            if cur != cid:
                log.info('已关联客户 %s 已合并到 %s，按存续档案的需求与喜好排序', cid, cur)
        else:
            log.info('已关联客户 %s 没有可读的需求与喜好档案，只用浏览信号', cid)
    return out


def _facts(c, vocab):
    """全部有货号的货 → match_facts（一次查询；读不了的跳过）。"""
    out = {}
    for r in c.execute('''SELECT k.code, i.title, n.* FROM item_codes k JOIN items i ON i.item_id=k.item_id
            LEFT JOIN std_names n ON n.item_id=k.item_id'''):
        try:
            out[r['code']] = rules.match_facts(r if r['item_id'] is not None else None, r['title'], vocab)
        except Exception:
            log.warning('货号 %s 的标准名称读不了，千人千面跳过这件', r['code'], exc_info=True)
    return out


def _events(c, accounts, since):
    """在用账号 since 之后的事件（一次查询）：{账号: [(类型, 货号, params 文字, server_ts)]}。"""
    marks = ','.join('?' * len(accounts))
    out = {}
    for r in c.execute(f'''SELECT account_id, type, co_code, params, server_ts FROM behavior_events
            WHERE account_id IN ({marks}) AND server_ts>=? ORDER BY server_ts, event_id''', list(accounts) + [since]):
        out.setdefault(r[0], []).append((r[1], r[2], r[3], r[4]))
    return out


def _seen(rows, facts, now):
    """浏览事件 → [(货号, facts, 类型, 停留秒数或 None, 距今天数)]（坏行跳过）。"""
    out = []
    for kind, code, params, server_ts in rows:
        if kind not in BROWSE_TYPES or code not in facts:
            continue
        try:
            age = max(0.0, (now - _epoch(server_ts)) / 86400)
            amount = (json.loads(params) or {}).get('seconds') if kind == 'item_dwell' else None
        except (ValueError, AttributeError, TypeError):
            continue
        if kind == 'item_dwell' and (isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount <= 0):
            continue
        out.append((code, facts[code], kind, amount, age))
    return out


def prepare(store, gc=None, root=ROOT, now=None):
    """发布前、写事务之外读数据并打分。没有在用的已关联账号时返回 None（不读 GoodCustomer）；
    出任何错记警告并返回 None（本次不带顺序）。gc 为 None 时读 config/settings.yaml 的 goodcustomer。
    返回 {customers: {客户编号: {scores: {货号: (沉底, 分数)}, record: 是否有可用记录}}}。"""
    try:
        return _prepare(store, gc, root, time.time() if now is None else now)
    except Exception:
        log.warning('千人千面准备失败，本次发布不带个性化顺序', exc_info=True)
        return None


def _prepare(store, gc, root, now):
    from . import publish
    with store.connect() as c:
        customers = active_customers(c)
    if not customers:
        return None
    vocab, params = match_rules_for(root), personalize_rules_for(root)
    if gc is None:
        try:
            from .config import load
            gc = load(root)['goodcustomer']
        except Exception:
            log.warning('读取 goodcustomer 设置失败，千人千面只用浏览信号', exc_info=True)
    try:
        profs = _profiles(gc, customers)
    except Exception:
        log.warning('读取 GoodCustomer 需求与喜好失败，千人千面只用浏览信号', exc_info=True)
        profs = {}
    since = _iso(now - CUTOFF_HALF_LIVES * params['half_life_days'] * 86400)
    with store.connect() as c:
        pool_codes = [it['code'] for it in publish.eligible(c)]
        facts = _facts(c, vocab)
        events = _events(c, [a for accts in customers.values() for a in accts], since)
        rejected = {}
        for cid, code in c.execute('''SELECT d.customer_id, k.code FROM match_decisions d
                JOIN item_codes k ON k.item_id=d.item_id WHERE d.decision='rejected' '''):
            rejected.setdefault(cid, set()).add(code)
    pool = [(code, facts[code]) for code in pool_codes if code in facts]
    out = {}
    for cid, accts in customers.items():
        prof = profs.get(cid) or {}
        rows = [e for a in accts for e in events.get(a, [])]
        mine = rejected.get(cid, set())
        scores, matched = rules.personal_scores(pool, prof.get('needs') or [], prof.get('prefs') or [],
                                                _seen(rows, facts, now), mine, vocab, params)
        out[cid] = dict(scores=scores, record=matched or bool(rows) or bool(mine & set(pool_codes)))
    return dict(customers=out)


def _prefix(order, base):
    """只留到与墨墨顺序最后一处不同的位置为止；与墨墨顺序完全相同时返回 []。"""
    last = max((i for i, (a, b) in enumerate(zip(order, base)) if a != b), default=-1)
    return order[:last + 1]


def orders(c, items, inputs):
    """在调用方事务内按 prepare 算好的分数排序：items 为入选的货（publish.eligible 的行，墨墨顺序）。
    重新核对谁在用（准备之后被停用、解除的不再下发）。返回 ({账号编号: 货号前缀}, {账号编号: 客户编号})。"""
    if not inputs or not items:
        return {}, {}
    base = [it['code'] for it in items]
    out, owner = {}, {}
    for cid, accts in active_customers(c).items():
        got = inputs['customers'].get(cid)
        if not got or not got['record']:
            continue
        prefix = _prefix(rules.order_by_scores(base, got['scores']), base)
        if not prefix:
            continue
        for account_id in accts:
            out[account_id] = list(prefix)
            owner[account_id] = cid
    return dict(sorted(out.items())), owner
