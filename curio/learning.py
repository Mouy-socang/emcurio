"""判断沉淀（故事 17，CAP-8、FR-18、FR-19；AD-15、AD-26）：从墨墨的人工拣选归纳待审规律，墨墨确认 / 否决 / 停用。

- 计数（FR-18）：只看 triage_events 里 actor=human、未被撤销的留下 / 去掉 / 捞回（捞回计入"留下"方向）；
  同一件货按其最后一次这样的动作计一次。点评只记录，不参与归纳（v1）。
- 同一类操作 = 特征组合（特征只取自标准名称 std_names，见 rules.rule_features）：单维（品牌 / 器型 / 图案）、
  两维（品牌+器型、品牌+图案）与品牌+图案+器型（图案只在 Meissen 下识别，即"Meissen梅森 蓝花的盘"这一级）；
  外画标记与价格区间是可选附加维——只在基础组合因比例不够而不成立时才往下细分（如"Meissen梅森的盘（€50 以下）"）。
- 门槛（config/learning.yaml）：同一方向 ≥ min_count 件且占该组合人工决定 ≥ min_ratio。
  组合越具体越优先：从最具体的组合开始提出；更一般的组合若已有待审或已确认的同向规律，具体组合不再提出、
  已在待审的具体组合撤下；一个组合的依据件若已全部落在别的同向规律（任何状态）里，也不再换个组合提出。
  否决、停用按组合键与方向记录，之后不再提出（即使次数继续增加）。
- 规律只是数据（learned_rules）：确认后由规则归口 curio/rules.py 在运行时读取执行（去掉类：入库 / 重判自动移出；
  留下类：基础分加分），从不改动已有人工决定的货。规律句子用模板生成，不调用 AI，如
  "Meissen梅森 蓝花的盘：6 件里你去掉了 6 件——以后自动去掉？"。
- 归纳在写锁外用一次联表查询算好，只在短写事务里落库；人工事件、标准名称、规律与参数都没变时跳过（kv learning_sig）。
"""
import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone

from . import rules

log = logging.getLogger(__name__)

DIRECTIONS = ('keep', 'remove')
# 计入归纳的人工动作 → 方向。
ACTION_DIRECTION = {'keep': 'keep', 'rescue': 'keep', 'remove': 'remove'}
# 基础组合（由一般到具体）与可选附加维。
BASES = (('brand',), ('function_id',), ('pattern_id',), ('brand', 'function_id'), ('brand', 'pattern_id'),
         ('brand', 'pattern_id', 'function_id'))
EXTRAS = (('overpaint',), ('price',), ('overpaint', 'price'))
STATUSES = ('pending', 'confirmed', 'rejected', 'retired')
ACTIVE = ('pending', 'confirmed')     # 会挡住更具体组合的状态
VERB = {'keep': '留下', 'remove': '去掉'}
ASK = {'keep': '以后同类新货加分？', 'remove': '以后自动去掉？'}
THROTTLE_SECONDS = 10                 # 打开 05 时归纳的最短间隔
REFRESH_BUSY_MS = 500                 # 打开 05 时归纳等写锁的上限，超过就跳过本次


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def ensure(c):
    # combo_key 即规律的特征组合（规范化 JSON），dims 由它解析。
    c.execute('''CREATE TABLE IF NOT EXISTS learned_rules(
        id INTEGER PRIMARY KEY AUTOINCREMENT, combo_key TEXT NOT NULL,
        direction TEXT NOT NULL CHECK(direction IN ('keep','remove')), label TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('pending','confirmed','rejected','retired')),
        count INTEGER NOT NULL, total INTEGER NOT NULL, last_at TEXT,
        created_at TEXT NOT NULL, decided_at TEXT, UNIQUE(combo_key,direction))''')
    c.execute('CREATE INDEX IF NOT EXISTS learned_rules_status ON learned_rules(status,id)')


def combo_key(dims):
    return json.dumps(dims, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def dims_of(row):
    return json.loads(row['combo_key'])


def _within(small, big):
    """small 的每一维都在 big 里且取值相同（small 比 big 一般或相同）。"""
    return all(k in big and big[k] == v for k, v in small.items())


def band_text(band):
    sign = '€' if band.get('currency') == 'EUR' else band.get('currency', '') + ' '
    num = lambda x: ('%d' % x) if float(x).is_integer() else '%.2f' % x
    if band.get('lo') is None:
        return '%s%s 以下' % (sign, num(band['hi']))
    if band.get('hi') is None:
        return '%s%s 以上' % (sign, num(band['lo']))
    return '%s%s–%s' % (sign, num(band['lo']), num(band['hi']))


def label(dims, names):
    """规律所指的那类货（模板，不调用 AI），如"Meissen梅森 蓝花的盘"、"Meissen梅森的盘（€50 以下）"。"""
    mods = []
    if dims.get('overpaint'):
        mods.append('外画')
    if 'brand' in dims:
        mods.append(names.get('brand') or dims['brand'])
    if 'pattern_id' in dims:
        mods.append(names.get('pattern') or dims['pattern_id'])
    noun = (names.get('function') or dims['function_id']) if 'function_id' in dims else '货'
    text = (' '.join(mods) + '的' + noun) if mods else noun
    if 'price' in dims:
        text += '（' + band_text(dims['price']) + '）'
    return text


def sentence(row):
    """待审核条目的一句话（按件计）：如"Meissen梅森 蓝花的盘：6 件里你去掉了 6 件——以后自动去掉？"。"""
    return '%s：%d 件里你%s了 %d 件——%s' % (row['label'], row['total'], VERB[row['direction']], row['count'],
                                        ASK[row['direction']])


def effect(row):
    """已生效规律的一句：如"自动去掉：Meissen梅森 蓝花的盘"。"""
    return ('自动去掉：' if row['direction'] == 'remove' else '新货加分：') + row['label']


def _names(std):
    try:
        seg = json.loads(std['segments']) if std['segments'] else {}
    except (TypeError, ValueError):
        seg = {}
    return {k: seg.get(k) for k in ('brand', 'pattern', 'function')}


# 每件货最后一次计入归纳的人工动作（未撤销的留下 / 去掉 / 捞回），连同标准名称特征与标价：一次联表查询。
_DECISIONS = '''SELECT e.item_id,e.action,e.at,s.brand,s.pattern_id,s.function_id,s.overpaint,s.segments,
    i.listed_price,i.currency
    FROM (SELECT item_id,max(id) id FROM triage_events WHERE actor='human' AND undone_by IS NULL
          AND action IN ('keep','remove','rescue') GROUP BY item_id) last
    JOIN triage_events e ON e.id=last.id
    JOIN std_names s ON s.item_id=e.item_id JOIN items i ON i.item_id=e.item_id'''


def groups(c, cfg):
    """按特征组合汇总人工决定：{combo_key: {dims, base, keep:set, remove:set, last:{方向: 时间}, names}}。"""
    bands = cfg['price_bands']
    out = {}
    for r in c.execute(_DECISIONS).fetchall():
        iid, direction, at = r['item_id'], ACTION_DIRECTION[r['action']], r['at']
        feats = rules.rule_features(r, r, bands)
        names = _names(r)
        for base in BASES:
            if not all(k in feats for k in base):
                continue
            base_dims = {k: feats[k] for k in base}
            for extra in (None,) + EXTRAS:
                if extra and not all(k in feats for k in extra):
                    continue
                dims = dict(base_dims, **{k: feats[k] for k in extra or ()})
                key = combo_key(dims)
                g = out.setdefault(key, dict(dims=dims, base=combo_key(base_dims) if extra else None,
                                             keep=set(), remove=set(), last={}, names=names))
                g[direction].add(iid)
                if at and (g['last'].get(direction) or '') < at:
                    g['last'][direction] = at
    return out


def signature(c, cfg):
    """归纳输入的指纹：人工事件（撤销也会追加事件）、标准名称、规律表与参数。没变就不必重算。"""
    ev = c.execute("SELECT max(id) FROM triage_events WHERE actor='human'").fetchone()[0]
    std = list(c.execute('SELECT count(*),max(generated_at) FROM std_names').fetchone())
    lr = list(c.execute("SELECT count(*),group_concat(id||':'||status||':'||count) FROM learned_rules").fetchone())
    return json.dumps([ev, std, lr, cfg], ensure_ascii=False, sort_keys=True)


def _qualifies(g, direction, cfg):
    n = len(g[direction])
    total = len(g['keep']) + len(g['remove'])
    return n >= cfg['min_count'] and total > 0 and n / total >= cfg['min_ratio']


def _candidate(gs, g, direction, cfg):
    """够门槛；附加维组合只在其基础组合同方向不够门槛（比例不够）时才算。"""
    if not _qualifies(g, direction, cfg):
        return False
    return g['base'] is None or not _qualifies(gs[g['base']], direction, cfg)


_INSERT = '''INSERT INTO learned_rules(combo_key,direction,label,status,count,total,last_at,created_at)
    VALUES(?,?,?,'pending',?,?,?,?)'''


def _apply(c, gs, cfg, at=None):
    """把一次归纳结果落库（调用方已开写事务）：更新待审条目的依据次数、撤下已不够门槛或已被更一般规律覆盖的待审条目、
    提出新规律。已确认 / 已否决 / 已停用的条目不动。返回 dict(pending, proposed, withdrawn)。"""
    at = at or now()
    existing = {(r['combo_key'], r['direction']): r for r in c.execute('SELECT * FROM learned_rules')}
    withdrawn = 0
    for (key, direction), r in list(existing.items()):
        if r['status'] != 'pending':
            continue
        g = gs.get(key)
        if g is None or not _candidate(gs, g, direction, cfg):
            c.execute('DELETE FROM learned_rules WHERE id=?', (r['id'],))
            del existing[(key, direction)]
            withdrawn += 1
            continue
        total = len(g['keep']) + len(g['remove'])
        c.execute('UPDATE learned_rules SET count=?,total=?,last_at=?,label=? WHERE id=?',
                  (len(g[direction]), total, g['last'].get(direction), label(g['dims'], g['names']), r['id']))
    # 已有条目（任何状态）的维度与当前依据件；本轮新提出的也加进来。
    known = [dict(dims=json.loads(k), direction=d, status=r['status'], support=gs[k][d] if k in gs else set())
             for (k, d), r in existing.items()]
    cands = [(key, d, g) for key, g in gs.items() for d in DIRECTIONS if _candidate(gs, g, d, cfg)]
    # 基础组合越具体越先；同一基础组合下，附加维越少越先（只细分到能分开为止）；再按依据件数。
    def order(x):
        g = x[2]
        base = len(json.loads(g['base'])) if g['base'] else len(g['dims'])
        return (-base, len(g['dims']) - base, -len(g[x[1]]), x[0], x[1])
    cands.sort(key=order)
    proposed = 0
    for key, d, g in cands:
        if (key, d) in existing:
            continue
        dims, support = g['dims'], g[d]
        # 更一般的组合已有待审或已确认的同向规律：不再提出。
        if any(k['direction'] == d and k['status'] in ACTIVE and _within(k['dims'], dims) for k in known):
            continue
        # 依据件已全部落在别的同向规律里（更具体的、本轮先提出的、已确认、已否决或已停用的）：同一批依据不再换个组合提出。
        covered = set()
        for k in known:
            if k['direction'] == d and k['dims'] != dims:
                covered |= k['support']
        if support <= covered:
            continue
        total = len(g['keep']) + len(g['remove'])
        c.execute(_INSERT, (key, d, label(dims, g['names']), len(support), total, g['last'].get(d), at))
        known.append(dict(dims=dims, direction=d, status='pending', support=support))
        proposed += 1
    # 待审的具体组合：更一般的同向规律已待审或已确认时撤下。
    live = c.execute("SELECT * FROM learned_rules WHERE status IN ('pending','confirmed')").fetchall()
    for r in live:
        if r['status'] != 'pending':
            continue
        dims = dims_of(r)
        if any(o['id'] != r['id'] and o['direction'] == r['direction'] and _within(dims_of(o), dims) for o in live):
            c.execute("DELETE FROM learned_rules WHERE id=? AND status='pending'", (r['id'],))
            withdrawn += 1
    pending_n = c.execute("SELECT count(*) FROM learned_rules WHERE status='pending'").fetchone()[0]
    return dict(pending=pending_n, proposed=proposed, withdrawn=withdrawn)


def _mark(c, cfg):
    c.execute("INSERT OR REPLACE INTO kv VALUES('learning_sig',?)", (signature(c, cfg),))


def refresh(c, cfg, at=None):
    """在调用方的连接上归纳一次（必要时开写事务）。返回 dict(pending, proposed, withdrawn)。"""
    ensure(c)
    if not c.in_transaction:
        c.execute('BEGIN IMMEDIATE')
    r = _apply(c, groups(c, cfg), cfg, at)
    _mark(c, cfg)
    return r


def refresh_store(store, cfg, busy_ms=None):
    """调度与 05 用：写锁外先算指纹与分组，没变化就跳过；有变化时在短写事务里落库（期间输入变了就在事务里重算）。
    busy_ms：等写锁的上限（超时抛 sqlite3.OperationalError）。返回 dict(pending, proposed, withdrawn, skipped)。"""
    with store.connect() as c:
        ensure(c)
        sig = signature(c, cfg)
        mark = c.execute("SELECT v FROM kv WHERE k='learning_sig'").fetchone()
        if mark and mark[0] == sig:
            return dict(pending=pending_count(c), proposed=0, withdrawn=0, skipped=True)
        gs = groups(c, cfg)
    with store.connect() as c:
        if busy_ms is not None:
            c.execute('PRAGMA busy_timeout=%d' % int(busy_ms))
        c.execute('BEGIN IMMEDIATE')
        if signature(c, cfg) != sig:
            gs = groups(c, cfg)
        r = _apply(c, gs, cfg)
        _mark(c, cfg)
        return dict(r, skipped=False)


def confirmed(c, direction=None):
    """已确认（未停用）的规律（规则归口执行用）：[{id, direction, dims, label}]。表尚未建立时为空。"""
    try:
        rows = c.execute("SELECT * FROM learned_rules WHERE status='confirmed' ORDER BY id").fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(id=r['id'], direction=r['direction'], dims=dims_of(r), label=r['label'])
            for r in rows if direction is None or r['direction'] == direction]


def pending(c):
    """05 待审核条目：按提出先后。每条 id、direction、label、sentence、count、total、last_at。"""
    ensure(c)
    return [dict(id=r['id'], direction=r['direction'], label=r['label'], sentence=sentence(r), count=r['count'],
                 total=r['total'], last_at=r['last_at'])
            for r in c.execute("SELECT * FROM learned_rules WHERE status='pending' ORDER BY id")]


def active(c):
    """05 下方"已生效的规律"：已确认、未停用的规律，按确认先后。每条 id、direction、label、text、decided_at。"""
    ensure(c)
    return [dict(id=r['id'], direction=r['direction'], label=r['label'], text=effect(r), decided_at=r['decided_at'])
            for r in c.execute("SELECT * FROM learned_rules WHERE status='confirmed' ORDER BY decided_at,id")]


def pending_count(c):
    try:
        return c.execute("SELECT count(*) FROM learned_rules WHERE status='pending'").fetchone()[0]
    except sqlite3.OperationalError:
        return 0


def _rescore(c, learn):
    from .store import refresh_baseline   # 延迟导入：store 依赖本模块
    return sum(refresh_baseline(c, x[0], learn) for x in c.execute("SELECT item_id FROM triage WHERE state='new'").fetchall())


def decide(c, rule_id, decision, learn=None, at=None):
    """确认 / 否决一条待审规律，或停用（retire）一条已确认规律；均为最终操作，无撤销。learn：已读好的 config.learning。
    - 确认：与已确认的反方向规律可能同时适用于同一类货时拒绝。去掉类当场把未经人工决定的同组合新货移出；
      留下类为未经人工决定的新货重算基础分。
    - 停用：之后不再执行、也不再提出；留下类当场重算基础分；已被去掉类移出的货仍可捞回，重判时回到新货。"""
    if decision not in ('confirm', 'reject', 'retire'):
        raise ValueError('操作无效')
    if type(rule_id) is not int:
        raise ValueError('规律编号无效')
    ensure(c)
    if not c.in_transaction:
        c.execute('BEGIN IMMEDIATE')
    r = c.execute('SELECT * FROM learned_rules WHERE id=?', (rule_id,)).fetchone()
    if decision == 'retire':
        if r is None or r['status'] != 'confirmed':
            raise ValueError('这条规律不在已生效的规律里，请刷新')
    elif r is None:
        raise ValueError('这条规律已不在待审核里，请刷新')
    elif r['status'] != 'pending':
        raise ValueError('这条规律已处理过')
    if decision == 'confirm':
        for o in confirmed(c):
            if o['direction'] != r['direction'] and rules.rules_overlap(o['dims'], dims_of(r)):
                raise ValueError('这条规律与已生效的「%s」方向相反，会同时适用于同一类货；请先停用那条再确认' % effect(o))
    status = dict(confirm='confirmed', reject='rejected', retire='retired')[decision]
    c.execute('UPDATE learned_rules SET status=?,decided_at=? WHERE id=?', (status, at or now(), rule_id))
    out = dict(id=rule_id, status=status, rescored=0, moved=0)
    if decision == 'confirm' and r['direction'] == 'remove':
        from .store import apply_removal_rule
        out['moved'] = apply_removal_rule(c, dict(dims=dims_of(r), label=r['label']), learn)
    if decision in ('confirm', 'retire') and r['direction'] == 'keep':
        out['rescored'] = _rescore(c, learn)
    return out


# ---------------------------------------------------------------- Store 层入口（后台接口与调度用）

def _cfg(root):
    from .config import learning_for
    return learning_for(root)


_last = {}
_last_lock = threading.Lock()


def _due(store):
    """打开 05 时的归纳节流：同一个库 THROTTLE_SECONDS 秒内只做一次。"""
    with _last_lock:
        key = str(store.path)
        t = time.monotonic()
        if key in _last and t - _last[key] < THROTTLE_SECONDS:
            return False
        _last[key] = t
        return True


def view(store, root, refresh_now=False):
    """05 待审核：refresh_now（打开 05 时）先归纳一次（节流；写锁被占用时跳过本次）。
    参数文件有误或归纳失败只记下原因（refresh_error，中文），照常返回已有条目。
    返回 dict(count, items, active, config{min_count, min_ratio}[, refresh_error])。"""
    out = {}
    try:
        cfg = _cfg(root)
        out['config'] = dict(min_count=cfg['min_count'], min_ratio=cfg['min_ratio'])
    except (OSError, ValueError) as e:
        cfg = None
        out['refresh_error'] = 'config/learning.yaml 有误：%s' % e
    if refresh_now and cfg is not None and _due(store):
        try:
            refresh_store(store, cfg, busy_ms=REFRESH_BUSY_MS)
        except sqlite3.OperationalError as e:
            if 'locked' not in str(e) and 'busy' not in str(e):
                log.exception('规律归纳失败')
                out['refresh_error'] = '规律归纳未完成，请稍后再试'
        except Exception:
            log.exception('规律归纳失败')
            out['refresh_error'] = '规律归纳未完成，请稍后再试'
    with store.connect() as c:
        items = pending(c)
        live = active(c)
    return dict(out, count=len(items), items=items, active=live)


def decide_request(store, rule_id, decision, request_id, root):
    """后台写接口：确认 / 否决 / 停用，带 request_id 幂等（triage_requests；同一 request_id 重放返回首次结果）。"""
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 100:
        raise ValueError('请求编号无效')
    payload = json.dumps(['rule', rule_id, decision], ensure_ascii=False)
    try:
        learn = _cfg(root)
    except (OSError, ValueError):
        learn = None      # 参数文件有误：基础分不计规律加分（store.baseline_learned 只记一次日志）
    with store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        ensure(c)
        prev = c.execute('SELECT * FROM triage_requests WHERE request_id=?', (request_id,)).fetchone()
        if prev:
            if prev['payload'] != payload:
                raise ValueError('重复请求内容不同')
            return json.loads(prev['result'])
        r = decide(c, rule_id, decision, learn)
        result = dict(r, count=pending_count(c))
        c.execute('INSERT INTO triage_requests VALUES(?,?,?,?)', (request_id, payload,
                                                                   json.dumps(result, ensure_ascii=False), now()))
        return result


def run(store, root):
    """调度每轮调用：归纳一次（没变化时跳过）。失败不影响其它结果，只记 learning_status: error 与中文原因。"""
    try:
        cfg = _cfg(root)
    except (OSError, ValueError) as e:
        log.warning('config/learning.yaml 有误，本轮不归纳：%s', e)
        return {'learning_status': 'error', 'learning_error': 'config/learning.yaml 有误：%s' % e}
    try:
        r = refresh_store(store, cfg)
        return {'learning_status': 'ok', 'rules_pending': r['pending'], 'rules_proposed': r['proposed']}
    except Exception:
        log.exception('规律归纳失败（下一轮重试）')
        return {'learning_status': 'error', 'learning_error': '规律归纳未完成，将在下一轮重试'}
