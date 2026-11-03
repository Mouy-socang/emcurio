"""故事 15（CAP-15；AD-9、AD-25）：行为收件箱——服务器一侧（TestClient，不连任何外部主机）。

覆盖 I/O 矩阵的服务器部分：点开单件（item_open from rank）、停留（45 秒；超过 1800 截为 1800）、重复发送只一条
（含 Mac 确认删除之后）、未知类型 400、停用账号与未登录拒收、拉取按序 + 确认只删 ≤ max_seq（幂等）、
30 天未确认删除并记日志；另有每批 50 条、每账号每小时 600 条、预览 / 停用页不带行为标记。
"""
import json
import logging
import re
import sqlite3
import uuid
from pathlib import Path

import pytest

import app as server
import guest as guest_mod
import inbox
from test_item_page import publish, register
from test_snapshot import AUTH, Clock, client, clock, db, send  # noqa: F401（fixtures）

SCHEMAS = Path(__file__).resolve().parents[2] / 'contract' / 'schemas'


def ev(type_, code=None, **params):
    return {'event_id': str(uuid.uuid4()), 'type': type_, 'co_code': code, 'client_ts': '2026-11-03T08:00:00.000Z',
            'params': params, 'event_schema_version': 1}


def post(client, *events, **kw):
    return client.post('/events', json={'schema_version': 1, 'events': list(events)}, **kw)


def pull(client, after=0, limit=None):
    q = f'?after={after}' + (f'&limit={limit}' if limit else '')
    r = client.get('/admin/inbox' + q, headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()


def ack(client, max_seq):
    return client.post('/admin/inbox/ack', json={'schema_version': 1, 'max_seq': max_seq}, headers=AUTH)


@pytest.fixture
def guest(client):
    """已注册并登录的客人（有快照）。返回其 account_id。"""
    publish(client)
    register(client)
    with sqlite3.connect(client.app.state.db_path) as c:
        return c.execute('SELECT account_id FROM accounts').fetchone()[0]


def rows(db):
    with sqlite3.connect(db) as c:
        return [json.loads(r[0]) for r in c.execute('SELECT event FROM inbox ORDER BY inbox_seq')]


def test_item_open_from_rank_lands_in_inbox_with_server_fields(client, guest, db, clock):
    e = dict(ev('item_open', 'CO-0012', **{'from': 'rank'}), account_id='f' * 32, server_ts='2000-01-01T00:00:00.000Z')
    r = post(client, e)
    assert r.status_code == 200 and r.json() == {'ok': True, 'accepted': 1, 'duplicates': 0, 'dropped': 0, 'rejected': []}
    [got] = rows(db)
    assert got['account_id'] == guest                    # 服务器按会话填，不信客户端
    assert got['server_ts'] == inbox.iso(clock.now)      # 入箱时写
    assert got['type'] == 'item_open' and got['co_code'] == 'CO-0012' and got['params'] == {'from': 'rank'}


def test_dwell_seconds_and_clamp(client, guest, db):
    assert post(client, ev('item_dwell', 'CO-0001', seconds=45), ev('item_dwell', 'CO-0002', seconds=5000)).status_code == 200
    assert [e['params']['seconds'] for e in rows(db)] == [45, 1800]


def test_all_five_types_accepted(client, guest, db):
    r = post(client, ev('home_open', brand='Meissen', band='lt200'), ev('item_open', 'CO-0001', **{'from': 'link'}),
             ev('item_dwell', 'CO-0001', seconds=3), ev('filter', form='FN-001'), ev('currency', currency='CNY'))
    assert r.json()['accepted'] == 5
    assert [e['type'] for e in rows(db)] == ['home_open', 'item_open', 'item_dwell', 'filter', 'currency']


def test_duplicate_event_id_kept_once_even_after_ack(client, guest, db):
    e = ev('item_open', 'CO-0003', **{'from': 'pool'})
    assert post(client, e).json()['accepted'] == 1
    assert post(client, e).json() == {'ok': True, 'accepted': 0, 'duplicates': 1, 'dropped': 0, 'rejected': []}
    assert post(client, e, e).json()['duplicates'] == 2
    assert len(rows(db)) == 1
    assert ack(client, pull(client)['events'][-1]['inbox_seq']).json()['deleted'] == 1
    assert post(client, e).json()['duplicates'] == 1 and rows(db) == []      # 确认删除后重发也不再入箱


@pytest.mark.parametrize('bad', [
    ev('share', 'CO-0001'),                                       # 枚举外类型
    ev('item_open', None, **{'from': 'rank'}),                   # 点开必须有货号
    ev('item_open', 'CO-0001', **{'from': 'mail'}),
    ev('home_open', 'CO-0001'),                                  # 首页没有货号
    ev('filter', colour='blue'),                                 # 未定义的筛选键
    ev('currency', currency='USD'),
    ev('item_dwell', 'CO-0001', seconds=0),
    ev('item_dwell', 'CO-0001', seconds=1.5),
    dict(ev('home_open'), event_id='not-a-uuid'),
    dict(ev('home_open'), event_id=str(uuid.uuid4()) + '\n'),
    dict(ev('home_open'), client_ts='yesterday'),
    dict(ev('home_open'), event_schema_version=2),
    dict(ev('home_open'), extra=1),
    dict(ev('home_open'), type=['home_open']),                   # 不可哈希的 type：不得 500
    dict(ev('home_open'), type={'a': 1}),
    'not-an-object',
])
def test_bad_event_rejected_400(client, guest, db, bad):
    r = post(client, bad)
    assert r.status_code == 400 and r.json()['ok'] is False and r.json().get('accepted', 0) == 0
    assert rows(db) == []


def test_mixed_batch_keeps_good_and_lists_rejected(client, guest, db):
    bad = ev('share')
    r = post(client, ev('home_open'), bad)
    assert r.status_code == 200 and r.json()['accepted'] == 1 and r.json()['rejected'] == [bad['event_id']]
    assert len(rows(db)) == 1


def test_batch_limit_and_body_shape(client, guest):
    assert post(client, *[ev('home_open') for _ in range(51)]).status_code == 400
    assert post(client, *[ev('home_open') for _ in range(50)]).json()['accepted'] == 50
    assert client.post('/events', json={'schema_version': 1, 'events': []}).status_code == 400
    assert client.post('/events', json={'schema_version': 2, 'events': [ev('home_open')]}).status_code == 400
    assert client.post('/events', content=b'{', headers={'Content-Type': 'application/json'}).status_code == 400
    assert client.post('/events', content=b'x', headers={'Content-Type': 'application/x-www-form-urlencoded'}).status_code == 415


def test_beacon_text_plain_and_cross_site(client, guest, db):
    body = json.dumps({'schema_version': 1, 'events': [ev('item_dwell', 'CO-0001', seconds=9)]})
    r = client.post('/events', content=body, headers={'Content-Type': 'text/plain;charset=UTF-8'})
    assert r.status_code == 200 and r.json()['accepted'] == 1
    r = client.post('/events', content=body, headers={'Content-Type': 'text/plain', 'Origin': 'https://evil.example'})
    assert r.status_code == 403


def test_not_logged_in_rejected(client):
    publish(client)
    assert post(client, ev('home_open')).status_code == 401


def test_disabled_account_rejected(client, guest, db):
    cmd = {'schema_version': 1, 'command_id': str(uuid.uuid4()), 'target_seq': 1, 'account_id': guest}
    assert send(client, 'disable_account', cmd).json()['applied'] is True
    assert post(client, ev('home_open')).status_code == 401          # 会话已失效
    r = client.post('/login', data={'username': 'wangjie', 'password': 'meissen1710', 'next': '/'})
    assert r.status_code == 303
    page = client.get('/')
    assert page.status_code == 403 and 'data-track-page' not in page.text      # 停用页不带行为标记
    assert post(client, ev('home_open')).status_code == 403          # 即使重新登录也不记录
    assert rows(db) == []


def test_pages_carry_track_marker(client, guest):
    home = client.get('/?band=lt200').text
    assert 'data-track-page="home"' in home and "data-params='{\"band\": \"lt200\"}'" in home
    assert re.search(r'data-track-page="item" data-who="[0-9a-f]{16}" data-code="CO-0001"', client.get('/item/CO-0001').text)
    assert 'data-track-page' not in client.get('/item/CO-9999').text     # 不存在的货号不记


def test_rate_limit_per_account_per_hour(client, guest, db, clock, caplog):
    monkey = inbox.RATE_PER_HOUR
    inbox.RATE_PER_HOUR = 5
    try:
        clock.now = 1_800_000_000.0 - 1_800_000_000.0 % 3600 + 10     # 整点后 10 秒
        assert post(client, *[ev('home_open') for _ in range(4)]).json()['accepted'] == 4
        with caplog.at_level(logging.WARNING, logger='curiobject.server'):
            r = post(client, *[ev('home_open') for _ in range(3)]).json()
        assert r == {'ok': True, 'accepted': 1, 'duplicates': 0, 'dropped': 2, 'rejected': []}
        assert '丢弃 2 条' in caplog.text
        with sqlite3.connect(db) as c:
            assert inbox.stats(c)['rate_dropped'] == 2
        clock.now += 3600                                              # 下一个小时
        assert post(client, ev('home_open')).json()['accepted'] == 1
    finally:
        inbox.RATE_PER_HOUR = monkey


def test_pull_in_order_with_paging_and_ack_only_up_to_max(client, guest, db):
    for k in range(7):
        post(client, ev('item_dwell', 'CO-0001', seconds=k + 1))
    page = pull(client, limit=3)
    seqs = [e['inbox_seq'] for e in page['events']]
    assert seqs == sorted(seqs) and len(seqs) == 3 and page['more'] is True
    page2 = pull(client, after=seqs[-1], limit=500)
    assert len(page2['events']) == 4 and page2['more'] is False
    assert [e['event']['params']['seconds'] for e in page['events'] + page2['events']] == [1, 2, 3, 4, 5, 6, 7]
    # 合契约
    from jsonschema import Draft202012Validator
    Draft202012Validator(json.loads((SCHEMAS / 'inbox_batch.v1.schema.json').read_text())).validate(page)
    assert ack(client, seqs[-1]).json() == {'ok': True, 'max_seq': seqs[-1], 'deleted': 3}
    assert ack(client, seqs[-1]).json()['deleted'] == 0                # 幂等
    assert [e['inbox_seq'] for e in pull(client)['events']] == [e['inbox_seq'] for e in page2['events']]
    # 删除后序号仍单调，不复用
    post(client, ev('home_open'))
    assert pull(client)['events'][-1]['inbox_seq'] > page2['events'][-1]['inbox_seq']


def test_inbox_admin_needs_token_and_valid_input(client):
    assert client.get('/admin/inbox').status_code == 401
    assert client.post('/admin/inbox/ack', json={'schema_version': 1, 'max_seq': 1}).status_code == 401
    assert client.get('/admin/inbox?after=-1', headers=AUTH).status_code == 400
    assert client.get('/admin/inbox?limit=abc', headers=AUTH).status_code == 400
    assert client.get('/admin/inbox?limit=9999', headers=AUTH).status_code == 200      # 截到 500
    for body in ({'schema_version': 1}, {'schema_version': 1, 'max_seq': -1}, {'schema_version': 2, 'max_seq': 1}, []):
        assert client.post('/admin/inbox/ack', json=body, headers=AUTH).status_code == 400


def test_unacked_events_removed_after_30_days_and_logged(client, guest, db, clock, caplog):
    post(client, ev('home_open'), ev('currency', currency='EUR'))
    clock.now += 29 * 86400
    assert len(pull(client)['events']) == 2
    clock.now += 2 * 86400
    with caplog.at_level(logging.WARNING, logger='curiobject.server'):
        assert pull(client)['events'] == []
    assert '2 条事件超过 30 天' in caplog.text
    with sqlite3.connect(db) as c:
        assert inbox.stats(c)['expired'] == 2


def test_unhashable_type_counted_as_rejected(client, guest, db):
    r = post(client, dict(ev('home_open'), type=['x']), ev('share'))
    assert r.status_code == 400
    with sqlite3.connect(db) as c:
        assert inbox.stats(c)['rejected'] == 2
    assert pull(client)['stats'] == {'expired': 0, 'rate_dropped': 0, 'rejected': 2}


def test_non_ascii_digits_are_400_not_500(client, guest):
    assert client.get('/admin/inbox?after=²', headers=AUTH).status_code == 400
    assert client.get('/admin/inbox?limit=١٢', headers=AUTH).status_code == 400


def test_body_over_limit_is_413_with_or_without_content_length(client, guest, db):
    big = json.dumps({'schema_version': 1, 'events': [ev('home_open')], 'pad': 'x' * 70_000}).encode()
    assert client.post('/events', content=big, headers={'Content-Type': 'application/json'}).status_code == 413

    def chunks():                                              # 分块传输：没有 Content-Length
        for _ in range(20):
            yield b' ' * 4096
    r = client.post('/events', content=chunks(), headers={'Content-Type': 'application/json'})
    assert r.status_code == 413 and rows(db) == []


def test_sec_fetch_site_without_origin(client, guest, db):
    body = json.dumps({'schema_version': 1, 'events': [ev('home_open')]})
    origin = client.headers.pop('origin')
    try:
        r = client.post('/events', content=body, headers={'Content-Type': 'text/plain', 'Sec-Fetch-Site': 'cross-site'})
        assert r.status_code == 403
        # 既无 Origin 也无 Sec-Fetch-Site（部分内置浏览器的 sendBeacon）：放行——会话 Cookie 是 SameSite=Lax，
        # 跨站 POST 本来带不上会话。
        r = client.post('/events', content=body, headers={'Content-Type': 'text/plain'})
        assert r.status_code == 200 and r.json()['accepted'] == 1
    finally:
        client.headers['origin'] = origin


def test_outdated_terms_consent_rejected(client, guest, db, clock, tmp_path):
    terms = tmp_path / 'terms.md'
    terms.write_text(guest_mod.DEFAULT_TERMS.read_text(encoding='utf-8').replace(
        'version: 2026-11-03-draft-2', 'version: 2099-01-01'), encoding='utf-8')
    from fastapi.testclient import TestClient
    newer = TestClient(server.create_app(db_path=db, admin_token='t', clock=clock, cookie_secure=False,
                                         public_url='https://curio.test', terms_path=terms),
                       follow_redirects=False, headers={'Origin': 'http://testserver'}, cookies=client.cookies)
    assert newer.app.state.terms_version == '2099-01-01'
    r = newer.post('/events', json={'schema_version': 1, 'events': [ev('home_open')]})
    assert r.status_code == 403 and rows(db) == []


def test_currency_recorded_by_server_once_per_change(client, guest, db):
    from test_item_page import choose
    assert choose(client, 'CNY').status_code == 200                    # 脚本：Accept JSON
    assert choose(client, 'CNY').status_code == 200                    # 值没变：不再记
    assert choose(client, 'EUR', js=False).status_code == 303          # 无脚本表单提交也记
    got = rows(db)
    assert [(e['type'], e['params'], e['account_id']) for e in got] == [
        ('currency', {'currency': 'CNY'}, guest), ('currency', {'currency': 'EUR'}, guest)]
    assert all(e['co_code'] is None and uuid.UUID(e['event_id']) for e in got)


def test_disabled_account_currency_not_recorded(client, guest, db):
    cmd = {'schema_version': 1, 'command_id': str(uuid.uuid4()), 'target_seq': 1, 'account_id': guest}
    assert send(client, 'disable_account', cmd).json()['applied'] is True
    client.post('/login', data={'username': 'wangjie', 'password': 'meissen1710', 'next': '/'})
    from test_item_page import choose
    assert choose(client, 'CNY').status_code == 403
    assert rows(db) == []


def test_track_marker_is_opaque_per_account(client, guest):
    import accounts
    html = client.get('/').text
    tag = accounts.track_tag(guest)
    assert f'data-who="{tag}"' in html and len(tag) == 16 and guest not in html
    assert accounts.track_tag('f' * 32) != tag
