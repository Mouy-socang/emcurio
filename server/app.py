"""Curiobject 香港服务器：管理接口（AD-3、AD-19、AD-21、AD-23、AD-24）与客人账号页面（AD-8、AD-18，见 guest.py）。

- `GET /healthz`：无鉴权，只返回 ok 与版本。
- `POST /admin/events`：`Authorization: Bearer <管理密钥>`；按 contract/ 的 JSON Schema 校验信封与载荷，
  按 event_id 去重——重复请求返回第一次的确认，不重复处理。
- 只监听 127.0.0.1，由 Caddy 反代并签 HTTPS 证书；服务器从不主动连接 Mac。
- 设置只来自环境变量（systemd EnvironmentFile）：CURIO_ADMIN_TOKEN、CURIO_SERVER_DB、CURIO_CONTRACT_DIR、
  CURIO_PUBLIC_URL（重设链接用的站点地址）、CURIO_COOKIE_SECURE（默认 1；本机 http 测试设 0）、
  CURIO_MAILER / CURIO_MAIL_DIR（见 mailer.py）、CURIO_SERVER_SECRET（HMAC 密钥；不设时在库所在目录生成 server_secret）。
- `GET /admin/accounts`：同一 Bearer 鉴权；账号名单（accounts_list.v1：账号编号、用户名、微信名/称呼、状态、注册时间、
  account_rev，不含邮箱与任何哈希），Mac 每轮同步与打开后台 06 时拉取（故事 14）。
- 已处理的载荷：ping（只存下）、set_invite_code（AD-24 指令：设置通用邀请码，服务器只存其哈希）、
  disable_account / reset_password（AD-24 指令：停用账号、重置密码；同一 command_id 只执行一次，target_seq 按账号单调，
  两者都使该账号全部会话与重设链接失效；临时密码只存哈希，events 表里存 HMAC）、
  snapshot（AD-5、AD-22：整份原子替换，编号 ≤ 当前的不替换，缺图拒收）、hide（含 unhide，按 hide_seq 与水位合并）、
  heartbeat（AD-10：已发布货下架检查最近成功时间；只留最新一份，不存入 events 表——每轮都发，不是事件）。
- 行为收件箱（故事 15，AD-9、AD-25，见 inbox.py）：客人页 `POST /events`（会话鉴权，一批最多 50 条）；
  Mac `GET /admin/inbox?after=<seq>&limit=500` 拉取、`POST /admin/inbox/ack {max_seq}` 确认后删除。
- 图片（AD-13）：`POST /admin/images/check` 查哪些还没有，`PUT /admin/images/<name>` 上传（核对内容哈希），
  客人经会话鉴权的 `/img/<name>` 读取（guest.py）。
"""
import argparse
import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from jsonschema import Draft202012Validator
from starlette.concurrency import run_in_threadpool

import accounts
import guest
import inbox
import snapshot

VERSION = '0.2.0'
HOST = '127.0.0.1'
DEFAULT_PORT = 8790
MAX_BODY = 9_000_000          # 快照可达数 MB；Caddy request_body 10MB 与之配套
HERE = Path(__file__).resolve().parent
DEFAULT_CONTRACT = HERE.parent / 'contract' / 'schemas'
DEFAULT_DB = HERE / 'data' / 'server.db'
_NAME_RE = re.compile(r'^[a-z][a-z0-9_-]{0,63}$')
_DIGITS_RE = re.compile(r'[0-9]{1,19}')   # 只认 ASCII 数字（Unicode 数字如 '²' 不算，int() 也不认）
_CREATED_RE = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z')

log = logging.getLogger('curiobject.server')


class BadPayload(Exception):
    """载荷不合契约（信息为中文）。"""


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def connect(path):
    c = sqlite3.connect(path, timeout=30, isolation_level=None)
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('PRAGMA busy_timeout=30000')
    return c


def init_db(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    c = connect(path)
    try:
        c.execute('''CREATE TABLE IF NOT EXISTS events(
            event_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            schema_version INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            payload TEXT NOT NULL,
            envelope TEXT NOT NULL,
            received_at TEXT NOT NULL)''')
        # 第一次的确认原样存下，重放时返回同样的内容（含 applied、current_publish_no）。
        if 'ack' not in {r[1] for r in c.execute('PRAGMA table_info(events)')}:
            c.execute('ALTER TABLE events ADD COLUMN ack TEXT')
        accounts.ensure(c)
        snapshot.ensure(c)
        inbox.ensure(c)
    finally:
        c.close()


class Contract:
    """按名称与版本读取 contract/schemas/<name>.v<N>.schema.json；不认识的版本或类别拒收。"""

    def __init__(self, schema_dir):
        self.dir = Path(schema_dir)
        self._cache = {}

    def validator(self, name, version):
        if not isinstance(name, str) or not _NAME_RE.match(name):
            raise BadPayload('未知的载荷类别')
        if type(version) is not int or version < 1:
            raise BadPayload(f'{name} 的 schema_version 必须为正整数')
        key = (name, version)
        if key not in self._cache:
            path = self.dir / f'{name}.v{version}.schema.json'
            try:
                schema = json.loads(path.read_text(encoding='utf-8'))
            except FileNotFoundError:
                raise BadPayload(f'不认识的 {name} schema_version：{version}') from None
            self._cache[key] = Draft202012Validator(schema)
        return self._cache[key]

    def check(self, envelope):
        if not isinstance(envelope, dict):
            raise BadPayload('信封应为对象')
        self._run(self.validator('envelope', envelope.get('schema_version')), envelope, '信封')
        self._strict(envelope)
        payload = envelope['payload']
        kind = envelope['kind']
        self._run(self.validator(kind, payload.get('schema_version')), payload, f'{kind} 载荷')

    @staticmethod
    def _strict(envelope):
        # Schema 的 pattern 用 `$`，会放过结尾换行：再严格全匹配一次（Mac 端同样做）。
        eid = envelope['event_id']
        try:
            ok = str(uuid.UUID(eid)) == eid
        except ValueError:
            ok = False
        if not ok:
            raise BadPayload('信封不合契约：event_id 不符合要求')
        if not _CREATED_RE.fullmatch(envelope['created_at']):
            raise BadPayload('信封不合契约：created_at 不符合要求')

    @staticmethod
    def _run(validator, instance, label):
        errors = sorted(validator.iter_errors(instance), key=lambda e: list(e.absolute_path))
        if errors:
            where = '.'.join(str(p) for p in errors[0].absolute_path) or '（顶层）'
            raise BadPayload(f'{label}不合契约：{where} 不符合要求')


def _error(status, detail):
    return JSONResponse({'ok': False, 'detail': detail}, status_code=status)


def _stored_payload(kind, payload):
    """存进 events 表的载荷：密钥类替换为哈希；快照只存编号与摘要（全文在 snapshots 表）。"""
    if kind == 'snapshot':
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        return {'schema_version': payload['schema_version'], 'publish_no': payload['publish_no'],
                'sha256': hashlib.sha256(text.encode('utf-8')).hexdigest()}
    return accounts.redact(kind, payload)


def _store_event(db_path, envelope, clock=None):
    """按 event_id 幂等写入并在同一事务内执行该载荷；返回 (status, body)。

    邀请码等明文在存入 events 表前替换为哈希（accounts.redact），重放比对也用替换后的内容。
    快照引用的图片缺失时整笔回滚、不记事件，返回 400。"""
    stored_payload = _stored_payload(envelope['kind'], envelope['payload'])
    stored = dict(envelope, payload=stored_payload)
    canonical = json.dumps(stored, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    now = (clock or time.time)()
    stale = []
    c = connect(db_path)
    try:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('SELECT envelope,received_at,ack FROM events WHERE event_id=?',
                        (envelope['event_id'],)).fetchone()
        if row:
            ack = {'ok': True, 'event_id': envelope['event_id'], 'received_at': row[1]}
            if row[2]:
                ack = json.loads(row[2])
            elif envelope['kind'] in ('set_invite_code',) + accounts.ACCOUNT_COMMANDS:
                done = c.execute('SELECT applied FROM commands WHERE command_id=?',
                                 (envelope['payload']['command_id'],)).fetchone()
                ack['applied'] = bool(done and done[0])
            c.execute('COMMIT')
            if row[0] != canonical:
                return 409, {'ok': False, 'detail': '同一 event_id 已收到不同内容'}
            return 200, ack
        received_at = utc_now()
        ack = {'ok': True, 'event_id': envelope['event_id'], 'received_at': received_at}
        if envelope['kind'] == 'snapshot':
            try:
                # 图片文件按真实时间判断新旧（与文件 mtime 同一时钟），不用注入的 clock。
                extra, stale = snapshot.apply_snapshot(c, envelope['payload'], snapshot.image_dir(db_path),
                                                       received_at, time.time())
            except snapshot.MissingImages as e:
                c.execute('ROLLBACK')
                return 400, {'ok': False, 'detail': str(e)}
            ack.update(extra)
        elif envelope['kind'] == 'hide':
            ack['applied'] = snapshot.apply_hide(c, envelope['payload'])
        elif envelope['kind'] == 'set_invite_code':
            # ack 里的 applied：false 表示服务器已有更新的邀请码命令，本条未生效（Mac 端按永久失败处理）。
            ack['applied'] = accounts.apply_set_invite_code(c, envelope['payload'], now)
        elif envelope['kind'] in accounts.ACCOUNT_COMMANDS:
            # 停用 / 重置密码（AD-24）：applied 为 false 表示序号过旧、账号不存在或已停用（Mac 端按永久失败处理）。
            ack['applied'] = accounts.apply_account_command(c, envelope['kind'], envelope['payload'], now)
        # ping 只存下。
        c.execute('INSERT INTO events(event_id,kind,schema_version,created_at,payload,envelope,received_at,ack) '
                  'VALUES(?,?,?,?,?,?,?,?)',
                  (envelope['event_id'], envelope['kind'], envelope['payload']['schema_version'],
                   envelope['created_at'], json.dumps(stored_payload, ensure_ascii=False, sort_keys=True),
                   canonical, received_at, json.dumps(ack, ensure_ascii=False)))
        c.execute('COMMIT')
    except Exception:
        if c.in_transaction:
            c.execute('ROLLBACK')
        raise
    finally:
        c.close()
    for path in stale:          # AD-13：不再被最近 3 份快照引用的旧图
        try:
            path.unlink()
        except OSError:
            pass
    return 200, ack


def _store_heartbeat(db_path, envelope, clock=None):
    """心跳：只更新 heartbeat 表（晚到的旧心跳不覆盖），确认回显 event_id。收到时间与判断过期用同一时钟。"""
    now = (clock or time.time)()
    received_at = datetime.fromtimestamp(now, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    c = connect(db_path)
    try:
        c.execute('BEGIN IMMEDIATE')
        applied = snapshot.apply_heartbeat(c, envelope['payload'], received_at, now)
        c.execute('COMMIT')
    except Exception:
        if c.in_transaction:
            c.execute('ROLLBACK')
        raise
    finally:
        c.close()
    return {'ok': True, 'event_id': envelope['event_id'], 'received_at': received_at, 'applied': applied}


def _env_flag(name, default):
    value = os.environ.get(name)
    if value is None or value == '':
        return default
    return value.strip().lower() not in ('0', 'false', 'no', 'off')


def create_app(db_path=None, admin_token=None, contract_dir=None, clock=None, mailer=None, cookie_secure=None,
               public_url=None, terms_path=None):
    db_path = str(db_path or os.environ.get('CURIO_SERVER_DB') or DEFAULT_DB)
    token = admin_token if admin_token is not None else os.environ.get('CURIO_ADMIN_TOKEN', '')
    contract = Contract(contract_dir or os.environ.get('CURIO_CONTRACT_DIR') or DEFAULT_CONTRACT)
    init_db(db_path)
    accounts.load_secret(str(Path(db_path).parent))
    if not token:
        log.warning('未设置 CURIO_ADMIN_TOKEN：所有管理接口一律拒绝')
    # 不暴露自动文档。无鉴权的只有 /healthz 与客人注册/登录/说明页（guest.py）。
    app = FastAPI(title='Curiobject server', version=VERSION, docs_url=None, redoc_url=None, openapi_url=None)
    expected = ('Bearer ' + token).encode() if token else None

    def authorized(request):
        given = request.headers.get('authorization', '').encode()
        return expected is not None and hmac.compare_digest(given, expected)

    @app.get('/healthz')
    def healthz():
        return {'status': 'ok', 'version': VERSION}

    @app.post('/admin/events')
    async def admin_events(request: Request):
        if not authorized(request):
            return JSONResponse({'ok': False, 'detail': '未授权'}, status_code=401,
                                headers={'WWW-Authenticate': 'Bearer'})
        declared = request.headers.get('content-length')
        if declared is not None:
            if not _DIGITS_RE.fullmatch(declared):
                return _error(400, 'Content-Length 无效')
            if int(declared) > MAX_BODY:
                return _error(413, '请求体过大')
        body = await request.body()
        if len(body) > MAX_BODY:
            return _error(413, '请求体过大')
        try:
            envelope = json.loads(body)
        except (ValueError, RecursionError):
            return _error(400, '请求体不是 JSON 或嵌套过深')
        try:
            contract.check(envelope)
            if envelope['kind'] == 'set_invite_code':
                accounts.check_invite_command(envelope['payload'])
            elif envelope['kind'] in accounts.ACCOUNT_COMMANDS:
                accounts.check_account_command(envelope['kind'], envelope['payload'])
            elif envelope['kind'] == 'snapshot':
                snapshot.check_snapshot(envelope['payload'])
            elif envelope['kind'] == 'hide':
                snapshot.check_hide(envelope['payload'])
            elif envelope['kind'] == 'heartbeat':
                snapshot.check_heartbeat(envelope['payload'])
        except (BadPayload, accounts.BadCommand, snapshot.BadSnapshot) as e:
            return _error(400, str(e))
        except RecursionError:
            return _error(400, '载荷嵌套过深')
        if envelope['kind'] == 'heartbeat':
            return await run_in_threadpool(_store_heartbeat, db_path, envelope, clock)
        status, result = await run_in_threadpool(_store_event, db_path, envelope, clock)
        return JSONResponse(result, status_code=status)

    def _accounts_list():
        c = connect(db_path)
        try:
            return accounts.list_accounts(c)
        finally:
            c.close()

    @app.get('/admin/accounts')
    async def admin_accounts(request: Request):
        """账号名单（accounts_list.v1，AD-8、AD-9）：Mac 每轮同步与打开后台 06 时拉取；不含邮箱与任何哈希。"""
        if not authorized(request):
            return JSONResponse({'ok': False, 'detail': '未授权'}, status_code=401,
                                headers={'WWW-Authenticate': 'Bearer'})
        return JSONResponse(await run_in_threadpool(_accounts_list), headers={'Cache-Control': 'no-store'})

    images = snapshot.image_dir(db_path)

    def unauthorized():
        return JSONResponse({'ok': False, 'detail': '未授权'}, status_code=401, headers={'WWW-Authenticate': 'Bearer'})

    async def read_body(request, limit):
        declared = request.headers.get('content-length')
        if declared is not None and (not _DIGITS_RE.fullmatch(declared) or int(declared) > limit):
            return None
        body = await request.body()
        return body if len(body) <= limit else None

    @app.post('/admin/images/check')
    async def admin_images_check(request: Request):
        """AD-13：哪些图片服务器上还没有（image_check.v1 → image_check_result.v1）。"""
        if not authorized(request):
            return unauthorized()
        body = await read_body(request, MAX_BODY)
        if body is None:
            return _error(413, '请求体过大')
        try:
            query = json.loads(body)
            if not isinstance(query, dict):
                raise BadPayload('图片查询应为对象')
            Contract._run(contract.validator('image_check', query.get('schema_version')), query, '图片查询')
        except (ValueError, RecursionError):
            return _error(400, '请求体不是 JSON')
        except BadPayload as e:
            return _error(400, str(e))
        return {'schema_version': 1, 'missing': snapshot.missing_images(images, query['names'])}

    @app.put('/admin/images/{name}')
    async def admin_image_put(name: str, request: Request):
        """AD-13：上传一张转码后的图（内容哈希命名、不可变）；确认为 image_ack.v1。"""
        if not authorized(request):
            return unauthorized()
        body = await read_body(request, snapshot.MAX_IMAGE)
        if body is None:
            return _error(413, '图片过大')
        try:
            ack = await run_in_threadpool(snapshot.store_image, images, name, body)
        except snapshot.BadSnapshot as e:
            return _error(400, str(e))
        return ack

    if cookie_secure is None:
        cookie_secure = _env_flag('CURIO_COOKIE_SECURE', True)
    if public_url is None:
        public_url = (os.environ.get('CURIO_PUBLIC_URL') or '').rstrip('/') or None
    checker = inbox.Checker(contract)
    register_inbox_routes(app, db_path, checker, authorized, clock or time.time)

    def record_event(c, account_id, type_, params, now, co_code=None):
        return inbox.record(c, checker, account_id, type_, params, now, co_code)
    # 客人路由含兜底 GET /{path}，必须最后注册。
    guest.register_guest_routes(app, db_path, connect, clock=clock, mailer=mailer, cookie_secure=cookie_secure,
                                public_url=public_url, terms_path=terms_path, record_event=record_event)
    app.state.db_path = db_path
    return app


EVENTS_MAX_BODY = 65_536          # 50 条事件绰绰有余


def _event_origin_ok(request):
    """行为上报的来源检查：带 Origin（非 'null'）时必须同站；带 Sec-Fetch-Site 时必须 same-origin。两者都没有时放行——
    会话 Cookie 是 SameSite=Lax，跨站 POST 本来就带不上会话（sendBeacon 在部分内置浏览器里不带这两个头）。"""
    origin = request.headers.get('origin')
    if origin and origin != 'null':
        return urlsplit(origin).netloc == request.headers.get('host', '')
    site = request.headers.get('sec-fetch-site')
    return site is None or site in ('same-origin', 'none')


def register_inbox_routes(app, db_path, checker, authorized, clock):
    """行为收件箱（AD-9、AD-25）：客人上报 POST /events（会话鉴权）；Mac 拉取 GET /admin/inbox、确认 POST /admin/inbox/ack。"""

    def unauthorized():
        return JSONResponse({'ok': False, 'detail': '未授权'}, status_code=401, headers={'WWW-Authenticate': 'Bearer'})

    def _receive(token, items):
        c = connect(db_path)
        c.row_factory = sqlite3.Row
        try:
            now = clock()
            state, account = accounts.lookup_session(c, token, now)
            if state != 'ok':
                return 401, {'ok': False, 'detail': '请先登录'}
            # 停用账号（其会话已失效，这里再拦一次）与待重新同意的账号不记录。条款版本由客人路由注册时读入。
            if account['status'] != 'active' or accounts.needs_consent(c, account['account_id'], app.state.terms_version):
                return 403, {'ok': False, 'detail': '不记录'}
            result = inbox.receive(c, checker, account['account_id'], items, now, client=True)
        finally:
            c.close()
        status = 400 if result['rejected'] and not (result['accepted'] or result['duplicates'] or result['dropped']) else 200
        return status, dict(result, ok=status == 200)

    @app.post('/events')
    async def events(request: Request):
        """客人页行为上报（behavior_upload.v1）。预览在 Mac 渲染、从不连到这里（AD-28）。"""
        if not _event_origin_ok(request):
            return _error(403, '来源不符')
        ctype = request.headers.get('content-type', '')
        if not ctype.startswith(('application/json', 'text/plain')):
            return _error(415, '请求类型不对')
        declared = request.headers.get('content-length')
        if declared is not None and (not _DIGITS_RE.fullmatch(declared) or int(declared) > EVENTS_MAX_BODY):
            return _error(413, '请求体过大')
        # 不带 Content-Length（分块传输）时边读边数，超过上限立即 413，不把整个请求体读进内存。
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > EVENTS_MAX_BODY:
                return _error(413, '请求体过大')
            chunks.append(chunk)
        body = b''.join(chunks)
        try:
            batch = json.loads(body)
        except (ValueError, RecursionError):
            return _error(400, '请求体不是 JSON')
        if not isinstance(batch, dict):
            return _error(400, '上报应为对象')
        try:
            Contract._run(checker.upload, batch, '行为上报')
        except BadPayload as e:
            return _error(400, str(e))
        except RecursionError:
            return _error(400, '载荷嵌套过深')
        status, result = await run_in_threadpool(_receive, request.cookies.get(guest.COOKIE), batch['events'])
        return JSONResponse(result, status_code=status, headers={'Cache-Control': 'no-store'})

    def _int_param(value, default, low, high):
        if value is None:
            return default
        if not _DIGITS_RE.fullmatch(value):
            raise BadPayload('参数无效')
        return max(low, min(high, int(value)))

    def _pull(after, limit):
        c = connect(db_path)
        try:
            return inbox.pull(c, after, limit, clock())
        finally:
            c.close()

    @app.get('/admin/inbox')
    async def admin_inbox(request: Request):
        """收件箱拉取（inbox_batch.v1）：inbox_seq > after 的事件按序返回，最多 limit（≤ 500）条。"""
        if not authorized(request):
            return unauthorized()
        try:
            after = _int_param(request.query_params.get('after'), 0, 0, 2 ** 63 - 1)
            limit = _int_param(request.query_params.get('limit'), inbox.PULL_MAX, 1, inbox.PULL_MAX)
        except BadPayload as e:
            return _error(400, str(e))
        return JSONResponse(await run_in_threadpool(_pull, after, limit), headers={'Cache-Control': 'no-store'})

    def _ack(max_seq):
        c = connect(db_path)
        try:
            return inbox.ack(c, max_seq)
        finally:
            c.close()

    @app.post('/admin/inbox/ack')
    async def admin_inbox_ack(request: Request):
        """收件箱确认（inbox_ack.v1）：只删除 inbox_seq ≤ max_seq 的事件；幂等。"""
        if not authorized(request):
            return unauthorized()
        body = await request.body()
        if len(body) > 4096:
            return _error(413, '请求体过大')
        try:
            data = json.loads(body)
            if not isinstance(data, dict):
                raise BadPayload('确认应为对象')
            Contract._run(checker.ack, data, '收件箱确认')
        except (ValueError, RecursionError):
            return _error(400, '请求体不是 JSON')
        except BadPayload as e:
            return _error(400, str(e))
        deleted = await run_in_threadpool(_ack, data['max_seq'])
        return {'ok': True, 'max_seq': data['max_seq'], 'deleted': deleted}


def main(argv=None):
    import uvicorn
    parser = argparse.ArgumentParser(description='Curiobject 服务器（只监听 127.0.0.1，由 Caddy 反代）')
    parser.add_argument('--port', type=int, default=int(os.environ.get('CURIO_SERVER_PORT', DEFAULT_PORT)))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(create_app(), host=HOST, port=args.port, access_log=False)


if __name__ == '__main__':
    main()
