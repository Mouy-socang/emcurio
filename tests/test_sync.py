"""故事 8：同步模块与发件箱、部署与备份脚本（AD-3、AD-19、AD-21、AD-23）。

只连本机 127.0.0.1 上临时启动的服务器（标准库假服务器，或 server/.venv 里的真服务器）；任何外部主机请求直接失败。
"""
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
import requests

from curio import sync
from curio.store import Store
from scripts import backup_server, deploy_server

ROOT = Path(__file__).resolve().parents[1]
TOKEN = 'test-admin-token-SENTINEL'
_real_request = requests.sessions.Session.request
_real_notify = sync._notify


@pytest.fixture(autouse=True)
def only_localhost():
    """只允许连 127.0.0.1。"""
    def guard(self, method, url, *a, **kw):
        assert urlsplit(url).hostname == '127.0.0.1', '测试不得连接外部主机'
        return _real_request(self, method, url, *a, **kw)
    with patch('requests.sessions.Session.request', guard):
        yield


@pytest.fixture(autouse=True)
def notes():
    sent = []
    with patch('curio.sync._notify', side_effect=sent.append):
        yield sent


class FakeServer:
    """模仿 /admin/events：Bearer 鉴权、按 event_id 去重；可让指定 event 返回 500 或"存下但丢确认"。"""

    def __init__(self):
        self.events = {}          # event_id -> ack
        self.order = []           # 首次处理顺序
        self.fail = set()         # 这些 event_id 返回 500，不存
        self.lose_ack = set()     # 这些 event_id 存下但返回 500（只一次）
        self.reject = {}          # event_id -> 400/409（永久拒收）
        self.drip = None          # 秒数：先发头，再每隔这么久滴一个字节
        self.not_applied = set()  # 这些 event_id（set_invite_code）回 applied: false
        self.requests = 0
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                outer.requests += 1
                body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                if self.headers.get('Authorization') != 'Bearer ' + TOKEN:
                    return self.reply(401, {'ok': False, 'detail': '未授权'})
                env = json.loads(body)
                try:
                    sync.check_envelope(env)
                except sync.ContractError as e:
                    return self.reply(400, {'ok': False, 'detail': str(e)})
                eid = env['event_id']
                if eid in outer.reject:
                    return self.reply(outer.reject[eid], {'ok': False, 'detail': '拒收'})
                if outer.drip:
                    self.send_response(200)
                    self.send_header('Content-Length', '100')
                    self.end_headers()
                    try:
                        for _ in range(100):
                            self.wfile.write(b' ')
                            self.wfile.flush()
                            time.sleep(outer.drip)
                    except OSError:
                        pass
                    return
                if eid in outer.fail:
                    return self.reply(500, {'ok': False, 'detail': '内部错误'})
                if eid not in outer.events:
                    outer.events[eid] = {'ok': True, 'event_id': eid, 'received_at': '2026-11-03T08:00:00.000Z'}
                    if env['kind'] == 'set_invite_code':
                        outer.events[eid]['applied'] = eid not in outer.not_applied
                    outer.order.append(eid)
                if eid in outer.lose_ack:
                    outer.lose_ack.discard(eid)
                    return self.reply(500, {'ok': False, 'detail': '确认丢失'})
                self.reply(200, outer.events[eid])

            def reply(self, status, body):
                data = json.dumps(body, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), H)
        self.url = 'http://127.0.0.1:%d' % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def fake():
    s = FakeServer()
    yield s
    s.close()


def write_env(root, **values):
    (Path(root) / '.env').write_text(''.join(f'{k}={v}\n' for k, v in values.items()))


@pytest.fixture
def root(tmp_path, fake):
    write_env(tmp_path, CURIO_SERVER_URL=fake.url, CURIO_ADMIN_TOKEN=TOKEN)
    return tmp_path


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / 'data' / 'curio.db')


def enqueue(store, n, msg='ping'):
    ids = []
    with store.connect() as c:
        for i in range(n):
            ids.append(sync.enqueue(c, 'ping', {'schema_version': 1, 'message': f'{msg}{i}'}))
    return ids


def rows(store):
    with store.connect() as c:
        return [dict(r) for r in c.execute('SELECT * FROM outbox ORDER BY seq')]


# ---------- 契约与校验器 ----------

def test_outbox_table_shape(store):
    with store.connect() as c:
        cols = [r[1] for r in c.execute('PRAGMA table_info(outbox)')]
    assert cols == ['seq', 'event_id', 'kind', 'payload', 'created_at', 'attempts', 'last_error', 'sent_at',
                    'failed_at']


def test_outbox_without_failed_at_is_migrated(tmp_path):
    path = tmp_path / 'data' / 'curio.db'
    path.parent.mkdir()
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE outbox(seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE, '
                  'kind TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL, '
                  'attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, sent_at TEXT)')
    store = Store(path)
    with store.connect() as c:
        assert 'failed_at' in [r[1] for r in c.execute('PRAGMA table_info(outbox)')]


def test_enqueue_records_valid_envelope(store):
    [eid] = enqueue(store, 1)
    [r] = rows(store)
    assert r['event_id'] == eid and r['kind'] == 'ping' and r['attempts'] == 0 and r['sent_at'] is None
    assert r['created_at'].endswith('Z') and len(r['created_at']) == 24
    sync.check_envelope(sync._envelope(eid, 'ping', json.loads(r['payload']), r['created_at']))


@pytest.mark.parametrize('kind,payload', [
    ('ping', {'schema_version': 9}),
    ('ping', {'schema_version': True}),
    ('ping', {}),
    ('ping', {'schema_version': 1, 'extra': 'x'}),
    ('ping', {'schema_version': 1, 'message': 'x' * 201}),
    ('ping', 'not-an-object'),
    ('snapshot', {'schema_version': 1}),
    ('../envelope', {'schema_version': 1}),
])
def test_bad_payload_rejected_before_enqueue(store, kind, payload):
    with store.connect() as c:
        with pytest.raises(sync.ContractError) as e:
            sync.enqueue(c, kind, payload)
    assert any('一' <= ch <= '鿿' for ch in str(e.value))
    assert rows(store) == []


def test_enqueue_is_part_of_caller_transaction(store):
    with pytest.raises(RuntimeError):
        with store.connect() as c:
            c.execute("INSERT INTO kv VALUES('state','changed')")
            sync.enqueue(c, 'ping', {'schema_version': 1})
            raise RuntimeError('状态变更失败')
    assert rows(store) == [] and store.get('state') is None


def test_validator_refuses_unsupported_keywords():
    with pytest.raises(sync.ContractError):
        sync.validate(1, {'type': 'integer', 'minimum': 0})
    assert sync.validate(True, {'const': 1}) and not sync.validate(1, {'const': 1})
    assert sync.validate(1, {'type': 'boolean'}) and sync.validate(True, {'type': 'integer'})


@pytest.mark.parametrize('schema', [
    {'type': ['string', 'null']},
    {'type': 'date'},
    {'properties': {'a': {'minimum': 1}}},                # 实例里没有 a 也要报
    {'additionalProperties': {'format': 'uuid'}},
    {'additionalProperties': 'no'},
    {'pattern': '('},
    {'required': 'a'},
    {'properties': []},
])
def test_bad_schema_fails_up_front(schema):
    with pytest.raises(sync.ContractError):
        sync.validate({}, schema)


@pytest.mark.parametrize('text', ['{oops', '[]'])
def test_unreadable_schema_is_contract_error(tmp_path, text):
    (tmp_path / 'ping.v1.schema.json').write_text(text)
    with pytest.raises(sync.ContractError):
        sync.load_schema('ping', 1, tmp_path)


def test_contract_schemas_only_use_supported_keywords():
    paths = list((ROOT / 'contract' / 'schemas').glob('*.schema.json'))
    assert paths
    for path in paths:
        sync.check_schema(json.loads(path.read_text()))   # 遍历整棵树（含 additionalProperties）


AGREEMENT_CASES = [
    dict(),
    dict(schema_version=9),
    dict(payload={'schema_version': 9}),
    dict(payload={'schema_version': True}),
    dict(payload={'schema_version': 1.0}),
    dict(payload={'schema_version': 1, 'message': 'x' * 201}),
    dict(payload={'schema_version': 1, 'extra': 1}),
    dict(payload=[]),
    dict(kind='snapshot'),
    dict(kind='../envelope'),
    dict(event_id='not-a-uuid'),
    dict(event_id='0f8fad5b-d9cb-469f-a165-70867728950e\n'),
    dict(event_id='0F8FAD5B-D9CB-469F-A165-70867728950E'),
    dict(created_at='2026-11-03T08:00:00.000Z\n'),
    dict(created_at='2026-11-03 08:00'),
    dict(extra=1),
]


def _agreement_envelopes():
    base = {'schema_version': 1, 'event_id': '0f8fad5b-d9cb-469f-a165-70867728950e', 'kind': 'ping',
            'created_at': '2026-11-03T08:00:00.000Z', 'payload': {'schema_version': 1, 'message': '你好'}}
    return [dict(base, **over) for over in AGREEMENT_CASES]


@pytest.mark.skipif(not (ROOT / 'server' / '.venv' / 'bin' / 'python').exists(), reason='先在 server/ 下 uv sync')
def test_mac_validator_agrees_with_server_jsonschema():
    envs = _agreement_envelopes()
    probe = ('import json,sys\n'
             'import app\n'
             'c=app.Contract(app.DEFAULT_CONTRACT)\n'
             'out=[]\n'
             'for e in json.load(sys.stdin):\n'
             '    try:c.check(e);out.append(True)\n'
             '    except app.BadPayload:out.append(False)\n'
             'print(json.dumps(out))\n')
    r = subprocess.run([str(ROOT / 'server' / '.venv' / 'bin' / 'python'), '-c', probe], cwd=ROOT / 'server',
                       input=json.dumps(envs), capture_output=True, text=True, timeout=60, check=True)
    server_ok = json.loads(r.stdout)
    mac_ok = []
    for e in envs:
        try:
            sync.check_envelope(e)
            mac_ok.append(True)
        except sync.ContractError:
            mac_ok.append(False)
    assert mac_ok == server_ok
    assert mac_ok[0] is True and not any(mac_ok[1:])


# ---------- flush：I/O 矩阵 ----------

def test_normal_send_three_in_order(store, root, fake):
    ids = enqueue(store, 3)
    r = sync.flush(store, root)
    assert r == {'sync_status': 'ok', 'sync_sent': 3, 'sync_pending': 0}
    assert fake.order == ids
    assert all(x['sent_at'] and x['attempts'] == 1 and x['last_error'] is None for x in rows(store))


def test_failure_stops_and_resumes_from_same_event(store, root, fake):
    ids = enqueue(store, 3)
    fake.fail.add(ids[1])
    r = sync.flush(store, root)
    assert r['sync_status'] == 'error' and r['sync_sent'] == 1 and r['sync_pending'] == 2
    assert '500' in r['sync_error']
    a, b, c = rows(store)
    assert a['sent_at'] and not b['sent_at'] and not c['sent_at']
    assert b['attempts'] == 1 and '500' in b['last_error'] and c['attempts'] == 0
    assert fake.order == ids[:1]
    fake.fail.clear()
    r = sync.flush(store, root)
    assert r['sync_status'] == 'ok' and r['sync_sent'] == 2
    assert fake.order == ids
    b = rows(store)[1]
    assert b['attempts'] == 2 and b['last_error'] is None and b['sent_at']


def test_replay_after_lost_ack_is_not_reprocessed(store, root, fake):
    [eid] = enqueue(store, 1)
    fake.lose_ack.add(eid)
    assert sync.flush(store, root)['sync_status'] == 'error'
    assert fake.order == [eid] and not rows(store)[0]['sent_at']
    assert sync.flush(store, root)['sync_status'] == 'ok'
    assert fake.order == [eid] and len(fake.events) == 1
    assert rows(store)[0]['sent_at']


def test_wrong_token_401_not_sent_and_secret_not_leaked(tmp_path, store, fake):
    write_env(tmp_path, CURIO_SERVER_URL=fake.url, CURIO_ADMIN_TOKEN='wrong-SECRET-xyz')
    enqueue(store, 1)
    r = sync.flush(store, tmp_path)
    assert r['sync_status'] == 'error' and '401' in r['sync_error']
    assert fake.events == {} and not rows(store)[0]['sent_at']
    dump = json.dumps(r) + json.dumps(rows(store))
    assert 'wrong-SECRET-xyz' not in dump and TOKEN not in dump


def test_server_rejects_bad_payload_400(store, root, fake, notes):
    [eid] = enqueue(store, 1)
    with store.connect() as c:  # 模拟库里一条被改坏的记录：发送前校验即拦下，判永久失败
        c.execute("UPDATE outbox SET payload='{\"schema_version\": 9}'")
    r = sync.flush(store, root)
    assert r['sync_status'] == 'blocked' and 'schema_version' in r['sync_error'] and fake.requests == 0
    assert r['sync_blocked_seq'] == 1 and rows(store)[0]['failed_at']
    assert len(notes) == 1 and '第 1 条' in notes[0]
    # 服务器端同样拒收（绕过 Mac 校验直接发）
    bad = sync._envelope(eid, 'ping', {'schema_version': 9}, sync.utc_now())
    resp = requests.post(fake.url + '/admin/events', json=bad, headers={'Authorization': 'Bearer ' + TOKEN})
    assert resp.status_code == 400 and fake.events == {}


def test_bad_ack_is_not_confirmation(store, root, fake):
    enqueue(store, 1)
    with patch.object(requests.Session, 'post') as post:
        post.return_value.status_code = 200
        post.return_value.iter_content.return_value = [b'{"ok": true, "event_id": "other"}']
        r = sync.flush(store, root)
    assert r['sync_status'] == 'error' and 'event_id' in r['sync_error'] and not rows(store)[0]['sent_at']


def test_network_error_recorded(tmp_path, store):
    port = _free_port()
    write_env(tmp_path, CURIO_SERVER_URL=f'http://127.0.0.1:{port}', CURIO_ADMIN_TOKEN=TOKEN)
    enqueue(store, 2)
    r = sync.flush(store, tmp_path)
    assert r['sync_status'] == 'error' and r['sync_error'].startswith('连接服务器失败')
    assert [x['attempts'] for x in rows(store)] == [1, 0]


@pytest.mark.parametrize('env', [{}, {'CURIO_SERVER_URL': ''}, {'CURIO_ADMIN_TOKEN': TOKEN}])
def test_unconfigured_is_disabled_and_keeps_outbox(tmp_path, store, fake, env):
    write_env(tmp_path, **env)
    enqueue(store, 2)
    r = sync.flush(store, tmp_path)
    assert r == {'sync_status': 'disabled', 'sync_pending': 2}
    assert fake.requests == 0 and len(rows(store)) == 2


def test_no_env_file_is_disabled(tmp_path, store):
    assert sync.flush(store, tmp_path)['sync_status'] == 'disabled'


@pytest.mark.parametrize('env,note', [
    ({'CURIO_SERVER_URL': 'http://example.com', 'CURIO_ADMIN_TOKEN': TOKEN}, 'https'),
    ({'CURIO_SERVER_URL': 'https://example.com'}, 'CURIO_ADMIN_TOKEN'),
])
def test_unsafe_or_incomplete_config_never_sends(tmp_path, store, env, note):
    write_env(tmp_path, **env)
    enqueue(store, 1)
    r = sync.flush(store, tmp_path)
    assert r['sync_status'] == 'disabled' and note in r['sync_error']


def test_three_consecutive_failures_notify_once_then_reset(store, root, fake, notes):
    [eid] = enqueue(store, 1)
    fake.fail.add(eid)
    for _ in range(2):
        sync.flush(store, root)
    assert notes == []
    assert sync.flush(store, root)['sync_fail_streak'] == 3
    assert len(notes) == 1 and '3' in notes[0]
    sync.flush(store, root)
    assert len(notes) == 1          # 只发一次
    fake.fail.clear()
    assert sync.flush(store, root)['sync_status'] == 'ok'
    assert store.get(sync.KV_FAIL_STREAK) is None and store.get(sync.KV_NOTIFIED) is None
    [eid2] = enqueue(store, 1)
    fake.fail.add(eid2)
    for _ in range(3):
        sync.flush(store, root)
    assert len(notes) == 2          # 恢复后重新计数


def test_timeout_leaves_rest_for_next_round(store, root, fake):
    enqueue(store, 3)
    store.set(sync.KV_FAIL_STREAK, 2)
    # 第一条送达后时钟跳到只剩不足 1 秒
    r = sync.flush(store, root, monotonic=lambda: time.monotonic() + (29.5 if fake.order else 0))
    assert r['sync_status'] == 'timeout' and r['sync_sent'] == 1 and r['sync_pending'] == 2
    assert store.get(sync.KV_FAIL_STREAK) == '2'      # timeout 不改连续失败计数


def test_dripping_server_cannot_exceed_wall_clock_limit(store, root, fake):
    enqueue(store, 1)
    fake.drip = 0.3
    started = time.monotonic()
    r = sync.flush(store, root, seconds=2)
    assert time.monotonic() - started < 3.5
    assert r['sync_status'] == 'error' and '超时' in r['sync_error'] and not rows(store)[0]['sent_at']


@pytest.mark.parametrize('status', [400, 409])
def test_permanent_rejection_blocks_without_skipping(store, root, fake, notes, status):
    ids = enqueue(store, 3)
    fake.reject[ids[1]] = status
    r = sync.flush(store, root)
    assert r['sync_status'] == 'blocked' and r['sync_sent'] == 1 and r['sync_blocked_seq'] == 2
    assert str(status) in r['sync_error']
    a, b, c = rows(store)
    assert a['sent_at'] and b['failed_at'] and not b['sent_at'] and not c['sent_at']
    assert len(notes) == 1 and '第 2 条' in notes[0] and '网络' not in notes[0]
    # 再 flush：仍停在第 2 条，不发请求、不跳过、不重复通知
    before = fake.requests
    r = sync.flush(store, root)
    assert r['sync_status'] == 'blocked' and fake.requests == before and fake.order == ids[:1] and len(notes) == 1
    # 人工 requeue 后从第 2 条继续
    fake.reject.clear()
    from scripts import outbox
    assert outbox.main(['--db', str(store.path), 'requeue', '2'], out=lambda *a: None) == 0
    assert sync.flush(store, root)['sync_status'] == 'ok' and fake.order == ids


def test_corrupt_payload_json_is_recorded_not_raised(store, root, fake, notes):
    enqueue(store, 2)
    with store.connect() as c:
        c.execute("UPDATE outbox SET payload='{oops' WHERE seq=1")
    r = sync.flush(store, root)
    assert r['sync_status'] == 'blocked' and '损坏' in r['sync_error'] and fake.requests == 0
    assert rows(store)[0]['failed_at'] and rows(store)[0]['attempts'] == 1 and len(notes) == 1


def test_outbox_script(store, capsys):
    from scripts import outbox
    lines = []
    db = ['--db', str(store.path)]
    assert outbox.main(db + ['ping', '你好'], out=lines.append) == 0
    assert rows(store)[0]['kind'] == 'ping' and json.loads(rows(store)[0]['payload'])['message'] == '你好'
    assert outbox.main(db + ['list'], out=lines.append) == 0 and '待发送' in lines[-1]
    assert outbox.main(db + ['drop', '1'], out=lines.append) == 2 and len(rows(store)) == 1
    assert outbox.main(db + ['drop', '1', '--yes'], out=lines.append) == 0 and rows(store) == []
    assert outbox.main(db + ['requeue', '1'], out=lines.append) == 1
    assert outbox.main(db + ['ping', 'x' * 300], out=lines.append) == 1 and rows(store) == []


def test_server_settings_accepts_https(tmp_path):
    write_env(tmp_path, CURIO_SERVER_URL='https://curio.example.com/', CURIO_ADMIN_TOKEN=TOKEN)
    assert sync.server_settings(tmp_path) == ('https://curio.example.com', TOKEN, None)
    write_env(tmp_path, CURIO_SERVER_URL='http://localhost:8790', CURIO_ADMIN_TOKEN=TOKEN)
    assert sync.server_settings(tmp_path) == ('http://localhost:8790', TOKEN, None)


def test_notify_uses_osascript():
    with patch('curio.sync.subprocess.run') as run:
        _real_notify('同步失败')
    cmd = run.call_args[0][0]
    assert cmd[:2] == ['osascript', '-e'] and '同步失败' in cmd[2] and 'display notification' in cmd[2]
    with patch('curio.sync.subprocess.run', side_effect=FileNotFoundError):
        _real_notify('没有 osascript 也不抛错')


def test_run_swallows_unexpected_errors_and_counts_them(store, root, notes):
    with patch('curio.sync.flush', side_effect=RuntimeError('boom')):
        for i in range(3):
            r = sync.run(store, root)
            assert r['sync_status'] == 'error' and r['sync_fail_streak'] == i + 1
    assert len(notes) == 1


# ---------- 采集轮末尾 flush ----------

def test_collection_round_flushes_at_end(tmp_path):
    shutil.copytree(ROOT / 'config', tmp_path / 'config')
    from curio import curio_monitor
    with patch('curio.schedule.Scheduler.fetch_due', return_value=False), \
         patch('curio.schedule.Scheduler.check_due', return_value=True), \
         patch('curio.schedule.Scheduler.mark_check_started'), \
         patch.object(curio_monitor, 'check_existing', return_value={}):
        result = curio_monitor.run(tmp_path)
    assert result['sync_status'] == 'disabled' and result['sync_pending'] == 0
    with sqlite3.connect(tmp_path / 'data' / 'curio.db') as c:
        saved = json.loads(c.execute('SELECT result FROM collection_runs ORDER BY id DESC').fetchone()[0])
    assert saved['sync_status'] == 'disabled'


def test_collection_round_calls_sync_after_ai(tmp_path):
    shutil.copytree(ROOT / 'config', tmp_path / 'config')
    from curio import curio_monitor
    calls = []
    with patch('curio.schedule.Scheduler.fetch_due', return_value=False), \
         patch('curio.schedule.Scheduler.check_due', return_value=True), \
         patch('curio.schedule.Scheduler.mark_check_started'), \
         patch.object(curio_monitor, 'check_existing', return_value={}), \
         patch('curio.ai.run', side_effect=lambda *a, **k: calls.append('ai') or {'ai_status': 'disabled'}), \
         patch('curio.sync.run', side_effect=lambda *a, **k: calls.append('sync') or {'sync_status': 'ok'}), \
         patch('curio.store.Store.archive', side_effect=lambda *a: calls.append('archive')):
        result = curio_monitor.run(tmp_path)
    assert calls == ['ai', 'sync', 'archive'] and result['sync_status'] == 'ok'


# ---------- 本机真服务器端到端 ----------

def _free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


SERVER_PY = ROOT / 'server' / '.venv' / 'bin' / 'python'


@pytest.mark.skipif(not SERVER_PY.exists(), reason='先在 server/ 下 uv sync')
def test_end_to_end_with_local_server(tmp_path, store):
    port = _free_port()
    env = dict(os.environ, CURIO_ADMIN_TOKEN=TOKEN, CURIO_SERVER_DB=str(tmp_path / 'server.db'))
    proc = subprocess.Popen([str(SERVER_PY), 'app.py', '--port', str(port)], cwd=ROOT / 'server', env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f'http://127.0.0.1:{port}'
    try:
        for _ in range(100):
            try:
                if requests.get(url + '/healthz', timeout=1).status_code == 200:
                    break
            except requests.RequestException:
                time.sleep(0.1)
        else:
            pytest.fail('本机服务器未启动')
        write_env(tmp_path, CURIO_SERVER_URL=url, CURIO_ADMIN_TOKEN=TOKEN)
        ids = enqueue(store, 3)
        assert sync.flush(store, tmp_path) == {'sync_status': 'ok', 'sync_sent': 3, 'sync_pending': 0}
        assert all(r['sent_at'] for r in rows(store))
        with sqlite3.connect(tmp_path / 'server.db') as c:
            got = [r[0] for r in c.execute('SELECT event_id FROM events ORDER BY received_at,rowid')]
        assert got == ids
        # 重放：同一确认、不重复处理
        r = rows(store)[0]
        env1 = sync._envelope(r['event_id'], 'ping', json.loads(r['payload']), r['created_at'])
        h = {'Authorization': 'Bearer ' + TOKEN}
        a = requests.post(url + '/admin/events', json=env1, headers=h)
        b = requests.post(url + '/admin/events', json=env1, headers=h)
        assert a.status_code == b.status_code == 200 and a.json() == b.json()
        assert requests.post(url + '/admin/events', json=env1).status_code == 401
        with sqlite3.connect(tmp_path / 'server.db') as c:
            assert c.execute('SELECT count(*) FROM events').fetchone()[0] == 3
    finally:
        proc.terminate()
        proc.wait(timeout=10)


# ---------- 故事 9：邀请码指令（set_invite_code，AD-2、AD-24） ----------

def test_outbox_invite_enqueues_command_with_increasing_target_seq(store):
    from scripts import outbox
    lines = []
    db = ['--db', str(store.path)]
    assert outbox.main(db + ['invite', 'MOMO2026'], out=lines.append) == 0
    assert outbox.main(db + ['invite', ' NEW-CODE_2 '], out=lines.append) == 0
    got = [(r['kind'], json.loads(r['payload'])) for r in rows(store)]
    assert [k for k, _ in got] == ['set_invite_code', 'set_invite_code']
    assert [p['target_seq'] for _, p in got] == [1, 2]
    assert [p['code'] for _, p in got] == ['MOMO2026', 'NEW-CODE_2']
    assert got[0][1]['command_id'] != got[1][1]['command_id']
    assert all('MOMO2026' not in line for line in lines)        # 不回显邀请码
    for r in rows(store):
        sync.check_envelope(sync._envelope(r['event_id'], r['kind'], json.loads(r['payload']), r['created_at']))


@pytest.mark.parametrize('code', ['abc', 'has space', 'x' * 33, '邀请码码码', '   ', 'MOMO\n2026'])
def test_outbox_invite_rejects_bad_code(store, code):
    from scripts import outbox
    lines = []
    assert outbox.main(['--db', str(store.path), 'invite', code], out=lines.append) == 1
    assert rows(store) == [] and store.get('cmd_seq:invite') is None
    assert lines[-1].startswith('失败：')


def test_invite_target_seq_is_part_of_transaction(store):
    with pytest.raises(RuntimeError):
        with store.connect() as c:
            sync.enqueue_invite(c, 'MOMO2026')
            raise RuntimeError('失败')
    assert rows(store) == [] and store.get('cmd_seq:invite') is None


def test_invite_not_applied_blocks_and_notifies(store, root, fake, notes):
    with store.connect() as c:
        eid, _ = sync.enqueue_invite(c, 'MOMO2026')
    fake.not_applied.add(eid)
    r = sync.flush(store, root)
    assert r['sync_status'] == 'blocked' and r['sync_error'] == sync.INVITE_NOT_APPLIED
    [row] = rows(store)
    assert row['failed_at'] and row['sent_at'] is None and row['last_error'] == '服务器已有更新的邀请码命令，未生效'
    assert len(notes) == 1 and '邀请码' in notes[0]


def test_invite_applied_ack_is_sent(store, root, fake):
    with store.connect() as c:
        sync.enqueue_invite(c, 'MOMO2026')
    assert sync.flush(store, root)['sync_status'] == 'ok'


def test_invite_seq_override_for_recovery(store):
    from scripts import outbox
    db = ['--db', str(store.path)]
    assert outbox.main(db + ['invite', 'MOMO2026'], out=lambda *_: None) == 0
    assert outbox.main(db + ['invite', 'NEWCODE2', '--seq', '50'], out=lambda *_: None) == 0
    assert outbox.main(db + ['invite', 'NEWCODE3', '--seq', '10'], out=lambda *_: None) == 0   # 只往上抬
    assert outbox.main(db + ['invite', 'NEWCODE4', '--seq', '0'], out=lambda *_: None) == 1
    assert [json.loads(r['payload'])['target_seq'] for r in rows(store)] == [1, 50, 51]


def test_outbox_list_masks_invite_code(store):
    from scripts import outbox
    lines = []
    db = ['--db', str(store.path)]
    outbox.main(db + ['invite', 'MOMO2026'], out=lines.append)
    outbox.main(db + ['list'], out=lines.append)
    assert 'MOMO2026' not in '\n'.join(lines) and 'M******6' in lines[-1] and '序号 1' in lines[-1]


INVITE_CASES = [
    dict(),
    dict(code='abc'),
    dict(code='MOMO2026\n'),
    dict(target_seq='1'),
    dict(target_seq=True),
    dict(target_seq=1.5),
    dict(command_id='not-a-uuid'),
    dict(extra=1),
]


@pytest.mark.skipif(not (ROOT / 'server' / '.venv' / 'bin' / 'python').exists(), reason='先在 server/ 下 uv sync')
def test_invite_contract_agrees_with_server_jsonschema():
    base = {'schema_version': 1, 'command_id': '0f8fad5b-d9cb-469f-a165-70867728950e', 'target_seq': 1,
            'code': 'MOMO2026'}
    envs = [{'schema_version': 1, 'event_id': '0f8fad5b-d9cb-469f-a165-70867728950e', 'kind': 'set_invite_code',
             'created_at': '2026-11-03T08:00:00.000Z', 'payload': dict(base, **over)} for over in INVITE_CASES]
    probe = ('import json,sys\n'
             'import app\n'
             'c=app.Contract(app.DEFAULT_CONTRACT)\n'
             'out=[]\n'
             'for e in json.load(sys.stdin):\n'
             '    try:c.check(e);out.append(True)\n'
             '    except app.BadPayload:out.append(False)\n'
             'print(json.dumps(out))\n')
    r = subprocess.run([str(ROOT / 'server' / '.venv' / 'bin' / 'python'), '-c', probe], cwd=ROOT / 'server',
                       input=json.dumps(envs), capture_output=True, text=True, timeout=60, check=True)
    server_ok = json.loads(r.stdout)
    mac_ok = []
    for e in envs:
        try:
            sync.check_envelope(e)
            mac_ok.append(True)
        except sync.ContractError:
            mac_ok.append(False)
    # 结尾换行由两端在 Schema 之外再严格检查（Mac：enqueue_invite；服务器：accounts.check_invite_command）。
    assert mac_ok == server_ok
    assert mac_ok[0] is True and mac_ok[1] is False and not any(mac_ok[3:])


@pytest.mark.skipif(not SERVER_PY.exists(), reason='先在 server/ 下 uv sync')
def test_invite_end_to_end_with_local_server(tmp_path, store):
    """Mac 经发件箱下发邀请码 → 本机真服务器生效 → 旧码注册失败、新码成功（只连 127.0.0.1）。"""
    from scripts import outbox
    port = _free_port()
    env = dict(os.environ, CURIO_ADMIN_TOKEN=TOKEN, CURIO_SERVER_DB=str(tmp_path / 'server.db'),
               CURIO_COOKIE_SECURE='0', CURIO_MAIL_DIR=str(tmp_path / 'mail'))
    proc = subprocess.Popen([str(SERVER_PY), 'app.py', '--port', str(port)], cwd=ROOT / 'server', env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f'http://127.0.0.1:{port}'
    try:
        for _ in range(100):
            try:
                if requests.get(url + '/healthz', timeout=1).status_code == 200:
                    break
            except requests.RequestException:
                time.sleep(0.1)
        else:
            pytest.fail('本机服务器未启动')
        write_env(tmp_path, CURIO_SERVER_URL=url, CURIO_ADMIN_TOKEN=TOKEN)

        def register(code, username):
            s = requests.Session()
            s.headers['Origin'] = url
            r = s.post(url + '/register', allow_redirects=False,
                       data={'invite': code, 'username': username, 'password': 'meissen1710',
                             'nickname': '王姐', 'consent': 'yes', 'next': '/item/CO-0001'})
            return r, s

        r, _ = register('OLDCODE1', 'before')
        assert r.status_code == 400 and '注册暂未开放' in r.text
        db = ['--db', str(store.path)]
        assert outbox.main(db + ['invite', 'OLDCODE1'], out=lambda *_: None) == 0
        assert sync.flush(store, tmp_path)['sync_status'] == 'ok'
        r, s = register('oldcode1', 'first')
        assert r.status_code == 303 and r.headers['location'] == '/item/CO-0001'
        assert '今日榜' in s.get(url + '/item/CO-0001').text
        assert outbox.main(db + ['invite', 'NEWCODE2'], out=lambda *_: None) == 0
        assert sync.flush(store, tmp_path)['sync_status'] == 'ok'
        r, _ = register('OLDCODE1', 'second')
        assert r.status_code == 400 and '邀请码不正确，请向墨墨确认。' in r.text
        r, _ = register('NEWCODE2', 'second')
        assert r.status_code == 303
        with sqlite3.connect(tmp_path / 'server.db') as c:
            dump = '\n'.join(c.iterdump())
        assert 'OLDCODE1' not in dump and 'NEWCODE2' not in dump and 'meissen1710' not in dump
    finally:
        proc.terminate()
        proc.wait(timeout=10)


# ---------- 部署与备份脚本（mock / 本机执行，不连真实 VPS） ----------

HEALTH = json.dumps({'status': 'ok', 'version': deploy_server.local_version(ROOT)})


class Recorder:
    def __init__(self, fail_at=None, stdout=HEALTH):
        self.calls, self.kwargs, self.fail_at, self.stdout = [], [], fail_at, stdout

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        self.kwargs.append(kw)
        rc = 1 if self.fail_at is not None and len(self.calls) - 1 == self.fail_at else 0
        return subprocess.CompletedProcess(cmd, rc, stdout=self.stdout, stderr='')


def _no_http(url):
    raise AssertionError('不应发 HTTPS 检查')


@pytest.fixture
def deploy_root(tmp_path):
    shutil.copytree(ROOT / 'server', tmp_path / 'server', ignore=shutil.ignore_patterns('.venv', 'data', '__pycache__'))
    return tmp_path


def test_deploy_commands(deploy_root, capsys):
    write_env(deploy_root, CURIO_SSH_HOST='deploy@203.0.113.10')
    rec = Recorder()
    assert deploy_server.main([], root=deploy_root, runner=rec, http_get=_no_http) == 0
    joined = [' '.join(c) for c in rec.calls]
    assert rec.calls[1][0] == 'rsync' and rec.calls[1][-1] == 'deploy@203.0.113.10:/opt/curiobject/server/'
    assert '--exclude' in rec.calls[1] and '.venv/' in rec.calls[1] and 'data/' in rec.calls[1]
    assert rec.calls[2][-1] == 'deploy@203.0.113.10:/opt/curiobject/contract/'
    assert 'UV_PYTHON_INSTALL_DIR=/opt/curiobject/python' in joined[3]
    assert 'uv sync --frozen --no-dev' in joined[3] and 'systemctl restart curiobject' in joined[3]
    assert '127.0.0.1:8790/healthz' in joined[4]
    for cmd in rec.calls:
        assert cmd[0] in ('ssh', 'rsync')
        if cmd[0] == 'ssh':
            assert cmd[1:5] == ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15']
        else:
            assert cmd[cmd.index('-e') + 1] == 'ssh -o BatchMode=yes -o ConnectTimeout=15'
    assert all(kw.get('timeout') for kw in rec.kwargs)


def test_deploy_checks_version_locally_and_over_https(deploy_root, capsys):
    write_env(deploy_root, CURIO_SSH_HOST='deploy@203.0.113.10', CURIO_SERVER_URL='https://curio.example.com/')
    seen = []
    assert deploy_server.main([], root=deploy_root, runner=Recorder(),
                              http_get=lambda u: seen.append(u) or HEALTH) == 0
    assert seen == ['https://curio.example.com/healthz']
    stale = json.dumps({'status': 'ok', 'version': '0.0.0'})
    assert deploy_server.main([], root=deploy_root, runner=Recorder(), http_get=lambda u: stale) == 1
    assert '版本不符' in capsys.readouterr().err
    assert deploy_server.main([], root=deploy_root, runner=Recorder(stdout=stale), http_get=_no_http) == 1


def test_deploy_dry_run_runs_nothing(deploy_root, capsys):
    write_env(deploy_root, CURIO_SSH_HOST='deploy@203.0.113.10')
    rec = Recorder()
    assert deploy_server.main(['--dry-run'], root=deploy_root, runner=rec, http_get=_no_http) == 0
    assert rec.calls == [] and 'rsync' in capsys.readouterr().out


@pytest.mark.parametrize('env', [{}, {'CURIO_SSH_HOST': '-oProxyCommand=evil'}, {'CURIO_SSH_HOST': 'a b'}])
def test_deploy_refuses_without_valid_host(deploy_root, capsys, env):
    write_env(deploy_root, **env)
    rec = Recorder()
    assert deploy_server.main([], root=deploy_root, runner=rec, http_get=_no_http) == 1 and rec.calls == []
    assert '部署失败' in capsys.readouterr().err


def test_deploy_stops_on_failed_step(deploy_root, capsys):
    write_env(deploy_root, CURIO_SSH_HOST='deploy@203.0.113.10')
    rec = Recorder(fail_at=1)
    assert deploy_server.main([], root=deploy_root, runner=rec, http_get=_no_http) == 1 and len(rec.calls) == 2


def test_deploy_timeout_is_failure(deploy_root, capsys):
    write_env(deploy_root, CURIO_SSH_HOST='deploy@203.0.113.10')

    def hang(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw['timeout'])
    assert deploy_server.main([], root=deploy_root, runner=hang, http_get=_no_http) == 1
    assert '超时' in capsys.readouterr().err


class LocalSSH:
    """把 ssh/rsync 换成在本机执行：ssh 的远端命令用 sh -c 跑（TMPDIR 指向测试目录），rsync 拉取改为本地复制。"""

    def __init__(self, tmpdir):
        self.calls, self.tmpdir = [], tmpdir

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        assert kw.get('timeout')
        if cmd[0] == 'ssh':
            assert cmd[1:5] == ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15']
            return subprocess.run(['sh', '-c', cmd[-1]], capture_output=True, text=True,
                                  env=dict(os.environ, TMPDIR=str(self.tmpdir)))
        if cmd[0] == 'rsync':
            src = cmd[-2].split(':', 1)[1]
            try:
                shutil.copyfile(src, cmd[-1])
            except OSError:
                return subprocess.CompletedProcess(cmd, 23)
            return subprocess.CompletedProcess(cmd, 0)
        raise AssertionError(cmd)


def _server_db(path):
    c = sqlite3.connect(path)
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('CREATE TABLE events(event_id TEXT PRIMARY KEY)')
    c.execute("INSERT INTO events VALUES('e1')")
    c.commit()
    return c   # 保持连接打开：备份不应依赖库处于空闲


def test_backup_pulls_openable_copy_and_prunes(tmp_path, capsys):
    srv = tmp_path / 'srv' / 'server.db'
    srv.parent.mkdir()
    remote_tmp = tmp_path / 'remote-tmp'
    remote_tmp.mkdir()
    live = _server_db(srv)
    try:
        write_env(tmp_path, CURIO_SSH_HOST='deploy@203.0.113.10', CURIO_REMOTE_DB=str(srv),
                  CURIO_REMOTE_PYTHON=sys.executable)
        dest = tmp_path / 'data' / 'server-backups'
        dest.mkdir(parents=True)
        now = datetime(2026, 11, 3, 4, 0, tzinfo=timezone.utc)
        old = dest / ('server-%s.db' % (now - timedelta(days=31)).strftime(backup_server.STAMP))
        recent = dest / ('server-%s.db' % (now - timedelta(days=29)).strftime(backup_server.STAMP))
        other = dest / 'notes.txt'
        for p in (old, recent, other):
            p.write_text('x')
        runner = LocalSSH(remote_tmp)
        final = backup_server.backup(tmp_path, runner, now)
    finally:
        live.close()
    assert final == dest / 'server-20261103T040000Z.db'
    with sqlite3.connect(final) as c:
        assert c.execute('SELECT event_id FROM events').fetchall() == [('e1',)]
    assert not old.exists() and recent.exists() and other.exists()
    assert not list(dest.glob('*.part'))
    assert 'VACUUM INTO' in ' '.join(runner.calls[0]) and 'mktemp -d' in runner.calls[0][-1]
    assert list(remote_tmp.iterdir()) == []          # 远端私有临时目录已删除


def test_backup_fails_nonzero_when_remote_db_missing(tmp_path, capsys):
    remote_tmp = tmp_path / 'remote-tmp'
    remote_tmp.mkdir()
    write_env(tmp_path, CURIO_SSH_HOST='deploy@203.0.113.10', CURIO_REMOTE_DB=str(tmp_path / 'nope.db'),
              CURIO_REMOTE_PYTHON=sys.executable)
    assert backup_server.main([], root=tmp_path, runner=LocalSSH(remote_tmp)) == 1
    assert '备份失败' in capsys.readouterr().err
    assert not (tmp_path / 'nope.db').exists()
    assert not list((tmp_path / 'data' / 'server-backups').iterdir())
    assert list(remote_tmp.iterdir()) == []


def test_backup_rejects_corrupt_copy(tmp_path, capsys):
    write_env(tmp_path, CURIO_SSH_HOST='deploy@203.0.113.10', CURIO_REMOTE_PYTHON=sys.executable)
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd)
        if cmd[0] == 'rsync':
            Path(cmd[-1]).write_bytes(b'not a database' * 100)
        return subprocess.CompletedProcess(cmd, 0, stdout=str(tmp_path / 'tmp.abc') + '\n')
    assert backup_server.main([], root=tmp_path, runner=runner) == 1
    assert not list((tmp_path / 'data' / 'server-backups').iterdir())
    assert calls[-1][-1] == 'rm -rf -- ' + str(tmp_path / 'tmp.abc')


def test_backup_without_host_fails(tmp_path, capsys):
    assert backup_server.main([], root=tmp_path, runner=Recorder()) == 1


@pytest.mark.parametrize('env', [{}, {'CURIO_SSH_HOST': '-oProxyCommand=evil'}, {'CURIO_SSH_HOST': 'a b'}])
def test_backup_refuses_invalid_host(tmp_path, capsys, env):
    write_env(tmp_path, **env)
    rec = Recorder()
    assert backup_server.main([], root=tmp_path, runner=rec) == 1 and rec.calls == []
    assert '备份失败' in capsys.readouterr().err
