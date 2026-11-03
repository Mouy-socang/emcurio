"""行为账本与 GoodCustomer 交接文件（故事 15，CAP-15；AD-9、AD-12、AD-25）。

- 账本：curio.db 的 `behavior_events`（按 event_id 唯一，INSERT OR IGNORE）是行为事件的唯一长期账本；
  不合 behavior_event.v1 的事件原样存入 `behavior_unknown`，照常确认，不阻塞。
- 拉取（`pull`，随 sync.run 每轮）：在 curio.db 同目录锁文件 `curio.db.inbox.lock` 的 `fcntl.flock` 内进行，
  同一时刻只有一个进程；拿不到锁就跳过本轮。每页：GET /admin/inbox → 写入账本并提交（同一事务标记受影响客户待重算）
  → 重算交接文件 → POST /admin/inbox/ack 确认本页最大 inbox_seq。写入失败不确认；失败只写结果字段，下轮重试。
- 交接文件：每个已关联客户一份 `<curio.db 所在目录>/gc_handoff/<GC编号>.json`（正式环境即 `data/gc_handoff/`），
  内容为该客户当前关联的全部账号在账本里的全部事件（按 server_ts、event_id 排序）加按类型的计数；每次整份原子覆盖写，
  不追加；没有任何关联账号时删除。不写入 GoodCustomer 库，也不依赖它是否可用（墨墨 2026-11-03 决定）。
- 待重算的客户记在 `handoff_dirty`（与触发它的写入同一事务）：新事件入账、关联、更正（前后两个客户）、解除；
  重算成功才清掉，写文件失败下次（下一轮拉取或下一次关联变化）再算。
"""
import fcntl
import hashlib
import json
import logging
import os
import re
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import requests

from . import sync

log = logging.getLogger(__name__)

EVENT_TYPES = ('home_open', 'item_open', 'item_dwell', 'filter', 'currency')
HANDOFF_DIR = 'gc_handoff'
HANDOFF_VERSION = 1
PULL_LIMIT = 500
PULL_SECONDS = 20              # 每轮拉取限时（墙钟）
PULL_MAX_RESPONSE = 4_000_000  # 一页 500 条事件的响应上限
KV_ACKED = 'inbox_acked_seq'
KV_PULLED_AT = 'inbox_pulled_at'
KV_ERROR = 'inbox_pull_error'
_GC_ID = re.compile(r'GC[0-9]{6}')
_ACCOUNT_RE = re.compile(r'[0-9a-f]{32}')
_TS_RE = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z')
_CODE_RE = re.compile(r'CO-[0-9]{4,8}')


def ensure(c):
    """建账本表（Store.__init__ 调用）。"""
    c.executescript('''
    CREATE TABLE IF NOT EXISTS behavior_events(
        event_id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL,
        type TEXT NOT NULL,
        co_code TEXT,
        client_ts TEXT NOT NULL,
        server_ts TEXT NOT NULL,
        params TEXT NOT NULL,
        event_schema_version INTEGER NOT NULL,
        inbox_seq INTEGER,
        pulled_at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS behavior_events_account ON behavior_events(account_id,server_ts,event_id);
    CREATE TABLE IF NOT EXISTS behavior_unknown(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        raw_hash TEXT NOT NULL UNIQUE,
        inbox_seq INTEGER,
        raw TEXT NOT NULL,
        error TEXT NOT NULL,
        pulled_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS handoff_dirty(customer_id TEXT PRIMARY KEY, at TEXT NOT NULL);
    ''')


def handoff_dir(store):
    return Path(store.path).parent / HANDOFF_DIR


def handoff_path(store, customer_id):
    if not _GC_ID.fullmatch(customer_id or ''):
        raise ValueError('客户编号无效')
    return handoff_dir(store) / f'{customer_id}.json'


# ---------- 校验（与服务器同一份 behavior_event.v1） ----------

def check_event(event, schema=None):
    """返回错误文字（None = 合格）。先整条按 behavior_event.v1，再按 $defs.<type>。"""
    schema = schema or sync.load_schema('behavior_event', 1)
    if not isinstance(event, dict):
        return '事件应为对象'
    errors = sync.validate(event, schema)
    if not errors:
        sub = schema.get('$defs', {}).get(event['type'])
        errors = sync.validate(event, sub) if sub is not None else ['事件类型不认识']
    if errors:
        return '；'.join(errors)[:300]
    try:
        ok = str(uuid.UUID(event['event_id'])) == event['event_id']
    except ValueError:
        ok = False
    if not ok or not _ACCOUNT_RE.fullmatch(event['account_id']):
        return '编号格式不符'
    if not (_TS_RE.fullmatch(event['client_ts']) and _TS_RE.fullmatch(event['server_ts'])):
        return '时间格式不符'
    if event['co_code'] is not None and not _CODE_RE.fullmatch(event['co_code']):
        return '货号格式不符'
    return None


# ---------- 待重算标记 ----------

def mark_dirty(c, *customer_ids):
    """在调用方事务内标记这些客户的交接文件待重算（None 忽略）。"""
    at = f'{sync.utc_now()} {uuid.uuid4().hex[:8]}'      # 每次标记唯一：重算期间又被标记的不会被误清
    for cid in customer_ids:
        if cid:
            c.execute('INSERT OR REPLACE INTO handoff_dirty(customer_id,at) VALUES(?,?)', (cid, at))


def _customers_of(c, account_ids):
    out = set()
    for aid in account_ids:
        row = c.execute('SELECT customer_id FROM guest_links WHERE account_id=?', (aid,)).fetchone()
        if row:
            out.add(row[0])
    return out


# ---------- 入账 ----------

def ingest(store, items, schema=None):
    """一页收件箱事件写入账本并提交（同一事务标记受影响客户待重算）。items：[{inbox_seq, event}]。
    返回 dict(new, duplicate, unknown)。异常时整页回滚、什么也不写（调用方不确认）。"""
    schema = schema or sync.load_schema('behavior_event', 1)
    at = sync.utc_now()
    new = dup = unknown = 0
    accounts = set()
    with store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        for item in items:
            event, seq = item['event'], item['inbox_seq']
            error = check_event(event, schema)
            if error:
                raw = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
                unknown += c.execute(
                    'INSERT OR IGNORE INTO behavior_unknown(raw_hash,inbox_seq,raw,error,pulled_at) VALUES(?,?,?,?,?)',
                    (hashlib.sha256(raw.encode('utf-8')).hexdigest(), seq, raw, error, at)).rowcount
                continue
            n = c.execute(
                'INSERT OR IGNORE INTO behavior_events(event_id,account_id,type,co_code,client_ts,server_ts,params,'
                'event_schema_version,inbox_seq,pulled_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (event['event_id'], event['account_id'], event['type'], event['co_code'], event['client_ts'],
                 event['server_ts'], json.dumps(event['params'], ensure_ascii=False, sort_keys=True),
                 event['event_schema_version'], seq, at)).rowcount
            if n:
                new += 1
                accounts.add(event['account_id'])
            else:
                dup += 1
        mark_dirty(c, *_customers_of(c, accounts))
    return {'new': new, 'duplicate': dup, 'unknown': unknown}


# ---------- 交接文件 ----------

def handoff_content(c, customer_id):
    """该客户当前关联的全部账号在账本中的全部事件 + 按类型计数。没有关联账号时返回 None。"""
    accts = c.execute('SELECT l.account_id, a.username, a.nickname FROM guest_links l '
                      'LEFT JOIN guest_accounts a ON a.account_id=l.account_id '
                      'WHERE l.customer_id=? ORDER BY l.account_id', (customer_id,)).fetchall()
    if not accts:
        return None
    ids = [r[0] for r in accts]
    marks = ','.join('?' * len(ids))
    rows = c.execute(f'SELECT event_id,account_id,type,co_code,client_ts,server_ts,params,event_schema_version '
                     f'FROM behavior_events WHERE account_id IN ({marks}) ORDER BY server_ts,event_id', ids).fetchall()
    counts = {t: 0 for t in EVENT_TYPES}
    events = []
    for r in rows:
        counts[r[2]] = counts.get(r[2], 0) + 1
        events.append({'event_id': r[0], 'account_id': r[1], 'type': r[2], 'co_code': r[3], 'client_ts': r[4],
                       'server_ts': r[5], 'params': json.loads(r[6]), 'event_schema_version': r[7]})
    return {'handoff_version': HANDOFF_VERSION, 'customer_id': customer_id, 'source': 'curiobject',
            'accounts': [{'account_id': r[0], 'username': r[1], 'nickname': r[2]} for r in accts],
            'summary': {'total': len(events), 'by_type': counts,
                        'first_server_ts': events[0]['server_ts'] if events else None,
                        'last_server_ts': events[-1]['server_ts'] if events else None},
            'events': events}


def _write_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name + '.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def rebuild(store, customer_id):
    """按当前映射从账本整份重算一位客户的交接文件（原子覆盖写；无关联账号时删除文件）。返回 'written' / 'deleted'。"""
    path = handoff_path(store, customer_id)
    with store.connect() as c:
        data = handoff_content(c, customer_id)
    if data is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return 'deleted'
    _write_atomic(path, data)
    return 'written'


@contextmanager
def _handoff_lock(store):
    """重算段的进程间互斥（等待）：后台关联与调度拉取同时重算同一客户时，后写的一定读到更新的账本与映射。"""
    p = Path(store.path)
    fd = os.open(p.with_name(p.name + '.handoff.lock'), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def rebuild_dirty(store):
    """重算全部待重算客户；成功的清掉标记（标记在重算开始后又被更新的保留，下次再算）。返回 (完成数, 错误列表)。"""
    with _handoff_lock(store):
        return _rebuild_dirty(store)


def _rebuild_dirty(store):
    with store.connect() as c:
        dirty = c.execute('SELECT customer_id,at FROM handoff_dirty ORDER BY customer_id').fetchall()
    done, errors = 0, []
    for cid, at in dirty:
        try:
            rebuild(store, cid)
        except Exception as e:      # 写文件失败：保留标记，下次重算
            log.warning('交接文件 %s 未能重算：%s', cid, type(e).__name__)
            errors.append(f'{cid}：{type(e).__name__}')
            continue
        with store.connect() as c:
            c.execute('DELETE FROM handoff_dirty WHERE customer_id=? AND at=?', (cid, at))
        done += 1
    return done, errors


def after_link_change(store):
    """关联 / 更正 / 解除提交后调用（标记已在同一事务写入）：立即重算；失败只记日志，下次再算。"""
    try:
        return rebuild_dirty(store)
    except Exception as e:
        log.warning('交接文件重算未完成（%s）', type(e).__name__)
        return 0, [type(e).__name__]


# ---------- 文件锁 ----------

def lock_path(store):
    p = Path(store.path)
    return p.with_name(p.name + '.inbox.lock')


@contextmanager
def inbox_lock(store):
    """拉取与确认段的进程间互斥（fcntl.flock，不等待）。拿到锁 yield True，被别的进程占着 yield False。"""
    path = lock_path(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ---------- 拉取 → 入账 → 重算 → 确认 ----------

def _check_batch(body, after):
    errors = sync.validate(body, sync.load_schema('inbox_batch', 1)) if isinstance(body, dict) else ['$ 应为 object']
    if errors:
        return '收件箱批次不合契约：' + '；'.join(errors)[:200]
    seqs = [e['inbox_seq'] for e in body['events']]
    if any(s <= after for s in seqs) or seqs != sorted(set(seqs)):
        return '收件箱批次的序号不是递增的'
    return None


KV_STATS = 'inbox_server_stats'


def _note_stats(store, stats, result):
    """服务器累计计数（expired / rate_dropped / rejected）抄进结果字段；比上次记下的增长时记日志（线上丢了数据要看得见）。"""
    if not isinstance(stats, dict):
        return
    for k, v in stats.items():
        result['inbox_server_' + k] = v
    try:
        before = json.loads(store.get(KV_STATS) or '{}')
    except ValueError:
        before = {}
    grown = {k: v - before.get(k, 0) for k, v in stats.items() if v > before.get(k, 0)}
    if grown:
        log.warning('服务器收件箱计数增长（可能有行为数据丢失）：%s', '、'.join(f'{k} +{n}' for k, n in sorted(grown.items())))
        result['inbox_server_grown'] = grown
    store.set(KV_STATS, json.dumps(stats, sort_keys=True))


def pull(store, root=sync.ROOT, session=None, monotonic=time.monotonic, seconds=PULL_SECONDS):
    """拉取收件箱一轮（持文件锁）。返回结果字段：inbox_status ok / busy / error（未配置服务器时 {}）；
    限时用完而服务器还有待拉的页时另有 inbox_more: true。服务器计数抄进 inbox_server_*。
    失败只写结果字段与 kv（inbox_pull_error），不抛异常；已入账的下轮重拉时 INSERT OR IGNORE 不重复。"""
    url, token, _ = sync.server_settings(root)
    if url is None:
        return {}
    session = session or requests.Session()
    with inbox_lock(store) as got:
        if not got:
            return {'inbox_status': 'busy'}
        totals = {'inbox_new': 0, 'inbox_duplicate': 0, 'inbox_unknown': 0, 'inbox_pulled': 0}
        state = {'handoff_errors': [], 'more': False, 'stats': None}
        try:
            error = _pull_pages(store, url, token, session, monotonic, seconds, totals, state)
        except Exception as e:          # 重算、契约文件等意外：同样只记结果字段
            error = f'拉取收件箱失败（{type(e).__name__}）'
        result = dict(totals, inbox_status='error' if error else 'ok')
        if state['more'] and not error:
            result['inbox_more'] = True
        if error:
            log.warning('收件箱未拉完：%s', error)
            result['inbox_error'] = error
            store.set(KV_ERROR, error)
        else:
            store.set(KV_PULLED_AT, sync.utc_now())
            with store.connect() as c:
                c.execute('DELETE FROM kv WHERE k=?', (KV_ERROR,))
        _note_stats(store, state['stats'], result)
        if state['handoff_errors']:
            result['handoff_errors'] = state['handoff_errors'][:20]
        acked = store.get(KV_ACKED)
        if acked:
            result['inbox_acked_seq'] = int(acked)
        return result


def _pull_pages(store, url, token, session, monotonic, seconds, totals, state):
    """逐页 拉取 → 入账 → 重算 → 确认。返回错误文字或 None。"""
    schema = sync.load_schema('behavior_event', 1)
    ack_schema = sync.load_schema('inbox_ack', 1)
    # 上一轮留下的待重算（写文件失败、或入账后进程退出）先算掉；限时从这之后开始算。
    state['handoff_errors'] += rebuild_dirty(store)[1]
    deadline = monotonic() + seconds
    after = 0
    while True:
        if monotonic() > deadline:
            state['more'] = after > 0     # 上一页说"还有"、但限时已到
            return None
        status, body, error = sync._call(session, 'GET', f'{url}/admin/inbox?after={after}&limit={PULL_LIMIT}',
                                         token, sync.REQUEST_TIMEOUT, deadline, monotonic,
                                         max_bytes=PULL_MAX_RESPONSE)
        if error is None and not 200 <= (status or 0) < 300:
            error = sync._http_error(status, body)[0]
        if error is None:
            error = _check_batch(body, after)
        if error:
            return error
        state['stats'] = body.get('stats', state['stats'])
        items, more = body['events'], body['more']
        if not items:
            return None
        try:
            got_n = ingest(store, items, schema)
        except Exception as e:          # 写入失败：不确认，下轮重拉
            return f'行为事件未能写入（{type(e).__name__}）'
        totals['inbox_pulled'] += len(items)
        totals['inbox_new'] += got_n['new']
        totals['inbox_duplicate'] += got_n['duplicate']
        totals['inbox_unknown'] += got_n['unknown']
        state['handoff_errors'] += rebuild_dirty(store)[1]
        max_seq = items[-1]['inbox_seq']
        ack = {'schema_version': 1, 'max_seq': max_seq}
        bad = sync.validate(ack, ack_schema)
        if bad:
            return '收件箱确认不合契约：' + '；'.join(bad)
        status, body, error = sync._call(session, 'POST', url + '/admin/inbox/ack', token, sync.REQUEST_TIMEOUT,
                                         deadline, monotonic, json=ack)
        if error is None and not (200 <= (status or 0) < 300 and isinstance(body, dict) and body.get('ok') is True):
            error = sync._http_error(status, body)[0] if status else '收件箱确认未成功'
        if error:
            return error
        store.set(KV_ACKED, str(max_seq))
        after = max_seq
        if not more:
            return None
