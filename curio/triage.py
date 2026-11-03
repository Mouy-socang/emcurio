"""拣选状态机（AD-26）与货号（AD-7）：拣选状态的唯一写入口。

状态：new（新货）、kept（留下）、removed（去掉）、auto_removed（自动移出）、out_of_pool（移出商品池）。
- 系统（规则归口）只在未经人工决定的货上写状态：入库放入 new / auto_removed，重判时在两者间移动。
- 人工动作：留下、去掉、捞回、移出商品池、放回商品池、撤销上一步。人工事件一经存在，系统不再改状态。
- 每次状态变化追加一条 triage_events；货号在首次进入 kept 时分配，永不释放、永不复用。

本模块的函数都接收一个 sqlite3 连接，在调用方的事务里工作；若连接尚未开启事务，
写操作会先执行 BEGIN IMMEDIATE。调用方自己开事务时须用 BEGIN IMMEDIATE（或已在事务里写过，
即已持有 RESERVED 写锁）。货号分配只在写事务内进行（断言 in_transaction），并由
item_codes.code / item_id 的唯一约束兜底，不会产生重复货号。
`cases` / `market_decisions` 是决定日志，拣选状态以 `triage` 为准；撤销不改写 cases。
"""
import sqlite3
import time
from datetime import datetime, timezone

STATES = ('new', 'kept', 'removed', 'auto_removed', 'out_of_pool')
LABELS = dict(new='新货', kept='留下', removed='去掉', auto_removed='自动移出', out_of_pool='移出商品池')
# 人工动作的合法转换：动作 -> (允许的起始状态, 目标状态)
HUMAN = {
    'keep': (('new', 'removed'), 'kept'),
    'remove': (('new', 'kept', 'auto_removed', 'out_of_pool'), 'removed'),
    'rescue': (('auto_removed',), 'new'),
    'out_of_pool': (('kept',), 'out_of_pool'),
    'back_to_pool': (('out_of_pool',), 'kept'),
}
ACTION_LABELS = dict(keep='留下', remove='去掉', rescue='捞回', out_of_pool='移出商品池', back_to_pool='放回商品池')
UNDOABLE = tuple(HUMAN)
CODE_PREFIX = 'CO-'
BACKLOG_DAYS = 7


class TriageError(ValueError):
    """拣选操作被拒绝（中文说明）。"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def ensure(c):
    c.executescript('''
    CREATE TABLE IF NOT EXISTS triage(
        item_id TEXT PRIMARY KEY, state TEXT NOT NULL, backlog INTEGER NOT NULL DEFAULT 0,
        rescued_at TEXT, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS triage_events(
        id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT NOT NULL, at TEXT NOT NULL,
        old_state TEXT, new_state TEXT NOT NULL, action TEXT NOT NULL, actor TEXT NOT NULL,
        agent_decision TEXT, score REAL, rule_version INTEGER, reason TEXT,
        undoes INTEGER, undone_by INTEGER);
    CREATE TABLE IF NOT EXISTS item_codes(
        seq INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL UNIQUE,
        item_id TEXT NOT NULL UNIQUE, assigned_at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS triage_queue ON triage(state,backlog,rescued_at);
    CREATE INDEX IF NOT EXISTS triage_events_item ON triage_events(item_id,id);
    CREATE INDEX IF NOT EXISTS triage_events_actor ON triage_events(actor,id);
    ''')
    # source：动作来源（review / block / market / cases …）；grp：一起撤销的动作组。
    cols = {r[1] for r in c.execute('PRAGMA table_info(triage_events)')}
    for col, typ in (('source', 'TEXT'), ('grp', 'INTEGER')):
        if col not in cols:
            c.execute('ALTER TABLE triage_events ADD COLUMN %s %s' % (col, typ))


def _begin(c):
    if not c.in_transaction:
        c.execute('BEGIN IMMEDIATE')


def _item(c, iid):
    row = c.execute('SELECT * FROM items WHERE item_id=?', (iid,)).fetchone()
    if not row:
        raise TriageError('商品不存在')
    return row


def system_state(agent_decision):
    """规则归口的落位：判断 skip → 自动移出，其余 → 新货。"""
    return 'auto_removed' if agent_decision == 'skip' else 'new'


def _snapshot(item):
    keys = item.keys()
    return (item['agent_decision'], item['score'] if 'score' in keys else None, item['rule_version'])


def _event(c, iid, old, new, action, actor, item, reason=None, at=None, undoes=None, source=None, grp=None):
    agent, score, version = _snapshot(item)
    cur = c.execute('''INSERT INTO triage_events(item_id,at,old_state,new_state,action,actor,agent_decision,score,
        rule_version,reason,undoes,source,grp) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (iid, at or now(), old, new, action, actor, agent, score, version, reason, undoes, source, grp))
    return cur.lastrowid


def _agent_reason(item):
    """系统落位事件的理由快照：当时的 items.agent_reason（之后会被重判覆盖，事件里的不会）。"""
    return item['agent_reason'] if 'agent_reason' in item.keys() else None


def has_human(c, iid):
    return c.execute("SELECT 1 FROM triage_events WHERE item_id=? AND actor='human' LIMIT 1", (iid,)).fetchone() is not None


def state(c, iid):
    r = c.execute('SELECT state FROM triage WHERE item_id=?', (iid,)).fetchone()
    return r[0] if r else None


def code(c, iid):
    r = c.execute('SELECT code FROM item_codes WHERE item_id=?', (iid,)).fetchone()
    return r[0] if r else None


def _row(c, iid, item=None):
    """取该货的拣选状态；没有记录时按判断结果由系统落位。"""
    s = state(c, iid)
    if s is None:
        item = item or _item(c, iid)
        s = system_state(item['agent_decision'])
        ts = now()
        c.execute('INSERT INTO triage(item_id,state,updated_at) VALUES(?,?,?)', (iid, s, ts))
        _event(c, iid, None, s, 'ingest', 'system', item, reason=_agent_reason(item), at=ts)
    return s


def current(c, iid):
    """取该货当前拣选状态；还没有记录时先由系统按判断结果落位。"""
    _begin(c)
    return _row(c, iid)


def system_place(c, iid):
    """入库或重新判断后调用：未经人工决定时按判断结果放入 new / auto_removed；有人工事件则不改状态。"""
    _begin(c)
    item = _item(c, iid)
    if state(c, iid) is None:
        return _row(c, iid, item)
    cur = state(c, iid)
    if has_human(c, iid):
        return cur
    target = system_state(item['agent_decision'])
    reason = _agent_reason(item)
    if cur != target and cur in ('new', 'auto_removed'):
        ts = now()
        c.execute('UPDATE triage SET state=?,updated_at=? WHERE item_id=?', (target, ts, iid))
        _event(c, iid, cur, target, 'rejudge', 'system', item, reason=reason, at=ts)
        return target
    return cur


def _assign_code(c, iid, at=None):
    existing = code(c, iid)
    if existing:
        return existing
    assert c.in_transaction, '货号必须在写事务内分配'
    seq = c.execute("SELECT seq FROM sqlite_sequence WHERE name='item_codes'").fetchone()
    nxt = (seq[0] if seq else 0) + 1
    value = CODE_PREFIX + '%04d' % nxt
    c.execute('INSERT INTO item_codes(seq,code,item_id,assigned_at) VALUES(?,?,?,?)', (nxt, value, iid, at or now()))
    return value


def transition(c, iid, action, reason=None, at=None, actor='human', source=None, grp=None):
    """执行一个人工动作。返回 dict(state=..., code=..., event=事件id或None)。非法转换抛 TriageError。"""
    if action not in HUMAN:
        raise TriageError('未知的拣选动作')
    _begin(c)
    item = _item(c, iid)
    cur = _row(c, iid, item)
    sources, target = HUMAN[action]
    if cur == target and action in ('keep', 'remove'):
        # 重复留下/去掉：不写事件；再次留下返回原货号。
        return dict(state=cur, code=code(c, iid), event=None)
    if cur not in sources:
        raise TriageError('「%s」状态的货不能%s' % (LABELS[cur], ACTION_LABELS[action]))
    ts = at or now()
    assigned = _assign_code(c, iid, ts) if target == 'kept' else code(c, iid)
    rescued = ts if action == 'rescue' else None
    # 任何人工转换都清除 backlog。
    c.execute('UPDATE triage SET state=?,updated_at=?,backlog=0,rescued_at=COALESCE(?,rescued_at) WHERE item_id=?',
              (target, ts, rescued, iid))
    eid = _event(c, iid, cur, target, action, actor, item, reason, at=ts, source=source, grp=grp)
    return dict(state=target, code=assigned, event=eid)


def keep(c, iid, reason=None, source=None):
    """留下。自动移出的货先捞回再留下、移出商品池的货放回，两步记为同一组，一次撤销全部回退。"""
    _begin(c)
    cur = _row(c, iid)
    if cur == 'out_of_pool':
        return transition(c, iid, 'back_to_pool', reason, source=source)
    if cur == 'auto_removed':
        first = transition(c, iid, 'rescue', reason, source=source)['event']
        c.execute('UPDATE triage_events SET grp=? WHERE id=?', (first, first))
        return transition(c, iid, 'keep', reason, source=source, grp=first)
    return transition(c, iid, 'keep', reason, source=source)


def remove(c, iid, reason=None, at=None, source=None):
    return transition(c, iid, 'remove', reason, at=at, source=source)


def note_probe(c, iid, reason=None, source=None):
    """询市：任何状态下都只记一条人工 probe 事件，不改状态；之后系统重判不再移动这件货。"""
    _begin(c)
    item = _item(c, iid)
    cur = _row(c, iid, item)
    return _event(c, iid, cur, cur, 'probe', 'human', item, reason, source=source)


def rescue(c, iid, reason=None, source=None):
    return transition(c, iid, 'rescue', reason, source=source)


def out_of_pool(c, iid, reason=None):
    return transition(c, iid, 'out_of_pool', reason)


def back_to_pool(c, iid, reason=None):
    return transition(c, iid, 'back_to_pool', reason)


def _later_human(c, ev):
    return c.execute('''SELECT 1 FROM triage_events WHERE item_id=? AND id>? AND actor='human' AND undone_by IS NULL
        AND action NOT IN ('undo','probe') AND (grp IS NULL OR grp IS NOT ?) LIMIT 1''', (ev['item_id'], ev['id'], ev['grp'])).fetchone()


def undo_event(c, ev):
    """撤销指定的人工事件（连同同组事件），回到动作前状态；货号保留。状态已变化时抛 TriageError。"""
    _begin(c)
    if ev['grp'] is not None:
        group = c.execute('SELECT * FROM triage_events WHERE grp=? AND undone_by IS NULL ORDER BY id DESC', (ev['grp'],)).fetchall()
    else:
        group = [ev]
    if state(c, ev['item_id']) != group[0]['new_state'] or any(e['old_state'] is None for e in group) or _later_human(c, group[0]):
        raise TriageError('这件货的状态已变化，不能撤销')
    item = _item(c, ev['item_id'])
    for e in group:
        cur = state(c, e['item_id'])
        ts = now()
        c.execute("UPDATE triage SET state=?,updated_at=?,backlog=0,rescued_at=CASE WHEN ?='rescue' THEN NULL ELSE rescued_at END WHERE item_id=?",
                  (e['old_state'], ts, e['action'], e['item_id']))
        uid = _event(c, e['item_id'], cur, e['old_state'], 'undo', 'human', item, at=ts, undoes=e['id'])
        c.execute('UPDATE triage_events SET undone_by=? WHERE id=?', (uid, e['id']))
    last = group[-1]
    return dict(item_id=last['item_id'], state=last['old_state'], code=code(c, last['item_id']), undid=ev['action'])


def undo(c, iid=None, actions=UNDOABLE):
    """撤销最近一次未撤销的人工动作（可指定某件货），回到动作前状态；货号保留。可连续调用多步。"""
    _begin(c)
    marks = ','.join('?' for _ in actions)
    sql = "SELECT * FROM triage_events WHERE actor='human' AND undone_by IS NULL AND action IN (" + marks + ')'
    args = list(actions)
    if iid is not None:
        sql += ' AND item_id=?'
        args.append(iid)
    ev = c.execute(sql + ' ORDER BY id DESC LIMIT 1', args).fetchone()
    if not ev:
        raise TriageError('没有可撤销的操作')
    return undo_event(c, ev)


def undo_market(c, iid):
    """取消「无关」：只撤销该货最近一次未撤销、来自市场页的去掉；没有则不做任何事。"""
    _begin(c)
    ev = c.execute("""SELECT * FROM triage_events WHERE item_id=? AND actor='human' AND undone_by IS NULL
        AND action IN ('remove','migrate') AND new_state='removed' AND source='market' ORDER BY id DESC LIMIT 1""", (iid,)).fetchone()
    return undo_event(c, ev) if ev else None


def queue(c, limit=100, offset=0):
    """新货队列：捞回的排最前（最近捞回优先），其余按 first_seen 新到旧；不含 backlog。"""
    return [dict(r) for r in c.execute('''SELECT i.*,t.state triage_state,t.rescued_at,
        (SELECT code FROM item_codes k WHERE k.item_id=i.item_id) code
        FROM triage t JOIN items i ON i.item_id=t.item_id
        WHERE t.state='new' AND t.backlog=0 AND i.status='active'
        ORDER BY t.rescued_at IS NULL, t.rescued_at DESC, i.first_seen DESC, i.item_id
        LIMIT ? OFFSET ?''', (limit, offset))]


def auto_removed(c, limit=100, offset=0):
    """「已自动移出」列表：当前状态为 auto_removed 的货，按移出时间新到旧。
    每件含 item_id、title、platform、价格（price=标价、total_price、currency）、url、status、reason、removed_at：
    reason 为该货当前的 items.agent_reason（重判后即为最新理由；当时的快照另记在 triage_events.reason），
    removed_at 为最近一次进入 auto_removed 的事件时间。捞回用 rescue()。"""
    if type(limit) is not int or limit < 1 or type(offset) is not int or offset < 0:
        raise ValueError('分页参数无效：limit 须 ≥1，offset 须 ≥0')
    return [dict(r) for r in c.execute('''SELECT i.item_id,i.title,i.platform,i.listed_price price,i.total_price,
        i.currency,i.url,i.status,i.agent_reason reason,
        COALESCE((SELECT e.at FROM triage_events e WHERE e.item_id=t.item_id AND e.new_state='auto_removed'
                  AND e.old_state IS NOT 'auto_removed' ORDER BY e.id DESC LIMIT 1), t.updated_at) removed_at
        FROM triage t JOIN items i ON i.item_id=t.item_id
        WHERE t.state='auto_removed'
        ORDER BY removed_at DESC, i.item_id
        LIMIT ? OFFSET ?''', (limit, offset))]


def counts(c):
    return dict(c.execute('SELECT state,count(*) FROM triage GROUP BY state').fetchall())


def run(path, fn, attempts=5):
    """在独立连接上以 BEGIN IMMEDIATE 事务执行 fn(c)，数据库被锁时重试。"""
    for n in range(attempts):
        c = sqlite3.connect(path, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            c.execute('PRAGMA busy_timeout=30000')
            c.execute('BEGIN IMMEDIATE')
            result = fn(c)
            c.commit()
            return result
        except sqlite3.OperationalError as e:
            c.rollback()
            if 'locked' not in str(e) or n == attempts - 1:
                raise
            time.sleep(0.05 * (n + 1))
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()


MIN = datetime.min.replace(tzinfo=timezone.utc)


def _ts(value):
    """解析 ISO 时间；空值或无法解析返回 None。"""
    try:
        t = datetime.fromisoformat(value)
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _key(value):
    return _ts(value) or MIN


def migrate(c, at=None):
    """一次性、幂等地把 cases / market_decisions 的人工决定落到拣选状态。返回本次新增的事件与货号数。"""
    _begin(c)
    # 迁移时刻只记一次：重跑沿用首次时刻，backlog 判定不随重跑漂移。
    c.execute('CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY,v TEXT)')
    stamp = c.execute("SELECT v FROM kv WHERE k='triage_migrated_at'").fetchone()
    if stamp:
        at = stamp[0]
    else:
        at = at or now()
        c.execute("INSERT INTO kv VALUES('triage_migrated_at',?)", (at,))
    at = _ts(at)
    if at is None:
        raise TriageError('迁移时刻无效')
    cutoff = at.timestamp() - BACKLOG_DAYS * 86400
    ev0 = c.execute('SELECT count(*) FROM triage_events').fetchone()[0]
    code0 = c.execute('SELECT count(*) FROM item_codes').fetchone()[0]
    # 早期迁移的市场去掉补上来源标记，供「取消无关」识别。
    c.execute("""UPDATE triage_events SET source='market' WHERE action='migrate' AND source IS NULL AND EXISTS(
        SELECT 1 FROM market_decisions md WHERE md.item_id=triage_events.item_id AND md.updated_at=triage_events.at AND md.irrelevant=1)""")
    last_human = {}
    for r in c.execute("SELECT item_id,at FROM triage_events WHERE actor='human'"):
        t = _ts(r[1])
        last_human[r[0]] = max(filter(None, [last_human.get(r[0]), t]), default=None)
    latest = {}
    for r in c.execute('SELECT * FROM cases ORDER BY id'):
        latest[r['item_id']] = r
    market = {r['item_id']: r for r in c.execute(
        'SELECT md.* FROM market_decisions md JOIN items i ON i.item_id=md.item_id AND i.platform=md.platform')}
    decisions = []
    for iid in set(latest) | set(market):
        case, md = latest.get(iid), market.get(iid)
        pick = None
        if md and (case is None or _key(md['updated_at']) >= _key(case['decided_at'])):
            # 市场页的决定较新：无关 → 去掉；取消无关 → 没有人工决定。
            if md['irrelevant'] == 1:
                pick = ('removed', md['updated_at'], md['reason'] or '无关', None, 'market')
        elif case is not None:
            target = dict(want='kept', probe='new', skip='removed', irrelevant='removed').get(case['decision'])
            if target:
                pick = (target, case['decided_at'], case['reason'], case, 'cases')
        if pick:
            decisions.append((_key(pick[1]), iid, pick))
    decisions.sort(key=lambda d: (d[0], d[1]))
    for _, iid, (target, when, reason, case, source) in decisions:
        if not c.execute('SELECT 1 FROM items WHERE item_id=?', (iid,)).fetchone():
            continue
        if iid in last_human:
            # 已有人工事件：只应用比最近一次人工事件更新的决定（如之后再导入的记录），保持幂等。
            seen, t = last_human[iid], _ts(when)
            if t is None or (seen is not None and t <= seen):
                continue
        item = _item(c, iid)
        cur = _row(c, iid, item)
        snap = dict(item)
        if case is not None:
            snap.update(agent_decision=case['agent_decision'], rule_version=case['rule_version'])
        if target == 'kept':
            _assign_code(c, iid, when)
        c.execute('UPDATE triage SET state=?,updated_at=?,backlog=0 WHERE item_id=?', (target, when or now(), iid))
        _event(c, iid, cur, target, 'migrate', 'human', snap, reason, at=when or now(), source=source)
        last_human[iid] = _ts(when)
    # 其余货：补齐系统落位；未经人工、迁移前 7 天以上的新货进 backlog。
    for item in c.execute('SELECT i.* FROM items i LEFT JOIN triage t ON t.item_id=i.item_id WHERE t.item_id IS NULL').fetchall():
        _row(c, item['item_id'], item)
    for r in c.execute("SELECT t.item_id,i.first_seen FROM triage t JOIN items i ON i.item_id=t.item_id WHERE t.state='new' AND t.backlog=0 AND t.rescued_at IS NULL").fetchall():
        seen = _ts(r['first_seen'])
        if r['item_id'] not in last_human and seen is not None and seen.timestamp() < cutoff:
            c.execute('UPDATE triage SET backlog=1 WHERE item_id=?', (r['item_id'],))
    return dict(events=c.execute('SELECT count(*) FROM triage_events').fetchone()[0] - ev0,
                codes=c.execute('SELECT count(*) FROM item_codes').fetchone()[0] - code0)

