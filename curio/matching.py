"""后台 04 匹配（故事 16，CAP-5；FR-9、FR-10、FR-17；AD-15、AD-29）：这件货适合谁。

- 对象：02 商品池里的货（拣选状态 kept：已留下、未移出、未标记售出）× GoodCustomer 客户（customers/ 与 潜在客户/，
  跳过已合并 / 已归档，沿用 curio/guests.py 的读取与 30 秒缓存）。客户称呼取档案的显示名。
- 规则只在规则归口（curio/judge.py 的 match_facts / match_customer，经 curio/rules.py 调用）；词表与权重在
  config/matching.yaml。本模块只读 GoodCustomer 每位客户 06_结构化结果/ 下的 needs.jsonl 与 preferences.jsonl，
  从不写 GoodCustomer 的任何文件，不调用 AI，结果不上线。坏行（不是 JSON 对象）跳过，每个文件每次读取记一次警告。
- 按客人汇总：有"明说想要"的客人在前，再按配上件数；每位客人的货按分级（明说想要 > 明说喜欢 > 推算口味）、再按分数。
- 墨墨逐件"确认"或"不合适"（表 match_decisions，写请求带 request_id，同一 request_id 重放返回第一次的结果）；
  不合适的此后不再对该客人提出，可"撤销"（删掉那一行）。只接受此刻匹配的客人与货（或已有处理记录的）。分区计数 = 有未处理（未确认、未标不合适）匹配的客人数。
- 单件页链接（AD-29）：站点地址（.env 的 CURIO_SERVER_URL，须为 http(s) 地址；不需要管理密钥）+ /item/CO-NNNN，不按人区分；
  只在该货号在当前已确认送达的快照（publishes.status=delivered）中且未隐藏（published.hidden=0）时给出，
  否则给出原因"请先发布这件货"；站点地址未配置时同样不给链接并说明。
"""
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import guests, ranking, rules, sync, triage

from .config import ROOT, _env, match_rules_for
from .store import card

log = logging.getLogger(__name__)

STRUCT_DIR = '06_结构化结果'
DECISIONS = ('confirmed', 'rejected')
REASON_UNPUBLISHED = '请先发布这件货'
REASON_NO_SITE = '站点地址未配置（.env 的 CURIO_SERVER_URL），暂不能复制链接'
REASON_BAD_SITE = '站点地址无效（.env 的 CURIO_SERVER_URL 须为 http(s) 地址），暂不能复制链接'
_cache = {}                       # gc['db'] -> (读取时刻, 全部客户的需求与喜好)
_cache_lock = threading.Lock()


# ---------- GoodCustomer（只读） ----------

def _read_jsonl(path):
    """逐行读 JSON 对象；文件不在返回 []。无效的 UTF-8 字节替换后照读，只跳过解析不了的行，本文件记一次警告。
    返回 (行列表, 坏行数, 文件读不了)。"""
    try:
        text = Path(path).read_text(encoding='utf-8', errors='replace')
    except FileNotFoundError:
        return [], 0, False
    except OSError as e:
        log.warning('未能读取 %s（%s），已跳过', path, type(e).__name__)
        return [], 0, True
    rows, bad, first = [], 0, None
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except ValueError:
            r = None
        if isinstance(r, dict):
            rows.append(r)
        else:
            bad += 1
            first = first or n
    if bad:
        log.warning('%s 有 %d 行不是 JSON 对象（第 %d 行起），已跳过', path, bad, first)
    return rows, bad, False


def _customer_dir(gc, cid):
    for root in guests.GC_ROOTS:
        d = Path(gc['db']) / root / cid
        if d.is_dir():
            return d
    return None


def profiles(gc):
    """每位客户（跳过已合并 / 已归档）的需求与喜好：[{customer_id, name, needs, prefs, bad}]（进程内缓存 30 秒）。
    资料库不在时返回 None。"""
    if not guests.gc_available(gc):
        return None
    with _cache_lock:
        hit = _cache.get(gc['db'])
    if hit and time.monotonic() - hit[0] < guests.GC_CACHE_SECONDS:
        return hit[1]
    out = []
    for cust in guests.gc_customers(gc):
        d = _customer_dir(gc, cust['customer_id'])
        if d is None:
            continue
        needs, b1, u1 = _read_jsonl(d / STRUCT_DIR / 'needs.jsonl')
        prefs, b2, u2 = _read_jsonl(d / STRUCT_DIR / 'preferences.jsonl')
        if needs or prefs or b1 or b2 or u1 or u2:
            out.append(dict(customer_id=cust['customer_id'], name=cust['display_name'], needs=needs, prefs=prefs,
                            bad=b1 + b2, unreadable=int(u1) + int(u2)))
    with _cache_lock:
        _cache[gc['db']] = (time.monotonic(), out)
    return out


# ---------- 单件页链接可达（AD-29） ----------

def reachable_codes(c):
    """当前已确认送达的快照里、且未隐藏的货号。"""
    return {r[0] for r in c.execute('''SELECT pi.code FROM publish_items pi
        JOIN publishes p ON p.publish_no=pi.publish_no JOIN published pb ON pb.code=pi.code
        WHERE p.status='delivered' AND pb.hidden=0''')}


def site(root=ROOT):
    """客人页站点地址：直接读 .env 的 CURIO_SERVER_URL（链接不需要管理密钥）。返回 (地址无末尾斜杠或 None, 不能用的原因)。"""
    url = ((_env(root, ('CURIO_SERVER_URL',)) or {}).get('CURIO_SERVER_URL') or '').strip().rstrip('/')
    if not url:
        return None, REASON_NO_SITE
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.query or parts.fragment:
        return None, REASON_BAD_SITE
    return url, None


def item_link(site, code):
    return site.rstrip('/') + '/item/' + code


# ---------- 视图 ----------

def _pool(c):
    """商品池里的货（kept、未标记售出），连同标准名称：一次查询。返回 [(items 行, std_names 行或 None)]。"""
    rows = c.execute('''SELECT i.*, s.score sort_score FROM triage t JOIN items i ON i.item_id=t.item_id
        LEFT JOIN scores s ON s.item_id=i.item_id WHERE t.state='kept'
        ORDER BY s.score IS NULL, s.score DESC, i.item_id''').fetchall()
    std = {r['item_id']: r for r in c.execute('''SELECT n.* FROM std_names n JOIN triage t ON t.item_id=n.item_id
        WHERE t.state='kept' ''')}
    try:
        sold = {r[0] for r in c.execute("SELECT item_id FROM triage WHERE state='kept' AND sold=1")}
    except sqlite3.OperationalError:
        sold = set()            # 旧表还没有 sold 列
    return [(r, std.get(r['item_id'])) for r in rows if r['item_id'] not in sold]


def _facts(vocab, r, std):
    return rules.match_facts(std, r['title'], vocab)


def view(store, gc, root=ROOT):
    """04 匹配整页数据：按客人汇总的匹配、计数（有未处理匹配的客人数）、资料库与站点地址状态。
    matching.yaml 缺失或格式不对时抛 FileNotFoundError / ValueError（中文）。"""
    vocab = match_rules_for(root)
    url, site_problem = site(root)
    profs = profiles(gc)
    if profs is None:
        return dict(available=False, error=guests.MSG_NO_GC, count=0, customers=[], site=url is not None,
                    site_problem=site_problem, warnings=0, unreadable=0)
    customers = []
    with store.connect() as c:
        decisions = {(r['customer_id'], r['item_id']): r['decision']
                     for r in c.execute('SELECT customer_id,item_id,decision FROM match_decisions')}
        reach = reachable_codes(c)
        pool = []
        for r, std in _pool(c):
            facts = _facts(vocab, r, std)
            if facts['brand']:
                pool.append((r, facts))
        cards = {}

        def shown(r):
            iid = r['item_id']
            if iid not in cards:
                cd = card(c, r, root)
                code = cd['code']
                if code not in reach:
                    link, why = None, REASON_UNPUBLISHED
                elif not url:
                    link, why = None, site_problem
                else:
                    link, why = item_link(url, code), None
                cards[iid] = dict(item_id=iid, code=code, name=cd['name'], name_lang=cd['name_lang'], price=cd['price'],
                                  image=cd['image'], tags=cd['tags'], on_sale=r['status'] == 'active', score=cd['score'],
                                  link=link, link_reason=why)
            return cards[iid]
        for p in profs:
            rows = []
            for r, facts in pool:
                dec = decisions.get((p['customer_id'], r['item_id']))
                if dec == 'rejected':
                    continue
                m = rules.match_customer(facts, p['needs'], p['prefs'], vocab)
                if m:
                    rows.append(dict(shown(r), grade=m['grade'], grade_label=m['label'], weight=m['weight'],
                                     reasons=m['reasons'], decision=dec))
            if not rows:
                continue
            rows.sort(key=lambda x: (-x['weight'], x['score'] is None, -(x['score'] or 0), x['code'] or ''))
            customers.append(dict(customer_id=p['customer_id'], name=p['name'], items=rows, total=len(rows),
                                  pending=sum(1 for x in rows if x['decision'] is None),
                                  has_need=any(x['grade'] == 'need' for x in rows)))
    # 有明说想要的在前，再按待处理件数、配上件数（处理完的客人不压在有待处理的前面）。
    customers.sort(key=lambda x: (not x['has_need'], -x['pending'], -x['total'], x['name'], x['customer_id']))
    return dict(available=True, count=sum(1 for x in customers if x['pending']), customers=customers,
                site=url is not None, site_problem=site_problem, warnings=sum(p['bad'] for p in profs),
                unreadable=sum(p['unreadable'] for p in profs))


def matches_now(store, gc, customer_id, item_id, root=ROOT):
    """这位客人与这件货此刻是否匹配（商品池里、规则对上；不看已有的处理）。"""
    profs = profiles(gc) or []
    prof = next((p for p in profs if p['customer_id'] == customer_id), None)
    if prof is None:
        return False
    vocab = match_rules_for(root)
    with store.connect() as c:
        hit = next(((r, std) for r, std in _pool(c) if r['item_id'] == item_id), None)
    if hit is None:
        return False
    facts = _facts(vocab, *hit)
    return bool(facts['brand'] and rules.match_customer(facts, prof['needs'], prof['prefs'], vocab))


# ---------- 写：确认 / 不合适 ----------

def decide(store, gc, customer_id, item_id, decision, request_id, root=ROOT):
    """确认（confirmed）、不合适（rejected）或撤销不合适（undo，删掉不合适那一行，回到未处理）。
    只接受此刻匹配的客人与货，或已有处理记录的（改处理 / 撤销）。不合适之后只能先撤销，不能直接确认。
    同一 request_id 重放返回第一次的结果。"""
    if decision not in DECISIONS + ('undo',):
        raise ValueError('未知操作')
    if not isinstance(customer_id, str) or not guests.GC_ID.fullmatch(customer_id):
        raise ValueError('客户编号无效')
    if not isinstance(item_id, str) or not item_id:
        raise ValueError('商品编号无效')
    with store.connect() as c:
        has_row = c.execute('SELECT 1 FROM match_decisions WHERE customer_id=? AND item_id=?',
                            (customer_id, item_id)).fetchone() is not None
        replay = c.execute('SELECT 1 FROM triage_requests WHERE request_id=?', (request_id,)).fetchone() is not None \
            if isinstance(request_id, str) else False
    current = has_row or replay or matches_now(store, gc, customer_id, item_id, root)

    def work(c):
        prev = c.execute('SELECT decision FROM match_decisions WHERE customer_id=? AND item_id=?',
                         (customer_id, item_id)).fetchone()
        at = sync.utc_now()
        if decision == 'undo':
            if not prev or prev[0] != 'rejected':
                raise ValueError('这件货没有标为不合适，无需撤销')
            c.execute('DELETE FROM match_decisions WHERE customer_id=? AND item_id=?', (customer_id, item_id))
            return dict(ok=True, customer_id=customer_id, item_id=item_id, decision='undo', at=at)
        if triage.state(c, item_id) != 'kept' or triage.is_sold(c, item_id):
            raise ValueError('这件货已不在商品池，请刷新本页')
        if prev is None and not current:
            raise ValueError('这位客人与这件货现在没有匹配，请刷新本页')
        if prev and prev[0] == 'rejected':
            raise ValueError('这件货已标为不合适，请先撤销')
        c.execute('INSERT OR REPLACE INTO match_decisions(customer_id,item_id,decision,at) VALUES(?,?,?,?)',
                  (customer_id, item_id, decision, at))
        return dict(ok=True, customer_id=customer_id, item_id=item_id, decision=decision, at=at)
    return ranking._once(store, request_id, ['match', customer_id, item_id, decision], work)
