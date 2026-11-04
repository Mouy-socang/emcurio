"""故事 31：喜欢 / 想要与阅读数——服务器一侧（TestClient，不连任何外部主机）。

覆盖 I/O 矩阵：点喜欢（实心、+1、记一条事件）、再点取消（−1、记取消事件）、刷新 / 换设备仍显示已点、停用账号 403、
已售出单件页不显示按钮（数字可显示）、预览按钮不可点、重复快速点以最后状态为准且计数不重复；另有三个账号喜欢同一件时
首页卡片显示「♡ 3」且不显示是谁、阅读数去重与"不到 10 不显示"、想要不公开计数、拍卖中一栏不带按钮、跨站提交拒绝。
"""
import json
import re
import sqlite3
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as server
import inbox
import preview
from test_item_page import NOW, SAME_ORIGIN, TOKEN, login, make_client, publish, register
from test_snapshot import Clock, hide, send

SCHEMAS = Path(__file__).resolve().parents[2] / 'contract' / 'schemas'


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def db(tmp_path):
    return tmp_path / 'server.db'


@pytest.fixture
def client(db, clock):
    c = make_client(db, clock)
    publish(c)
    register(c)
    return c


def mark(client, kind='like', on=True, code='CO-0001', js=True, next_path=None):
    headers = {'Accept': 'application/json'} if js else {}
    data = {'code': code, 'kind': kind, 'on': '1' if on else '0', 'next': next_path or '/item/' + code}
    return client.post('/mark', data=data, headers=headers)


def events(db):
    with sqlite3.connect(db) as c:
        return [json.loads(r[0]) for r in c.execute('SELECT event FROM inbox ORDER BY inbox_seq')]


def marks_of(db):
    return [e for e in events(db) if e['type'] in ('like', 'want')]


def box(html, code):
    """单件 / 卡片里该货号的按钮区（第一个）。"""
    m = re.search(r'<div class="mk" data-marks data-code="%s".*?</div>' % code, html, re.S)
    return m.group(0) if m else None


def other(db, clock, username):
    c = make_client(db, clock)
    register(c, username=username, first=False)
    return c


def account_id(db, username='wangjie'):
    with sqlite3.connect(db) as c:
        return c.execute('SELECT account_id FROM accounts WHERE username=?', (username,)).fetchone()[0]


def test_like_then_unlike_counts_and_events(client, db):
    html = client.get('/item/CO-0001').text
    b = box(html, 'CO-0001')
    assert b and 'aria-pressed="false"' in b and '喜欢' in b and '想要这件' in b and '♡ ' not in b.split('data-mk-count')[1]
    r = mark(client)
    assert r.status_code == 200 and r.json() == {'ok': True, 'code': 'CO-0001', 'kind': 'like', 'on': True, 'likes': 1,
                                                 'liked': True, 'wanted': False}
    b = box(client.get('/item/CO-0001').text, 'CO-0001')
    assert 'mk-like on' in b and '已喜欢' in b and '♥' in b and '♡ 1' in b
    r = mark(client, on=False)
    assert r.json()['likes'] == 0 and r.json()['on'] is False
    b = box(client.get('/item/CO-0001').text, 'CO-0001')
    assert 'mk-like on' not in b and '♡ 1' not in b
    got = marks_of(db)
    assert [(e['type'], e['co_code'], e['params']) for e in got] == [
        ('like', 'CO-0001', {'on': True}), ('like', 'CO-0001', {'on': False})]
    assert all(e['account_id'] == account_id(db) and uuid.UUID(e['event_id']) for e in got)


def test_repeat_same_state_is_idempotent(client, db):
    for _ in range(4):                         # 连点：每次带的是目标状态，同一状态只算一次
        assert mark(client).json()['likes'] == 1
    assert mark(client, on=False).json()['likes'] == 0
    assert mark(client, on=False).json()['likes'] == 0
    assert mark(client).json()['likes'] == 1   # 以最后状态为准
    assert [e['params']['on'] for e in marks_of(db)] == [True, False, True]
    with sqlite3.connect(db) as c:
        assert c.execute('SELECT count(*) FROM item_marks').fetchone()[0] == 1


def test_like_and_want_are_independent_and_want_not_public(client, db, clock):
    assert mark(client, 'want').json() == {'ok': True, 'code': 'CO-0001', 'kind': 'want', 'on': True, 'likes': 0,
                                           'liked': False, 'wanted': True}
    mark(client, 'like')
    mark(client, 'like', on=False)
    b = box(client.get('/item/CO-0001').text, 'CO-0001')
    assert 'mk-want on' in b and '已想要' in b and 'mk-like on' not in b
    # 别的客人看不到想要的人数
    b2 = box(other(db, clock, 'lisi').get('/item/CO-0001').text, 'CO-0001')
    assert 'mk-want on' not in b2 and '想要 1' not in b2 and '♡' not in b2.split('data-mk-count')[1]
    assert [(e['type'], e['params']['on']) for e in marks_of(db)] == [('want', True), ('like', True), ('like', False)]


def test_state_survives_new_device(client, db, clock):
    mark(client)
    mark(client, 'want')
    fresh = make_client(db, clock)
    login(fresh)
    home = fresh.get('/').text
    b = box(home, 'CO-0001')
    assert 'mk-like on' in b and 'mk-want on' in b and '♡ 1' in b


def test_three_accounts_like_shows_three_without_names(client, db, clock):
    mark(client)
    for name in ('lisi', 'zhaowu'):
        assert mark(other(db, clock, name)).status_code == 200
    viewer = other(db, clock, 'qianliu')
    home = viewer.get('/').text
    b = box(home, 'CO-0001')
    assert '♡ 3' in b and 'mk-like on' not in b
    for who in ('wangjie', 'lisi', 'zhaowu', '王姐'):
        assert who not in b
    # 今日榜卡与商品池卡都带（CO-0001 两处都在）
    assert len(re.findall(r'data-marks data-code="CO-0001"', home)) == 2
    assert all('♡ 3' in m for m in re.findall(r'<div class="mk" data-marks data-code="CO-0001".*?</div>', home, re.S))
    # 续载片段同样带数字
    batch = viewer.get('/api/pool?offset=0').json()['html']
    assert '♡ 3' in box(batch, 'CO-0001')


def test_reads_dedup_and_threshold(client, db, clock):
    for _ in range(3):
        client.get('/item/CO-0002')
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT count(*) FROM item_reads WHERE co_code='CO-0002'").fetchone()[0] == 1
    guests = [other(db, clock, f'kehu{k}') for k in range(8)]
    for g in guests:
        g.get('/item/CO-0002')
    assert '阅读' not in box(client.get('/item/CO-0002').text, 'CO-0002')      # 9 人：不到 10 不显示
    other(db, clock, 'kehu9').get('/item/CO-0002')
    assert '阅读 10' in box(client.get('/item/CO-0002').text, 'CO-0002')
    assert '阅读 10' in box(client.get('/').text, 'CO-0002')
    assert '阅读' not in box(client.get('/').text, 'CO-0003')
    # 只数未停用的账号：停用一位读者后不到 10，不再显示
    cmd = {'schema_version': 1, 'command_id': str(uuid.uuid4()), 'target_seq': 1, 'account_id': account_id(db, 'kehu9')}
    assert send(client, 'disable_account', cmd).json()['applied'] is True
    assert '阅读' not in box(client.get('/item/CO-0002').text, 'CO-0002')


def test_disabled_account_gets_403(client, db):
    mark(client)
    cmd = {'schema_version': 1, 'command_id': str(uuid.uuid4()), 'target_seq': 1, 'account_id': account_id(db)}
    assert send(client, 'disable_account', cmd).json()['applied'] is True
    client.post('/login', data={'username': 'wangjie', 'password': 'meissen1710', 'next': '/'})
    r = mark(client, on=False)
    assert r.status_code == 403 and r.json()['ok'] is False and '停用' in r.json()['detail']
    r = mark(client, on=False, js=False)
    assert r.status_code == 403 and '停用' in r.text
    with sqlite3.connect(db) as c:
        assert c.execute('SELECT "on" FROM item_marks').fetchone()[0] == 1       # 不生效
    assert [e['params']['on'] for e in marks_of(db)] == [True]


def test_disabled_likes_drop_from_public_count(client, db, clock):
    mark(client)
    mark(other(db, clock, 'lisi'))
    cmd = {'schema_version': 1, 'command_id': str(uuid.uuid4()), 'target_seq': 1, 'account_id': account_id(db, 'lisi')}
    send(client, 'disable_account', cmd)
    assert '♡ 1' in box(client.get('/item/CO-0001').text, 'CO-0001')


def test_no_js_form_redirects_back(client, db):
    r = mark(client, js=False, next_path='/?form=FN-001')
    assert r.status_code == 303 and r.headers['location'] == '/?form=FN-001'
    r = mark(client, js=False, next_path='https://evil.example/')
    assert r.status_code == 303 and r.headers['location'] == '/'
    html = client.get('/item/CO-0001').text
    # 已喜欢：表单里带的是"取消"（on=0）
    assert re.search(r'name="kind" value="like"><input type="hidden" name="on" value="0">', html)


def test_guards(client, db, clock):
    assert mark(client, kind='share').status_code == 400
    assert client.post('/mark', data={'code': 'CO-1', 'kind': 'like', 'on': '1'},
                       headers={'Accept': 'application/json'}).status_code == 400
    assert client.post('/mark', data={'code': 'CO-0001', 'kind': 'like', 'on': 'yes'},
                       headers={'Accept': 'application/json'}).status_code == 400
    r = client.post('/mark', data={'code': 'CO-0001', 'kind': 'like', 'on': '1'},
                    headers={'Accept': 'application/json', 'Origin': 'https://evil.example'})
    assert r.status_code == 403
    anon = TestClient(server.create_app(db_path=db, admin_token=TOKEN, clock=clock, cookie_secure=False,
                                        public_url='https://curio.test'), follow_redirects=False, headers=SAME_ORIGIN)
    assert mark(anon).status_code == 401
    r = mark(anon, js=False)
    assert r.status_code == 303 and r.headers['location'].startswith('/login?next=')
    assert mark(client, code='CO-9999').status_code == 404                      # 不在展出
    assert marks_of(db) == []


def test_sold_item_page_has_numbers_but_no_buttons(client, db, clock):
    mark(client)
    assert hide(client, 'CO-0001', 2).status_code == 200
    html = client.get('/item/CO-0001').text
    assert '已售出' in html
    b = box(html, 'CO-0001')
    assert b and '♡ 1' in b and '<form' not in b and '<button' not in b
    assert mark(client, on=False).status_code == 404                           # 已售出：不能再改
    # 没人喜欢、阅读不足时整块不出现
    hide(client, 'CO-0002', 3)
    assert box(client.get('/item/CO-0002').text, 'CO-0002') is None


def test_auction_column_has_no_buttons(db, clock):
    from test_snapshot import build_v2
    c = make_client(db, clock)
    payload, normal, auction = build_v2(c, clock, 1)
    assert send(c, 'snapshot', payload).status_code == 200
    register(c)
    html = c.get('/').text
    sec = html[html.index('id="auction"'):html.index('id="pool"')]
    assert 'data-marks' not in sec
    assert 'data-marks data-code="%s"' % normal[0]['code'] in html
    # 拍卖货单件页正在展出：照常可点
    assert mark(c, code=auction[0]['code']).status_code == 200


def test_preview_buttons_disabled_and_no_request_targets():
    from test_snapshot import make_item, build
    items = [make_item(k, [('a' * 64 + '.jpg', 'b' * 64 + '.jpg')]) for k in range(1, 4)]
    payload = build(1, items)
    home = preview.render(payload, '/')['body']
    b = box(home, 'CO-0001')
    assert b and 'disabled' in b and '<form' not in b and '/mark' not in home
    item = preview.render(payload, '/item/CO-0001')['body']
    b = box(item, 'CO-0001')
    assert b and b.count('disabled') == 2 and '/mark' not in item and 'data-track-page' not in item
    batch = preview.render(payload, '/api/pool')['body']['html']
    assert 'disabled' in box(batch, 'CO-0001') and '/mark' not in batch


def test_events_validate_against_contract(client, db):
    import jsonschema
    mark(client)
    mark(client, 'want')
    schema = json.loads((SCHEMAS / 'behavior_event.v1.schema.json').read_text(encoding='utf-8'))
    for e in marks_of(db):
        jsonschema.validate(e, schema)
        jsonschema.validate(e, schema['$defs'][e['type']])
    assert set(schema['$defs']) == set(schema['properties']['type']['enum'])


def test_client_uploaded_like_events_rejected(client, db):
    """like / want 只由 POST /mark 记（服务器生成）：客人页直接上报的一律拒收、计入 rejected，不进收件箱。"""
    from test_inbox import ev, post
    for e in (ev('like', 'CO-0001', on=True), ev('want', 'CO-0002', on=True), ev('want', None, on=True)):
        r = post(client, e)
        assert r.status_code == 400 and r.json()['rejected'] == [e['event_id']]
    r = post(client, ev('home_open'), ev('want', 'CO-0003', on=True))
    assert r.status_code == 200 and r.json()['accepted'] == 1 and len(r.json()['rejected']) == 1
    assert marks_of(db) == []
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT n FROM inbox_stats WHERE k='rejected'").fetchone()[0] == 4
        assert c.execute('SELECT count(*) FROM item_marks').fetchone()[0] == 0


def test_event_not_recorded_restores_state_503(client, db, clock):
    """限频用尽：事件被丢弃，状态改回去、回 503（无脚本时回原页），服务器与 Mac 账本不会不一致。"""
    mark(client)                                              # 先喜欢上（记了一条）
    with sqlite3.connect(db) as c:
        c.execute('INSERT OR REPLACE INTO inbox_rate(account_id,hour,n) VALUES(?,?,?)',
                  (account_id(db), int(clock.now // 3600), inbox.RATE_PER_HOUR))
    r = mark(client, on=False)
    assert r.status_code == 503 and r.json() == {'ok': False, 'detail': '未能保存，请稍后再试'}
    r = mark(client, 'want', js=False)
    assert r.status_code == 303 and r.headers['location'] == '/item/CO-0001'
    b = box(client.get('/item/CO-0001').text, 'CO-0001')
    assert 'mk-like on' in b and 'mk-want on' not in b and '♡ 1' in b
    with sqlite3.connect(db) as c:
        assert dict(c.execute('SELECT kind,"on" FROM item_marks').fetchall()) == {'like': 1, 'want': 0}
    assert [(e['type'], e['params']['on']) for e in marks_of(db)] == [('like', True)]


def test_static_script_has_mark_logic(client):
    js = client.get('/static/guest.js').text
    assert "fetch('/mark'" in js and '/preview' in js and 'data-who' in js
