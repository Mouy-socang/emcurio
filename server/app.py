"""Curiobject 香港服务器：管理接口骨架（AD-3、AD-19、AD-21、AD-23）。

- `GET /healthz`：无鉴权，只返回 ok 与版本。
- `POST /admin/events`：`Authorization: Bearer <管理密钥>`；按 contract/ 的 JSON Schema 校验信封与载荷，
  按 event_id 去重——重复请求返回第一次的确认，不重复处理。
- 只监听 127.0.0.1，由 Caddy 反代并签 HTTPS 证书；服务器从不主动连接 Mac。
- 设置只来自环境变量（systemd EnvironmentFile）：CURIO_ADMIN_TOKEN、CURIO_SERVER_DB、CURIO_CONTRACT_DIR。
"""
import argparse
import hmac
import json
import logging
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from jsonschema import Draft202012Validator
from starlette.concurrency import run_in_threadpool

VERSION = '0.1.0'
HOST = '127.0.0.1'
DEFAULT_PORT = 8790
MAX_BODY = 1_000_000
HERE = Path(__file__).resolve().parent
DEFAULT_CONTRACT = HERE.parent / 'contract' / 'schemas'
DEFAULT_DB = HERE / 'data' / 'server.db'
_NAME_RE = re.compile(r'^[a-z][a-z0-9_-]{0,63}$')
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


def _store_event(db_path, envelope):
    """按 event_id 幂等写入；返回 (status, body)。"""
    canonical = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    c = connect(db_path)
    try:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('SELECT envelope,received_at FROM events WHERE event_id=?',
                        (envelope['event_id'],)).fetchone()
        if row:
            c.execute('COMMIT')
            if row[0] != canonical:
                return 409, {'ok': False, 'detail': '同一 event_id 已收到不同内容'}
            return 200, {'ok': True, 'event_id': envelope['event_id'], 'received_at': row[1]}
        received_at = utc_now()
        c.execute('INSERT INTO events(event_id,kind,schema_version,created_at,payload,envelope,received_at) '
                  'VALUES(?,?,?,?,?,?,?)',
                  (envelope['event_id'], envelope['kind'], envelope['payload']['schema_version'],
                   envelope['created_at'], json.dumps(envelope['payload'], ensure_ascii=False, sort_keys=True),
                   canonical, received_at))
        # 各类载荷的处理在后续故事加入；ping 只存下。
        c.execute('COMMIT')
        return 200, {'ok': True, 'event_id': envelope['event_id'], 'received_at': received_at}
    except Exception:
        if c.in_transaction:
            c.execute('ROLLBACK')
        raise
    finally:
        c.close()


def create_app(db_path=None, admin_token=None, contract_dir=None):
    db_path = str(db_path or os.environ.get('CURIO_SERVER_DB') or DEFAULT_DB)
    token = admin_token if admin_token is not None else os.environ.get('CURIO_ADMIN_TOKEN', '')
    contract = Contract(contract_dir or os.environ.get('CURIO_CONTRACT_DIR') or DEFAULT_CONTRACT)
    init_db(db_path)
    if not token:
        log.warning('未设置 CURIO_ADMIN_TOKEN：所有管理接口一律拒绝')
    # 不暴露自动文档：除 /healthz 外没有无鉴权接口。
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
            if not declared.isdigit():
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
        except BadPayload as e:
            return _error(400, str(e))
        except RecursionError:
            return _error(400, '载荷嵌套过深')
        status, result = await run_in_threadpool(_store_event, db_path, envelope)
        return JSONResponse(result, status_code=status)

    app.state.db_path = db_path
    return app


def main(argv=None):
    import uvicorn
    parser = argparse.ArgumentParser(description='Curiobject 服务器（只监听 127.0.0.1，由 Caddy 反代）')
    parser.add_argument('--port', type=int, default=int(os.environ.get('CURIO_SERVER_PORT', DEFAULT_PORT)))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(create_app(), host=HOST, port=args.port, access_log=False)


if __name__ == '__main__':
    main()
