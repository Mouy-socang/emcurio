"""故事 8：服务器管理接口骨架（TestClient，不连任何外部主机）。"""
import json
import sqlite3
import uuid

import pytest
from fastapi.testclient import TestClient

import app as server

TOKEN = 'test-admin-token-not-real'
AUTH = {'Authorization': 'Bearer ' + TOKEN}


def envelope(**over):
    env = {'schema_version': 1, 'event_id': str(uuid.uuid4()), 'kind': 'ping',
           'created_at': '2026-11-03T08:00:00.000Z', 'payload': {'schema_version': 1, 'message': '你好'}}
    env.update(over)
    return env


@pytest.fixture
def db(tmp_path):
    return tmp_path / 'server.db'


@pytest.fixture
def client(db):
    return TestClient(server.create_app(db_path=db, admin_token=TOKEN))


def count(db):
    with sqlite3.connect(db) as c:
        return c.execute('SELECT count(*) FROM events').fetchone()[0]


def test_healthz_needs_no_auth_and_reveals_nothing_else(client):
    r = client.get('/healthz')
    assert r.status_code == 200 and r.json() == {'status': 'ok', 'version': server.VERSION}


def test_no_docs_or_openapi(client):
    for path in ('/docs', '/redoc', '/openapi.json'):
        assert client.get(path).status_code == 404


def test_store_three_pings(client, db):
    envs = [envelope() for _ in range(3)]
    for env in envs:
        r = client.post('/admin/events', json=env, headers=AUTH)
        assert r.status_code == 200
        assert r.json()['event_id'] == env['event_id'] and r.json()['ok'] is True
    assert count(db) == 3


def test_replay_returns_same_ack_without_reprocessing(client, db):
    env = envelope()
    first = client.post('/admin/events', json=env, headers=AUTH)
    again = client.post('/admin/events', json=env, headers=AUTH)
    assert first.status_code == again.status_code == 200
    assert first.json() == again.json()
    assert count(db) == 1


def test_same_event_id_different_content_conflicts(client, db):
    env = envelope()
    client.post('/admin/events', json=env, headers=AUTH)
    r = client.post('/admin/events', json=dict(env, payload={'schema_version': 1, 'message': '别的'}), headers=AUTH)
    assert r.status_code == 409 and count(db) == 1


@pytest.mark.parametrize('headers', [{}, {'Authorization': 'Bearer wrong'}, {'Authorization': TOKEN},
                                     {'Authorization': 'Basic ' + TOKEN}])
def test_auth_required(client, db, headers):
    r = client.post('/admin/events', json=envelope(), headers=headers)
    assert r.status_code == 401 and count(db) == 0
    assert TOKEN not in r.text


def test_unset_token_rejects_everything(db):
    c = TestClient(server.create_app(db_path=db, admin_token=''))
    assert c.post('/admin/events', json=envelope(), headers={'Authorization': 'Bearer '}).status_code == 401
    assert count(db) == 0


@pytest.mark.parametrize('env', [
    envelope(schema_version=9),
    envelope(payload={'schema_version': 9}),
    envelope(kind='snapshot'),
    envelope(kind='../envelope'),
    envelope(event_id='not-a-uuid'),
    envelope(created_at='2026-11-03 08:00'),
    envelope(payload={'schema_version': 1, 'extra': 1}),
    envelope(payload={'schema_version': True}),
    {k: v for k, v in envelope().items() if k != 'payload'},
])
def test_bad_payload_400_in_chinese(client, db, env):
    r = client.post('/admin/events', json=env, headers=AUTH)
    assert r.status_code == 400
    detail = r.json()['detail']
    assert any('一' <= ch <= '鿿' for ch in detail)
    assert count(db) == 0


def test_not_json_and_too_large(client, db):
    assert client.post('/admin/events', content=b'{oops', headers=AUTH).status_code == 400
    big = json.dumps(envelope(payload={'schema_version': 1, 'message': 'x' * (server.MAX_BODY + 10)}))
    assert client.post('/admin/events', content=big, headers=AUTH).status_code == 413
    assert count(db) == 0


def test_wal_and_busy_timeout(db):
    server.init_db(db)
    c = server.connect(db)
    try:
        assert c.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
        assert c.execute('PRAGMA busy_timeout').fetchone()[0] == 30000
    finally:
        c.close()


def test_listens_only_on_localhost(monkeypatch, tmp_path):
    calls = {}
    import uvicorn
    monkeypatch.setattr(uvicorn, 'run', lambda app, **kw: calls.update(kw))
    monkeypatch.setenv('CURIO_SERVER_DB', str(tmp_path / 's.db'))
    server.main(['--port', '8790'])
    assert calls['host'] == '127.0.0.1' and calls['port'] == 8790


def test_uses_shared_contract_dir():
    assert (server.DEFAULT_CONTRACT / 'envelope.v1.schema.json').exists()
    assert (server.DEFAULT_CONTRACT / 'ping.v1.schema.json').exists()


def test_trailing_newline_in_event_id_or_created_at_rejected(client, db):
    for env in (envelope(event_id=str(uuid.uuid4()) + '\n'), envelope(created_at='2026-11-03T08:00:00.000Z\n'),
                envelope(event_id=str(uuid.uuid4()).upper())):
        assert client.post('/admin/events', json=env, headers=AUTH).status_code == 400
    assert count(db) == 0


def test_declared_content_length_checked_before_reading(client, db):
    h = dict(AUTH, **{'Content-Length': str(server.MAX_BODY + 1), 'Content-Type': 'application/json'})
    r = client.post('/admin/events', content=b'{}' + b' ' * (server.MAX_BODY - 1), headers=h)
    assert r.status_code == 413 and count(db) == 0


def test_deep_nesting_is_400(client, db):
    deep = '[' * 100000 + ']' * 100000
    r = client.post('/admin/events', content=deep.encode(), headers=AUTH)
    assert r.status_code == 400 and count(db) == 0
