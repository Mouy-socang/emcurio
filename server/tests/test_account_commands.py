"""故事 14：账号名单接口与 Mac 账号指令（停用、重置密码；AD-8、AD-24）。TestClient，不连任何外部主机。

覆盖：名单只含六个字段（无邮箱、无哈希）且合契约、需管理密钥；停用使会话与重设链接失效、登录只见停用页；
重置使旧会话失效、临时密码可登录、events 表不存明文；同一 command_id 重放返回第一次结果、不再改密码；
target_seq 不大于已应用值不生效；账号不存在 / 对已停用账号重置不生效；坏载荷 400。
"""
import json
import sqlite3
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

import accounts
import app as server
from test_accounts import (AUTH, PASSWORD, SAME_ORIGIN, TOKEN, login, make_client_like, register,  # noqa: F401
                                 reset_link, rows, set_invite)
from test_accounts import client, clock, db, inbox, make  # noqa: F401  （fixtures）

CONTRACT = Path(__file__).resolve().parents[2] / 'contract' / 'schemas'
TEMP = 'Hk7mPq2Rx9Tz'


def command(client, kind, account_id, target_seq, command_id=None, event_id=None, **extra):
    payload = {'schema_version': 1, 'command_id': command_id or str(uuid.uuid4()), 'target_seq': target_seq,
               'account_id': account_id, **extra}
    env = {'schema_version': 1, 'event_id': event_id or str(uuid.uuid4()), 'kind': kind,
           'created_at': '2026-11-03T08:00:00.000Z', 'payload': payload}
    return client.post('/admin/events', json=env, headers=AUTH), env


def disable(client, account_id, seq, **kw):
    return command(client, 'disable_account', account_id, seq, **kw)


def reset(client, account_id, seq, temp=TEMP, **kw):
    return command(client, 'reset_password', account_id, seq, temp_password=temp, **kw)


def signed_up(client, db, **over):
    """注册一位客人（已登录的另一个访客），返回 (访客, account_id)。"""
    set_invite(client)
    guest = make_client_like(client)
    assert register(guest, **over).status_code == 303
    username = over.get('username', 'wangjie')
    [acct] = rows(db, 'SELECT account_id FROM accounts WHERE username=?', username)
    return guest, acct['account_id']


def home_ok(r):
    return r.status_code == 200 and '今日榜' in r.text and '/register' not in r.text


# ---------- 名单 ----------

def test_accounts_list_requires_admin_token(client):
    assert client.get('/admin/accounts').status_code == 401
    assert client.get('/admin/accounts', headers={'Authorization': 'Bearer wrong'}).status_code == 401


def test_accounts_list_fields_and_contract(client, db, clock):
    signed_up(client, db, email='wang@example.com')
    clock.now += 60
    signed_up(client, db, username='lily_sg', nickname='Lily')
    r = client.get('/admin/accounts', headers=AUTH)
    assert r.status_code == 200 and r.headers['cache-control'] == 'no-store'
    body = r.json()
    schema = json.loads((CONTRACT / 'accounts_list.v1.schema.json').read_text())
    assert not list(Draft202012Validator(schema).iter_errors(body))
    assert [a['username'] for a in body['accounts']] == ['wangjie', 'lily_sg']
    first = body['accounts'][0]
    assert set(first) == {'account_id', 'username', 'nickname', 'status', 'created_at', 'account_rev'}
    assert first['nickname'] == '王姐' and first['status'] == 'active' and first['account_rev'] == 1
    text = r.text
    assert 'wang@example.com' not in text and '$argon2' not in text and 'password' not in text


# ---------- 停用 ----------

def test_disable_revokes_sessions_and_reset_links_and_shows_disabled_page(client, db, inbox):
    guest, aid = signed_up(client, db, email='wang@example.com')
    assert home_ok(guest.get('/'))
    assert client.post('/forgot', data={'username': 'wangjie'}).status_code == 200
    link = reset_link(inbox)
    r, env = disable(client, aid, 1)
    assert r.status_code == 200 and r.json()['applied'] is True and r.json()['event_id'] == env['event_id']
    [acct] = rows(db, 'SELECT status,account_rev FROM accounts')
    assert acct == {'status': 'disabled', 'account_rev': 2}
    assert '登录已过期，请重新登录。' in guest.get('/').text            # 现有会话立即失效
    assert '此链接已失效' in client.get(link.replace('https://curio.test', '')).text
    other = make_client_like(client)
    assert login(other).status_code == 303
    r = other.get('/')
    assert r.status_code == 403 and '<h1>账号已停用</h1>' in r.text and '今日榜' not in r.text
    assert client.get('/admin/accounts', headers=AUTH).json()['accounts'][0]['status'] == 'disabled'


def test_unknown_account_is_not_applied(client):
    r, _ = disable(client, uuid.uuid4().hex, 1)
    assert r.status_code == 200 and r.json()['applied'] is False


# ---------- 重置密码 ----------

def test_reset_password_revokes_sessions_and_temp_password_logs_in(client, db):
    guest, aid = signed_up(client, db)
    r, _ = reset(client, aid, 1)
    assert r.status_code == 200 and r.json()['applied'] is True
    assert '登录已过期，请重新登录。' in guest.get('/').text
    assert login(make_client_like(client)).status_code == 401          # 旧密码不能再登录
    fresh = make_client_like(client)
    assert login(fresh, password=TEMP).status_code == 303
    assert home_ok(fresh.get('/'))
    assert rows(db, 'SELECT account_rev FROM accounts')[0]['account_rev'] == 2
    with sqlite3.connect(db) as c:
        dump = '\n'.join(c.iterdump())
    assert TEMP not in dump                                             # 只存哈希；events 表里是 HMAC
    [ev] = rows(db, "SELECT payload FROM events WHERE kind='reset_password'")
    assert json.loads(ev['payload'])['temp_password'].startswith('hmac-sha256:')


def test_replay_same_command_returns_first_result_without_changing_password(client, db):
    _, aid = signed_up(client, db)
    cid = str(uuid.uuid4())
    first, env = reset(client, aid, 1, command_id=cid)
    again = client.post('/admin/events', json=env, headers=AUTH)          # 同一信封重发
    assert first.status_code == again.status_code == 200 and first.json() == again.json()
    # 同一 command_id 换信封、换密码再发：返回第一次的结果，不改密码
    other, _ = reset(client, aid, 2, temp='Zz9xYw8vUt7s', command_id=cid)
    assert other.status_code == 200 and other.json()['applied'] is True
    assert login(make_client_like(client), password='Zz9xYw8vUt7s').status_code == 401
    assert login(make_client_like(client), password=TEMP).status_code == 303
    assert rows(db, 'SELECT account_rev FROM accounts')[0]['account_rev'] == 2
    assert rows(db, 'SELECT count(*) AS n FROM commands WHERE command_id=?', cid)[0]['n'] == 1


def test_stale_target_seq_is_not_applied(client, db):
    _, aid = signed_up(client, db)
    assert reset(client, aid, 5)[0].json()['applied'] is True
    r, _ = reset(client, aid, 5, temp='Zz9xYw8vUt7s')
    assert r.json()['applied'] is False                                 # 等于已应用值
    r, _ = disable(client, aid, 3)
    assert r.json()['applied'] is False                                 # 小于已应用值（两种指令共用序号）
    assert rows(db, 'SELECT status FROM accounts')[0]['status'] == 'active'
    assert login(make_client_like(client), password=TEMP).status_code == 303
    assert disable(client, aid, 6)[0].json()['applied'] is True


def test_reset_on_disabled_account_is_not_applied(client, db):
    _, aid = signed_up(client, db)
    assert disable(client, aid, 1)[0].json()['applied'] is True
    r, _ = reset(client, aid, 2)
    assert r.status_code == 200 and r.json()['applied'] is False
    assert rows(db, 'SELECT account_rev FROM accounts')[0]['account_rev'] == 2


def test_sequences_are_per_account(client, db):
    _, a = signed_up(client, db)
    _, b = signed_up(client, db, username='lily_sg', nickname='Lily')
    assert disable(client, a, 7)[0].json()['applied'] is True
    assert disable(client, b, 1)[0].json()['applied'] is True


@pytest.mark.parametrize('kind,patch', [
    ('disable_account', {'account_id': 'NOT-AN-ID'}),
    ('disable_account', {'account_id': 'a' * 32 + '\n'}),
    ('disable_account', {'target_seq': 0}),
    ('disable_account', {'temp_password': TEMP}),                       # 停用不带密码
    ('reset_password', {'temp_password': 'short'}),
    ('reset_password', {'temp_password': 'Hk7mPq2Rx9T0'}),              # 含易混字符 0
    ('reset_password', {'temp_password': TEMP + '\n'}),
    ('reset_password', {'command_id': 'X' * 36}),
])
def test_bad_command_payloads_rejected(client, kind, patch):
    payload = {'schema_version': 1, 'command_id': str(uuid.uuid4()), 'target_seq': 1, 'account_id': 'a' * 32}
    if kind == 'reset_password':
        payload['temp_password'] = TEMP
    payload.update(patch)
    env = {'schema_version': 1, 'event_id': str(uuid.uuid4()), 'kind': kind,
           'created_at': '2026-11-03T08:00:00.000Z', 'payload': payload}
    r = client.post('/admin/events', json=env, headers=AUTH)
    assert r.status_code == 400 and '不合契约' in r.json()['detail']
