"""故事 18（CAP-12，AD-11）：快照里按账号的商品池顺序——服务器校验与渲染（TestClient，不连任何外部主机）。

覆盖 I/O 矩阵的服务器一侧：两种口味、未关联、隐藏、集合差异、坏顺序、筛选、续载、旧快照、预览。
"""
import re
import sqlite3
import uuid

import pytest
from fastapi.testclient import TestClient

import app as server
import preview
import snapshot
from test_snapshot import AUTH, SAME_ORIGIN, TOKEN, Clock, build, hide, make_item, pool_codes, rank_codes, send


@pytest.fixture
def db(tmp_path):
    return tmp_path / 'server.db'


@pytest.fixture
def app(db):
    return server.create_app(db_path=db, admin_token=TOKEN, clock=Clock(), cookie_secure=False,
                             public_url='https://curio.test')


def browser(app):
    return TestClient(app, follow_redirects=False, headers=SAME_ORIGIN)


def invite(app):
    env = {'schema_version': 1, 'event_id': str(uuid.uuid4()), 'kind': 'set_invite_code',
           'created_at': '2026-11-03T08:00:00.000Z',
           'payload': {'schema_version': 1, 'command_id': str(uuid.uuid4()), 'target_seq': 1, 'code': 'MOMO2026'}}
    assert browser(app).post('/admin/events', json=env, headers=AUTH).status_code == 200


def guest(app, db, username):
    c = browser(app)
    r = c.post('/register', data={'invite': 'MOMO2026', 'username': username, 'password': 'meissen1710',
                                  'nickname': username, 'email': '', 'consent': 'yes', 'next': '/'})
    assert r.status_code == 303
    with sqlite3.connect(db) as con:
        account_id = con.execute('SELECT account_id FROM accounts WHERE username=?', (username,)).fetchone()[0]
    return c, account_id


@pytest.fixture
def scene(app, db):
    """30 件货（Meissen 与 KPM Berlin 交替出现）；A 的顺序 KPM 在前、B 的顺序倒过来、C 没有顺序。"""
    invite(app)
    a, aid = guest(app, db, 'guest_a')
    b, bid = guest(app, db, 'guest_b')
    c, _ = guest(app, db, 'guest_c')
    items = [make_item(n, []) for n in range(1, 31)]
    pool = [it['code'] for it in items]
    kpm = [it['code'] for it in items if it['brand'] == 'KPM Berlin']
    order_a = kpm + [x for x in pool if x not in kpm]
    order_b = list(reversed(pool))
    payload = build(1, items)
    payload['orders'] = {aid: order_a, bid: order_b}
    r = send(browser(app), 'snapshot', payload)
    assert r.status_code == 200 and r.json()['applied'] is True, r.text
    return dict(a=a, b=b, c=c, aid=aid, bid=bid, items=items, pool=pool, order_a=order_a, order_b=order_b,
                payload=payload)


def all_pool(client, query=''):
    """首页首批 + 续载到底的全部卡片货号。"""
    html = client.get('/?' + query if query else '/').text
    got = pool_codes(html)
    while True:
        sep = '&' if query else ''
        r = client.get(f'/api/pool?{query}{sep}offset={len(got)}').json()
        got += pool_codes(r['html'])
        if r['next_offset'] is None:
            return got, rank_codes(html)


def test_two_tastes_different_pool_order_same_rank_same_set(scene):
    pa, ra = all_pool(scene['a'])
    pb, rb = all_pool(scene['b'])
    pc, rc = all_pool(scene['c'])
    assert pa == scene['order_a'] and pb == scene['order_b'] and pc == scene['pool']    # 未关联：墨墨顺序
    assert ra == rb == rc == scene['pool'][:10]                                            # 今日榜人人相同
    assert sorted(pa) == sorted(pb) == sorted(pc) and len(pa) == 30


def test_filter_keeps_personal_order(scene):
    got, _ = all_pool(scene['a'], 'brand=Meissen')
    meissen = [it['code'] for it in scene['items'] if it['brand'] == 'Meissen']
    assert got == [x for x in scene['order_a'] if x in meissen]
    got_b, _ = all_pool(scene['b'], 'brand=Meissen')
    assert got_b == [x for x in scene['order_b'] if x in meissen]


def test_hidden_codes_skipped_rest_keep_order(scene, app):
    first = scene['order_a'][0]
    assert hide(browser(app), first, 5).status_code == 200
    got, _ = all_pool(scene['a'])
    assert first not in got and got == scene['order_a'][1:]


def test_order_missing_items_go_last_in_moomoo_order(app, db):
    invite(app)
    a, aid = guest(app, db, 'guest_a')
    items = [make_item(n, []) for n in range(1, 16)]
    pool = [it['code'] for it in items]
    payload = build(1, items)
    payload['orders'] = {aid: [pool[9], pool[3]]}           # 只排了两件
    assert send(browser(app), 'snapshot', payload).json()['applied'] is True
    got, _ = all_pool(a)
    assert got == [pool[9], pool[3]] + [x for x in pool if x not in (pool[9], pool[3])]


def test_old_snapshot_without_orders_everyone_moomoo_order(app, db):
    invite(app)
    a, _ = guest(app, db, 'guest_a')
    items = [make_item(n, []) for n in range(1, 16)]
    assert send(browser(app), 'snapshot', build(1, items)).json()['applied'] is True
    assert all_pool(a)[0] == [it['code'] for it in items]


def test_new_snapshot_without_account_drops_its_order(scene, app):
    """停用 / 不再有记录：下一份快照不带该账号，客人回到墨墨顺序。"""
    payload = dict(scene['payload'], publish_no=2, hide_watermark=2, orders={scene['bid']: scene['order_b']})
    assert send(browser(app), 'snapshot', payload).json()['applied'] is True
    assert all_pool(scene['a'])[0] == scene['pool']
    assert all_pool(scene['b'])[0] == scene['order_b']


@pytest.mark.parametrize('mutate, detail', [
    (lambda p, a: p['orders'].update({a: p['pool'][:3] + ['CO-9999']}), '不在商品池'),
    (lambda p, a: p['orders'].update({a: [p['pool'][0], p['pool'][1], p['pool'][0]]}), '不符合要求'),
    (lambda p, a: p['orders'].update({'not-an-account': p['pool'][:2]}), '不符合要求'),
    (lambda p, a: p['orders'].update({a.upper(): p['pool'][:2]}), '不符合要求'),
    (lambda p, a: p['orders'].update({a: 'CO-0001'}), '不符合要求'),
    (lambda p, a: p['orders'].update({a: ['co-0001']}), '不符合要求'),
    (lambda p, a: p.update(orders=[p['pool']]), '不符合要求'),
])
def test_bad_orders_reject_whole_snapshot(app, db, mutate, detail):
    client = browser(app)
    good = build(1, [make_item(n, []) for n in range(1, 12)])
    aid = uuid.uuid4().hex
    payload = dict(good, orders={aid: list(good['pool'])})
    mutate(payload, aid)
    r = send(client, 'snapshot', payload)
    assert r.status_code == 400 and detail in r.json()['detail'], r.text
    with sqlite3.connect(db) as con:
        assert con.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0] == 0


def test_view_for_account_unit():
    items = [make_item(n, []) for n in range(1, 6)]
    payload = build(1, items, rank=[it['code'] for it in items] * 2)
    aid = 'a' * 32
    payload['orders'] = {aid: ['CO-0005', 'CO-0002', 'CO-0099']}
    view = snapshot.View(1, None, payload, {'CO-0002': 'sold'})
    mine = view.for_account(aid)
    assert [it['code'] for it in mine.pool] == ['CO-0005', 'CO-0001', 'CO-0003', 'CO-0004']
    assert [it['code'] for it in view.pool] == ['CO-0001', 'CO-0003', 'CO-0004', 'CO-0005']     # 原视图不变
    assert view.for_account('b' * 32) is view and view.for_account(None) is view
    assert mine.rank == view.rank


def test_check_snapshot_semantic_checks_without_schema():
    """语义检查独立于 Schema 也拒收（两道关）。"""
    payload = build(1, [make_item(n, []) for n in range(1, 12)])
    for bad in ({'A' * 32: payload['pool'][:1]}, {'a' * 32: ['CO-0001', 'CO-0001']}, {'a' * 32: ['CO-9999']}):
        with pytest.raises(snapshot.BadSnapshot):
            snapshot.check_snapshot(dict(payload, orders=bad))


def test_preview_renders_full_pool_in_moomoo_order_even_with_orders():
    items = [make_item(n, []) for n in range(1, 46)]
    payload = build(0, items)
    payload['orders'] = {'a' * 32: list(reversed(payload['pool']))}
    def cards(html):
        return re.findall(r'<li class="card"><a class="[a-z-]+" href="/preview/item/(CO-\d+)"', html)
    got = cards(preview.render(payload, '/')['body'])
    assert got
    for _ in range(10):
        body = preview.render(payload, '/api/pool', f'offset={len(got)}&pn=0')['body']
        assert body['reset'] is False and body['count'] > 0
        got += cards(body['html'])
        if body['next_offset'] is None:
            break
    assert got == payload['pool']


def test_load_more_after_new_snapshot_resets_from_top(scene, app):
    a = scene['a']
    html = a.get('/').text
    assert 'data-pn="1"' in html
    first = pool_codes(html)
    assert first == scene['order_a'][:20]
    same = a.get('/api/pool?offset=20&pn=1').json()
    assert same['reset'] is False and pool_codes(same['html']) == scene['order_a'][20:]
    # 续载途中发布了新快照（A 的顺序变了）：带旧编号续载 → 从头给第一批，reset。
    new_order = list(reversed(scene['order_a']))
    payload = dict(scene['payload'], publish_no=2, hide_watermark=2, orders={scene['aid']: new_order})
    assert send(browser(app), 'snapshot', payload).json()['applied'] is True
    r = a.get('/api/pool?offset=20&pn=1').json()
    assert r['reset'] is True and r['publish_no'] == 2
    assert pool_codes(r['html']) == new_order[:20] and r['next_offset'] == 20
    # 不带 pn（旧页面脚本）照旧按偏移
    assert a.get('/api/pool?offset=20').json()['reset'] is False
