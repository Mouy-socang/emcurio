"""行为收件箱（CAP-15；AD-9、AD-18、AD-25）：客人页上报的行为事件只在这里临时中转，Mac 拉走并确认后删除。

- 上报 `POST /events`（会话鉴权，见 app.py）：一批最多 50 条（behavior_upload.v1）；服务器按会话填 `account_id`、
  入箱时写 `server_ts`，补齐后按 behavior_event.v1（含 `$defs.<type>`）校验整条。枚举外类型等不合格的列入 rejected；
  一条都不合格时返回 400。`item_dwell` 的秒数超过 1800 截为 1800。
- 去重：`event_id` 记在 inbox_seen（保留 30 天），重发同一条（含已被 Mac 确认删掉的）不再入箱。
- 限频：每账号每小时（整点窗口）最多 600 条，超出的丢弃、计数（inbox_stats.rate_dropped）并记日志；客户端不重试。
- 入箱分配单调 `inbox_seq`（AUTOINCREMENT，删除后也不复用）。
- 拉取 `GET /admin/inbox?after=<seq>&limit=500`（inbox_batch.v1）按序返回；确认 `POST /admin/inbox/ack {max_seq}`
  （inbox_ack.v1）只删除 ≤ max_seq 的事件，幂等。
- 未确认的事件保留 30 天：上报与拉取时顺手清理超期的，记日志（线上不长期保存，AD-18）。
- 计数 inbox_stats（expired / rate_dropped / rejected，累计）随每次拉取以 `stats` 返回，Mac 据此发现丢失。
- 所有函数接收调用方的 sqlite3 连接（autocommit 模式），写操作自己开 `BEGIN IMMEDIATE`；时间由调用方传入。
"""
import json
import logging
import re
import uuid
from datetime import datetime, timezone

from jsonschema import Draft202012Validator

log = logging.getLogger('curiobject.server')

BATCH_MAX = 50
RATE_PER_HOUR = 600
RETENTION_SECONDS = 30 * 86400
PULL_MAX = 500
DWELL_MAX = 1800
STAT_KEYS = ('expired', 'rate_dropped', 'rejected')    # 累计计数，随拉取返回给 Mac
SERVER_KEYS = ('account_id', 'server_ts')      # 服务器填写，客户端带了也覆盖
_TS_RE = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z')
_CODE_RE = re.compile(r'CO-[0-9]{4,8}')
_ACCOUNT_RE = re.compile(r'[0-9a-f]{32}')


class BadEvent(Exception):
    """事件不合契约（中文）。"""


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def ensure(c):
    c.executescript('''
    CREATE TABLE IF NOT EXISTS inbox(
        inbox_seq INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE,
        account_id TEXT NOT NULL,
        received REAL NOT NULL,
        event TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS inbox_received ON inbox(received);
    -- 见过的 event_id（含已被 Mac 确认删除的），保留 30 天：重发不再入箱。
    CREATE TABLE IF NOT EXISTS inbox_seen(event_id TEXT PRIMARY KEY, at REAL NOT NULL);
    CREATE INDEX IF NOT EXISTS inbox_seen_at ON inbox_seen(at);
    -- 每账号每小时入箱条数（整点窗口）。
    CREATE TABLE IF NOT EXISTS inbox_rate(account_id TEXT NOT NULL, hour INTEGER NOT NULL, n INTEGER NOT NULL,
        PRIMARY KEY(account_id, hour));
    -- 计数：rate_dropped（限频丢弃）、expired（超 30 天未确认删除）、rejected（不合契约拒收）。
    CREATE TABLE IF NOT EXISTS inbox_stats(k TEXT PRIMARY KEY, n INTEGER NOT NULL);
    ''')


class Checker:
    """按 contract/ 的 behavior_event.v1 校验整条事件，再按 $defs.<type> 校验类型专属部分（与 Mac 同一份定义）。"""

    def __init__(self, contract):
        self.base = contract.validator('behavior_event', 1)
        self.upload = contract.validator('behavior_upload', 1)
        self.batch = contract.validator('inbox_batch', 1)
        self.ack = contract.validator('inbox_ack', 1)
        self.types = {k: Draft202012Validator(v) for k, v in self.base.schema.get('$defs', {}).items()}

    def check(self, event):
        kind = event.get('type') if isinstance(event, dict) else None
        for validator in (self.base, self.types.get(kind) if isinstance(kind, str) else None):
            if validator is None:
                raise BadEvent('事件类型不认识')
            errors = sorted(validator.iter_errors(event), key=lambda e: list(e.absolute_path))
            if errors:
                where = '.'.join(str(p) for p in errors[0].absolute_path) or '（顶层）'
                raise BadEvent(f'事件不合契约：{where} 不符合要求')
        # pattern 用 `$` 会放过结尾换行：再严格全匹配一次（Mac 端同样做）。
        try:
            ok = str(uuid.UUID(event['event_id'])) == event['event_id']
        except ValueError:
            ok = False
        if not ok or not _ACCOUNT_RE.fullmatch(event['account_id']):
            raise BadEvent('事件不合契约：编号不符合要求')
        if not (_TS_RE.fullmatch(event['client_ts']) and _TS_RE.fullmatch(event['server_ts'])):
            raise BadEvent('事件不合契约：时间不符合要求')
        if event['co_code'] is not None and not _CODE_RE.fullmatch(event['co_code']):
            raise BadEvent('事件不合契约：货号不符合要求')

    def complete(self, item, account_id, server_ts):
        """客户端上报的一条 → 完整事件（服务器字段覆盖客户端的；停留秒数超过 1800 截为 1800）。"""
        if not isinstance(item, dict):
            raise BadEvent('事件应为对象')
        event = {k: v for k, v in item.items() if k not in SERVER_KEYS}
        event.setdefault('co_code', None)
        event['account_id'] = account_id
        event['server_ts'] = server_ts
        params = event.get('params')
        if event.get('type') == 'item_dwell' and isinstance(params, dict):
            s = params.get('seconds')
            if type(s) is int and s > DWELL_MAX:
                event['params'] = dict(params, seconds=DWELL_MAX)
        self.check(event)
        return event


def _bump(c, key, n):
    c.execute('INSERT INTO inbox_stats(k,n) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET n=n+excluded.n', (key, n))


def purge(c, now):
    """删除超过 30 天仍未确认的事件与过期的去重记录（在调用方事务内）。返回删除的事件数。"""
    n = c.execute('DELETE FROM inbox WHERE received < ?', (now - RETENTION_SECONDS,)).rowcount
    c.execute('DELETE FROM inbox_seen WHERE at < ?', (now - RETENTION_SECONDS,))
    if n:
        _bump(c, 'expired', n)
        log.warning('收件箱：%d 条事件超过 30 天未被 Mac 确认，已删除', n)
    return n


def receive(c, checker, account_id, items, now):
    """一批上报入箱。返回 dict(accepted, duplicates, dropped, rejected=[event_id…])。"""
    server_ts = iso(now)
    valid, rejected = [], []
    for item in items:
        try:
            valid.append(checker.complete(item, account_id, server_ts))
        except BadEvent:
            eid = item.get('event_id') if isinstance(item, dict) else None
            rejected.append(eid if isinstance(eid, str) and len(eid) <= 64 else None)
    accepted = duplicates = dropped = 0
    hour = int(now // 3600)
    c.execute('BEGIN IMMEDIATE')
    try:
        purge(c, now)
        c.execute('DELETE FROM inbox_rate WHERE hour < ?', (hour - 1,))
        row = c.execute('SELECT n FROM inbox_rate WHERE account_id=? AND hour=?', (account_id, hour)).fetchone()
        used = row[0] if row else 0
        for ev in valid:
            if c.execute('SELECT 1 FROM inbox_seen WHERE event_id=?', (ev['event_id'],)).fetchone():
                duplicates += 1
                continue
            if used >= RATE_PER_HOUR:
                dropped += 1
                continue
            c.execute('INSERT INTO inbox(event_id,account_id,received,event) VALUES(?,?,?,?)',
                      (ev['event_id'], account_id, now,
                       json.dumps(ev, ensure_ascii=False, sort_keys=True, separators=(',', ':'))))
            c.execute('INSERT INTO inbox_seen(event_id,at) VALUES(?,?)', (ev['event_id'], now))
            used += 1
            accepted += 1
        c.execute('INSERT INTO inbox_rate(account_id,hour,n) VALUES(?,?,?) '
                  'ON CONFLICT(account_id,hour) DO UPDATE SET n=excluded.n', (account_id, hour, used))
        if dropped:
            _bump(c, 'rate_dropped', dropped)
        if rejected:
            _bump(c, 'rejected', len(rejected))
        c.execute('COMMIT')
    except Exception:
        if c.in_transaction:
            c.execute('ROLLBACK')
        raise
    if dropped:
        log.warning('收件箱：账号 %s… 本小时超过 %d 条，丢弃 %d 条', account_id[:6], RATE_PER_HOUR, dropped)
    if rejected:
        log.warning('收件箱：账号 %s… 有 %d 条事件不合契约，已拒收', account_id[:6], len(rejected))
    return {'accepted': accepted, 'duplicates': duplicates, 'dropped': dropped, 'rejected': rejected}


def record(c, checker, account_id, type_, params, now):
    """服务器自己记一条事件（如 POST /currency 的币种切换：不跑脚本的表单提交也要记）。event_id 由服务器生成。"""
    item = {'event_id': str(uuid.uuid4()), 'type': type_, 'co_code': None, 'client_ts': iso(now),
            'params': params, 'event_schema_version': 1}
    return receive(c, checker, account_id, [item], now)


def pull(c, after, limit, now):
    """inbox_batch.v1：inbox_seq > after 的前 limit 条（升序）。顺手清理超期事件。"""
    c.execute('BEGIN IMMEDIATE')
    try:
        purge(c, now)
        c.execute('COMMIT')
    except Exception:
        if c.in_transaction:
            c.execute('ROLLBACK')
        raise
    rows = c.execute('SELECT inbox_seq,event FROM inbox WHERE inbox_seq > ? ORDER BY inbox_seq LIMIT ?',
                     (after, limit + 1)).fetchall()
    st = stats(c)
    return {'schema_version': 1, 'events': [{'inbox_seq': r[0], 'event': json.loads(r[1])} for r in rows[:limit]],
            'more': len(rows) > limit,
            'stats': {k: st.get(k, 0) for k in STAT_KEYS}}


def ack(c, max_seq):
    """删除 inbox_seq ≤ max_seq 的事件（幂等）。返回删除条数。"""
    return c.execute('DELETE FROM inbox WHERE inbox_seq <= ?', (max_seq,)).rowcount


def stats(c):
    return dict(c.execute('SELECT k,n FROM inbox_stats').fetchall())
