"""Mac → 服务器的唯一出口：持久发件箱 + 按序发送（AD-3、AD-19、AD-21、AD-23）。

- 写入方只调用 `enqueue(c, kind, payload)`，与触发它的状态变更在同一事务里（本函数不提交）。
- `flush()` 按 seq 顺序逐条 POST 到 `<CURIO_SERVER_URL>/admin/events`（Bearer 管理密钥），
  遇失败即停、不跳过，下次从同一条重试；服务器 2xx 且回显 event_id 才写 sent_at。
- 永久失败（服务器 400/409/413、本机契约校验不过、库里载荷坏掉）把该条标 `failed_at` 并停住（不跳过，顺序要紧），
  `sync_status: blocked`，通知点名这一条；由 `scripts/outbox.py requeue|drop` 人工处理。
- 发送前后都按 `contract/schemas/` 校验（标准库最小校验器，只支持这些 Schema 用到的关键字）。
- 连续 3 次 flush 失败（含意外异常）发一次 macOS 通知；只有 ok 清零，timeout 不改计数。
- 管理密钥只从 .env 读取，不进日志、错误信息或数据库。
"""
import json
import logging
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import requests

from ..config import ROOT, _env

log = logging.getLogger(__name__)

SCHEMA_DIR = ROOT / 'contract' / 'schemas'
ENVELOPE_VERSION = 1
FLUSH_SECONDS = 30           # 每轮 flush 限时（墙钟）；超时的下轮继续
REQUEST_TIMEOUT = 10         # 单次请求上限（仍受整轮时限约束）
MAX_RESPONSE = 65536         # 确认响应最多读这么多字节
FAIL_NOTIFY_AFTER = 3        # 连续失败几次后通知
PERMANENT_STATUS = {400, 409, 413}
KV_FAIL_STREAK = 'sync_fail_streak'
KV_NOTIFIED = 'sync_fail_notified'
_KIND_RE = re.compile(r'^[a-z][a-z0-9_-]{0,63}$')
_CREATED_RE = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z')


class ContractError(ValueError):
    """载荷或 Schema 不符合契约（信息为中文，不含密钥）。"""


# ---------- 最小 JSON Schema 校验器 ----------

_TYPES = {
    'object': lambda v: isinstance(v, dict),
    'array': lambda v: isinstance(v, list),
    'string': lambda v: isinstance(v, str),
    'integer': lambda v: isinstance(v, int) and not isinstance(v, bool),
    'number': lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    'boolean': lambda v: isinstance(v, bool),
    'null': lambda v: v is None,
}
# 只描述、不约束的关键字。
_ANNOTATIONS = {'$schema', '$id', 'title', 'description'}
_SUPPORTED = _ANNOTATIONS | {'type', 'required', 'properties', 'additionalProperties',
                             'const', 'enum', 'pattern', 'minLength', 'maxLength'}


def check_schema(schema, path='$'):
    """预先检查整棵 Schema（含 properties、additionalProperties）只用支持的关键字与写法；不合格抛 ContractError。"""
    if not isinstance(schema, dict):
        raise ContractError(f'Schema {path} 应为对象')
    unknown = set(schema) - _SUPPORTED
    if unknown:
        raise ContractError(f'Schema {path} 用到校验器不支持的关键字：' + '、'.join(sorted(unknown)))
    if 'type' in schema and (not isinstance(schema['type'], str) or schema['type'] not in _TYPES):
        raise ContractError(f'Schema {path} 的 type 不受支持')
    if 'required' in schema and (not isinstance(schema['required'], list)
                                 or not all(isinstance(k, str) for k in schema['required'])):
        raise ContractError(f'Schema {path} 的 required 应为文字列表')
    if 'enum' in schema and not isinstance(schema['enum'], list):
        raise ContractError(f'Schema {path} 的 enum 应为列表')
    for key in ('minLength', 'maxLength'):
        if key in schema and (type(schema[key]) is not int or schema[key] < 0):
            raise ContractError(f'Schema {path} 的 {key} 应为非负整数')
    if 'pattern' in schema:
        try:
            re.compile(schema['pattern'])
        except (re.error, TypeError):
            raise ContractError(f'Schema {path} 的 pattern 无效') from None
    props = schema.get('properties', {})
    if not isinstance(props, dict):
        raise ContractError(f'Schema {path} 的 properties 应为对象')
    for key, sub in props.items():
        check_schema(sub, f'{path}.{key}')
    extra = schema.get('additionalProperties', True)
    if isinstance(extra, dict):
        check_schema(extra, f'{path}.*')
    elif not isinstance(extra, bool):
        raise ContractError(f'Schema {path} 的 additionalProperties 应为布尔或对象')


def _same(a, b):
    """JSON 意义上的相等：1 与 true 不同。"""
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    return a == b


def _validate(instance, schema, path):
    errors = []
    t = schema.get('type')
    if t is not None and not _TYPES[t](instance):
        return [f'{path} 应为 {t}']
    if 'const' in schema and not _same(instance, schema['const']):
        errors.append(f'{path} 应为 {json.dumps(schema["const"], ensure_ascii=False)}')
    if 'enum' in schema and not any(_same(instance, v) for v in schema['enum']):
        errors.append(f'{path} 不在允许的取值内')
    if isinstance(instance, str):
        if 'minLength' in schema and len(instance) < schema['minLength']:
            errors.append(f'{path} 太短')
        if 'maxLength' in schema and len(instance) > schema['maxLength']:
            errors.append(f'{path} 超过 {schema["maxLength"]} 字')
        if 'pattern' in schema and not re.search(schema['pattern'], instance):
            errors.append(f'{path} 格式不符')
    if isinstance(instance, dict):
        props = schema.get('properties', {})
        for key in schema.get('required', []):
            if key not in instance:
                errors.append(f'{path} 缺少 {key}')
        extra = schema.get('additionalProperties', True)
        for key, value in instance.items():
            if key in props:
                errors.extend(_validate(value, props[key], f'{path}.{key}'))
            elif extra is False:
                errors.append(f'{path} 不允许字段 {key}')
            elif isinstance(extra, dict):
                errors.extend(_validate(value, extra, f'{path}.{key}'))
    return errors


def validate(instance, schema, path='$'):
    """返回错误列表（空列表 = 通过）。先检查整棵 Schema，用到不支持的关键字立即抛 ContractError。"""
    check_schema(schema)
    return _validate(instance, schema, path)


def load_schema(name, version, schema_dir=None):
    """读取并检查 `<name>.v<version>.schema.json`；名称、版本不认识或文件坏掉时抛 ContractError。"""
    if not isinstance(name, str) or not _KIND_RE.match(name):
        raise ContractError('未知的载荷类别')
    if type(version) is not int or version < 1:
        raise ContractError(f'{name} 的 schema_version 必须为正整数')
    path = Path(schema_dir or SCHEMA_DIR) / f'{name}.v{version}.schema.json'
    try:
        schema = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        raise ContractError(f'不认识的 {name} schema_version：{version}') from None
    except (OSError, ValueError, RecursionError) as e:
        raise ContractError(f'{path.name} 无法读取（{type(e).__name__}）') from None
    check_schema(schema)
    return schema


def _strict_envelope(envelope):
    """Schema 的 pattern 用 `$`，会放过结尾换行；这里再严格全匹配一次（服务器同样做）。"""
    eid = envelope['event_id']
    try:
        ok = str(uuid.UUID(eid)) == eid
    except ValueError:
        ok = False
    if not ok:
        raise ContractError('信封不合契约：$.event_id 格式不符')
    if not _CREATED_RE.fullmatch(envelope['created_at']):
        raise ContractError('信封不合契约：$.created_at 格式不符')


def check_envelope(envelope, schema_dir=None):
    """按契约校验信封与其 payload；不合格抛 ContractError（中文）。"""
    if not isinstance(envelope, dict):
        raise ContractError('信封应为对象')
    errors = validate(envelope, load_schema('envelope', envelope.get('schema_version'), schema_dir))
    if errors:
        raise ContractError('信封不合契约：' + '；'.join(errors))
    _strict_envelope(envelope)
    payload = envelope['payload']
    errors = validate(payload, load_schema(envelope['kind'], payload.get('schema_version'), schema_dir))
    if errors:
        raise ContractError(f'{envelope["kind"]} 载荷不合契约：' + '；'.join(errors))


# ---------- 发件箱 ----------

def ensure(c):
    """建发件箱表（Store.__init__ 调用）。"""
    c.execute('''CREATE TABLE IF NOT EXISTS outbox(
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE,
        kind TEXT NOT NULL,
        payload TEXT NOT NULL,
        created_at TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        sent_at TEXT,
        failed_at TEXT)''')
    if 'failed_at' not in {r[1] for r in c.execute('PRAGMA table_info(outbox)')}:
        try:
            c.execute('ALTER TABLE outbox ADD COLUMN failed_at TEXT')
        except Exception as e:  # 另一进程已补列
            if 'duplicate column' not in str(e):
                raise
    c.execute('CREATE INDEX IF NOT EXISTS outbox_pending ON outbox(sent_at,seq)')


def utc_now():
    """UTC，ISO 8601 带 Z，精确到毫秒（跨机时间约定）。"""
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def _envelope(event_id, kind, payload, created_at):
    return {'schema_version': ENVELOPE_VERSION, 'event_id': event_id, 'kind': kind,
            'created_at': created_at, 'payload': payload}


def enqueue(c, kind, payload):
    """校验后写入发件箱，返回 event_id。不提交：与调用方的状态变更同一事务。坏载荷抛 ContractError、不入队。"""
    event_id = str(uuid.uuid4())
    created_at = utc_now()
    check_envelope(_envelope(event_id, kind, payload, created_at))
    c.execute('INSERT INTO outbox(event_id,kind,payload,created_at) VALUES(?,?,?,?)',
              (event_id, kind, json.dumps(payload, ensure_ascii=False, sort_keys=True), created_at))
    return event_id


def pending(c):
    return c.execute('SELECT count(*) FROM outbox WHERE sent_at IS NULL').fetchone()[0]


# ---------- 服务器设置与通知 ----------

def server_settings(root=ROOT):
    """返回 (url, token, note)。url 为 None 表示未启用；note 为未启用的原因（不含密钥）。"""
    values = _env(root, ('CURIO_SERVER_URL', 'CURIO_ADMIN_TOKEN')) or {}
    url = (values.get('CURIO_SERVER_URL') or '').rstrip('/')
    token = values.get('CURIO_ADMIN_TOKEN') or ''
    if not url:
        return None, None, None
    if not token:
        return None, None, '.env 缺少 CURIO_ADMIN_TOKEN'
    parts = urlsplit(url)
    local = parts.hostname in ('127.0.0.1', 'localhost', '::1')
    # 密钥只经 HTTPS 发出；本机测试服务器例外。
    if parts.scheme != 'https' and not (parts.scheme == 'http' and local):
        return None, None, 'CURIO_SERVER_URL 必须为 https 地址'
    return url, token, None


def _notify(message):
    """macOS 系统通知；失败只记日志（AD-19、Open Question 17）。"""
    script = 'display notification %s with title "Curiobject"' % json.dumps(message, ensure_ascii=False)
    try:
        subprocess.run(['osascript', '-e', script], timeout=10, check=False, capture_output=True)
    except Exception as e:  # 没有 osascript 等
        log.warning('同步失败通知未发出（%s）', type(e).__name__)


def _record_round(store, status):
    """连续失败计数：error 加一（第 3 次通知一次），ok 清零，其它（timeout、blocked）不变。返回当前计数。"""
    notify = False
    with store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        if status == 'ok':
            c.execute('DELETE FROM kv WHERE k IN (?,?)', (KV_FAIL_STREAK, KV_NOTIFIED))
            return 0
        row = c.execute('SELECT v FROM kv WHERE k=?', (KV_FAIL_STREAK,)).fetchone()
        streak = int(row[0]) if row else 0
        if status != 'error':
            return streak
        streak += 1
        c.execute('INSERT OR REPLACE INTO kv VALUES(?,?)', (KV_FAIL_STREAK, str(streak)))
        notify = streak >= FAIL_NOTIFY_AFTER and not c.execute('SELECT 1 FROM kv WHERE k=?', (KV_NOTIFIED,)).fetchone()
        if notify:
            c.execute('INSERT OR REPLACE INTO kv VALUES(?,?)', (KV_NOTIFIED, utc_now()))
    if notify:
        _notify(f'同步到服务器已连续 {streak} 次失败，发件箱会自动重试；请检查网络或服务器。')
    return streak


# ---------- 发送 ----------

def _post(session, url, token, envelope, timeout, deadline, monotonic):
    """发一条；返回 (错误或 None, 是否永久失败)。错误为中文、不含密钥。读响应时按墙钟 deadline 截止。"""
    try:
        r = session.post(url + '/admin/events', json=envelope, timeout=timeout, stream=True,
                         headers={'Authorization': 'Bearer ' + token})
    except requests.RequestException as e:
        return f'连接服务器失败（{type(e).__name__}）', False
    chunks, size = [], 0
    try:
        for chunk in r.iter_content(4096):
            if monotonic() > deadline:
                return '服务器响应超时', False
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_RESPONSE:
                return '服务器响应过大', False
    except requests.RequestException as e:
        return f'读取服务器响应失败（{type(e).__name__}）', False
    finally:
        r.close()
    try:
        body = json.loads(b''.join(chunks))
    except (ValueError, RecursionError):
        body = None
    if not 200 <= r.status_code < 300:
        detail = ''
        if isinstance(body, dict) and isinstance(body.get('detail'), str):
            detail = '：' + body['detail'][:200]
        return f'服务器返回 {r.status_code}{detail}', r.status_code in PERMANENT_STATUS
    if not isinstance(body, dict) or body.get('event_id') != envelope['event_id']:
        return '服务器确认无效（未回显 event_id）', False
    return None, False


def _post_with_deadline(session, url, token, envelope, deadline, monotonic):
    """在后台线程发送，主线程按墙钟截止；超时就放弃等待（服务器按 event_id 去重，晚到的重复无害）。"""
    left = deadline - monotonic()
    box = {}

    def work():
        try:
            box['r'] = _post(session, url, token, envelope, min(REQUEST_TIMEOUT, left), deadline, monotonic)
        except Exception as e:
            box['r'] = (f'发送失败（{type(e).__name__}）', False)
    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(max(0.0, left))
    if t.is_alive():
        return '服务器响应超时', False
    return box['r']


def _block_message(seq, kind, error):
    return f'发件箱第 {seq} 条（{kind}）被拒收，已暂停同步：{error[:120]}。请用 scripts/outbox.py 处理。'


def flush(store, root=ROOT, seconds=FLUSH_SECONDS, session=None, monotonic=time.monotonic):
    """按 seq 顺序发送未确认的发件箱记录。返回 sync_status 等结果字段。

    sync_status：disabled（未配置）、ok（全部送达）、error（暂时失败，下次从它重试）、
    blocked（某条永久失败，需人工处理）、timeout（时限到，下轮继续）。"""
    url, token, note = server_settings(root)
    if url is None:
        with store.connect() as c:
            result = {'sync_status': 'disabled', 'sync_pending': pending(c)}
        if note:
            result['sync_error'] = note
        return result
    session = session or requests.Session()
    deadline = monotonic() + seconds
    sent, error, status, blocked_seq = 0, None, 'ok', None
    while True:
        with store.connect() as c:
            row = c.execute('SELECT seq,event_id,kind,payload,created_at,failed_at,last_error FROM outbox '
                            'WHERE sent_at IS NULL ORDER BY seq LIMIT 1').fetchone()
        if row is None:
            break
        if row['failed_at']:   # 已判永久失败：停在这里，不跳过
            status, error, blocked_seq = 'blocked', row['last_error'] or '已被拒收', row['seq']
            break
        if deadline - monotonic() <= 1:
            status = 'timeout'
            break
        permanent = False
        try:
            envelope = _envelope(row['event_id'], row['kind'], json.loads(row['payload']), row['created_at'])
            check_envelope(envelope)
        except ContractError as e:
            error, permanent = str(e), True
        except (ValueError, RecursionError) as e:
            error, permanent = f'库内载荷损坏（{type(e).__name__}）', True
        else:
            error, permanent = _post_with_deadline(session, url, token, envelope, deadline, monotonic)
        with store.connect() as c:
            if error:
                c.execute('UPDATE outbox SET attempts=attempts+1,last_error=?,failed_at=? WHERE seq=?',
                          (error, utc_now() if permanent else None, row['seq']))
            else:
                c.execute('UPDATE outbox SET attempts=attempts+1,last_error=NULL,sent_at=? WHERE seq=?',
                          (utc_now(), row['seq']))
        if error:
            status = 'blocked' if permanent else 'error'
            if permanent:
                blocked_seq = row['seq']
                _notify(_block_message(row['seq'], row['kind'], error))
            break
        sent += 1
    streak = _record_round(store, status)
    with store.connect() as c:
        result = {'sync_status': status, 'sync_sent': sent, 'sync_pending': pending(c)}
    if error:
        result['sync_error'] = error
        result['sync_fail_streak'] = streak
        if blocked_seq is not None:
            result['sync_blocked_seq'] = blocked_seq
        log.warning('同步未完成：%s', error)
    return result


def run(store, root=ROOT):
    """采集轮末尾调用：限时 flush，任何意外都只变成 sync_status: error 并计入连续失败。"""
    try:
        return flush(store, root)
    except Exception as e:
        log.warning('同步未完成（%s）', type(e).__name__)
        result = {'sync_status': 'error', 'sync_error': f'同步未完成（{type(e).__name__}）'}
        try:
            result['sync_fail_streak'] = _record_round(store, 'error')
        except Exception:
            pass
        return result
