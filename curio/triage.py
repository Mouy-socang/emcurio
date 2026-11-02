"""拣选状态机（AD-26）与货号（AD-7）：拣选状态的唯一写入口。

状态：new（新货）、kept（留下）、removed（去掉）、auto_removed（自动移出）、out_of_pool（移出商品池）。
- 系统（规则归口）只在未经人工决定的货上写状态：入库放入 new / auto_removed，重判时在两者间移动。
- 人工动作：留下、去掉、捞回、移出商品池、放回商品池、撤销上一步。人工事件一经存在，系统不再改状态。
- 每次状态变化追加一条 triage_events；货号在首次进入 kept 时分配，永不释放、永不复用。

本模块的函数都接收一个 sqlite3 连接，在调用方的事务里工作；若连接尚未开启事务，
写操作会先执行 BEGIN IMMEDIATE（货号分配必须在 BEGIN IMMEDIATE 事务内完成）。
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


def _event(c, iid, old, new, action, actor, item, reason=None, at=None, undoes=None):
    agent, score, version = _snapshot(item)
    cur = c.execute('''INSERT INTO triage_events(item_id,at,old_state,new_state,action,actor,agent_decision,score,
        rule_version,reason,undoes) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
        (iid, at or now(), old, new, action, actor, agent, score, version, reason, undoes))
    return cur.lastrowid


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
        _event(c, iid, None, s, 'ingest', 'system', item, at=ts)
    return s


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
    if cur != target and cur in ('new', 'auto_removed'):
        ts = now()
        c.execute('UPDATE triage SET state=?,updated_at=? WHERE item_id=?', (target, ts, iid))
        _event(c, iid, cur, target, 'rejudge', 'system', item, at=ts)
        return target
    return cur


def _assign_code(c, iid, at=None):
    existing = code(c, iid)
    if existing:
        return existing
    seq = c.execute("SELECT seq FROM sqlite_sequence WHERE name='item_codes'").fetchone()
    nxt = (seq[0] if seq else 0) + 1
    value = CODE_PREFIX + '%04d' % nxt
    c.execute('INSERT INTO item_codes(seq,code,item_id,assigned_at) VALUES(?,?,?,?)', (nxt, value, iid, at or now()))
    return value


def transition(c, iid, action, reason=None, at=None, actor='human'):
    """执行一个人工动作。返回 dict(state=..., code=...)。非法转换抛 TriageError。"""
    if action not in HUMAN:
        raise TriageError('未知的拣选动作')
    _begin(c)
    item = _item(c, iid)
    cur = _row(c, iid, item)
    sources, target = HUMAN[action]
    if cur == target and action in ('keep', 'remove'):
        # 重复留下/去掉：不写事件；再次留下返回原货号。
        return dict(state=cur, code=code(c, iid))
    if cur not in sources:
        raise TriageError('「%s」状态的货不能%s' % (LABELS[cur], ACTION_LABELS[action]))
    ts = at or now()
    assigned = _assign_code(c, iid, ts) if target == 'kept' else code(c, iid)
    rescued = ts if action == 'rescue' else None
    c.execute('UPDATE triage SET state=?,updated_at=?,backlog=CASE WHEN ? IS NULL THEN backlog ELSE 0 END,rescued_at=COALESCE(?,rescued_at) WHERE item_id=?',
              (target, ts, rescued, rescued, iid))
    _event(c, iid, cur, target, action, actor, item, reason, at=ts)
    return dict(state=target, code=assigned)


def keep(c, iid, reason=None):
    return transition(c, iid, 'keep', reason)


def remove(c, iid, reason=None):
    return transition(c, iid, 'remove', reason)


def rescue(c, iid, reason=None):
    return transition(c, iid, 'rescue', reason)


def out_of_pool(c, iid, reason=None):
    return transition(c, iid, 'out_of_pool', reason)


def back_to_pool(c, iid, reason=None):
    return transition(c, iid, 'back_to_pool', reason)


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
    cur = state(c, ev['item_id'])
    if cur != ev['new_state'] or ev['old_state'] is None:
        raise TriageError('这件货的状态已变化，不能撤销')
    item = _item(c, ev['item_id'])
    ts = now()
    c.execute('UPDATE triage SET state=?,updated_at=?,rescued_at=CASE WHEN ?=\'rescue\' THEN NULL ELSE rescued_at END WHERE item_id=?',
              (ev['old_state'], ts, ev['action'], ev['item_id']))
    uid = _event(c, ev['item_id'], cur, ev['old_state'], 'undo', 'human', item, at=ts, undoes=ev['id'])
    c.execute('UPDATE triage_events SET undone_by=? WHERE id=?', (uid, ev['id']))
    return dict(item_id=ev['item_id'], state=ev['old_state'], code=code(c, ev['item_id']), undid=ev['action'])


def queue(c, limit=100, offset=0):
    """新货队列：捞回的排最前（最近捞回优先），其余按 first_seen 新到旧；不含 backlog。"""
    return [dict(r) for r in c.execute('''SELECT i.*,t.state triage_state,t.rescued_at,
        (SELECT code FROM item_codes k WHERE k.item_id=i.item_id) code
        FROM triage t JOIN items i ON i.item_id=t.item_id
        WHERE t.state='new' AND t.backlog=0
        ORDER BY t.rescued_at IS NULL, t.rescued_at DESC, i.first_seen DESC, i.item_id
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


def _ts(value):
    try:
        t = datetime.fromisoformat(value)
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)


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
    cutoff = at.timestamp() - BACKLOG_DAYS * 86400
    ev0 = c.execute('SELECT count(*) FROM triage_events').fetchone()[0]
    code0 = c.execute('SELECT count(*) FROM item_codes').fetchone()[0]
    human = {r[0] for r in c.execute("SELECT DISTINCT item_id FROM triage_events WHERE actor='human'")}
    latest = {}
    for r in c.execute('SELECT * FROM cases ORDER BY id'):
        latest[r['item_id']] = r
    market = {r['item_id']: r for r in c.execute(
        'SELECT md.* FROM market_decisions md JOIN items i ON i.item_id=md.item_id AND i.platform=md.platform')}
    decisions = []
    for iid in set(latest) | set(market):
        case, md = latest.get(iid), market.get(iid)
        pick = None
        if md and (case is None or _ts(md['updated_at']) >= _ts(case['decided_at'])) and md['irrelevant'] == 1:
            pick = ('removed', md['updated_at'], md['reason'] or '无关', None)
        elif case is not None:
            target = dict(want='kept', probe='new', skip='removed', irrelevant='removed').get(case['decision'])
            if target:
                pick = (target, case['decided_at'], case['reason'], case)
        if pick:
            decisions.append((pick[1] or '', iid, pick))
    decisions.sort()
    for _, iid, (target, when, reason, case) in decisions:
        if iid in human or not c.execute('SELECT 1 FROM items WHERE item_id=?', (iid,)).fetchone():
            continue
        item = _item(c, iid)
        cur = _row(c, iid, item)
        snap = dict(item)
        if case is not None:
            snap.update(agent_decision=case['agent_decision'], rule_version=case['rule_version'])
        if target == 'kept':
            _assign_code(c, iid, when)
        c.execute('UPDATE triage SET state=?,updated_at=? WHERE item_id=?', (target, when or now(), iid))
        _event(c, iid, cur, target, 'migrate', 'human', snap, reason, at=when or now())
        human.add(iid)
    # 其余货：补齐系统落位；未经人工、迁移前 7 天以上的新货进 backlog。
    for item in c.execute('SELECT i.* FROM items i LEFT JOIN triage t ON t.item_id=i.item_id WHERE t.item_id IS NULL').fetchall():
        _row(c, item['item_id'], item)
    for r in c.execute("SELECT t.item_id,i.first_seen FROM triage t JOIN items i ON i.item_id=t.item_id WHERE t.state='new' AND t.backlog=0 AND t.rescued_at IS NULL").fetchall():
        if r['item_id'] not in human and _ts(r['first_seen']).timestamp() < cutoff:
            c.execute('UPDATE triage SET backlog=1 WHERE item_id=?', (r['item_id'],))
    return dict(events=c.execute('SELECT count(*) FROM triage_events').fetchone()[0] - ev0,
                codes=c.execute('SELECT count(*) FROM item_codes').fetchone()[0] - code0)

