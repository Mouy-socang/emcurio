"""故事 22：01 新货瀑布流（游标续载、排序、卡片不带分数）与墨墨行为记录（owner_events：去重、校验、截断、令牌）。
全部用临时库，不连外部主机；界面排版、IntersectionObserver 采集与 sendBeacon 见故事 Verification 的人工检查。"""
import json
import re
import threading
import urllib.error
import urllib.request

import pytest

from curio import triage
from curio.config import load
from curio.store import Store


@pytest.fixture
def cfg():
    return dict(load(), keywords=['Meissen'])


@pytest.fixture
def db(tmp_path):
    return Store(tmp_path / 'curio.db')


def item(i, title='Meissen Tasse', **kw):
    return dict(dict(item_id=i, platform='ebay', title=title, price=40, currency='EUR', buying='FIXED_PRICE',
                     url='https://www.ebay.de/itm/example', image='https://i.ebayimg.com/x.jpg'), **kw)


def seen_at(db, iid, ts):
    with db.connect() as c:
        c.execute('UPDATE items SET first_seen=? WHERE item_id=?', (ts, iid))


def score(db, iid, value):
    with db.connect() as c:
        c.execute("INSERT OR REPLACE INTO scores(item_id,score,source,reason,rule_version,scored_at) VALUES(?,?,'ai','x',1,'2026-01-01')",
                  (iid, value))


def stocked(db, cfg, n=5):
    """n 件新货，first_seen 依次更早（n0 最新）；最旧的那件分数最高；另有一件自动移出后被捞回。"""
    for k in range(n):
        db.ingest(item('n%d' % k), cfg)
        seen_at(db, 'n%d' % k, '2026-11-%02dT10:00:00+00:00' % (20 - k))
    score(db, 'n%d' % (n - 1), 99)       # 高分但最旧
    score(db, 'n0', 10)
    db.ingest(item('bad', title='Meissen defekt Tasse'), cfg)
    seen_at(db, 'bad', '2026-01-01T00:00:00+00:00')
    assert db.triage_state()['counts']['auto_removed'] == 1
    db.triage_act('rescue', 'bad', 'r-rescue')


@pytest.fixture
def http(db):
    from curio.review import server
    httpd = server(db, port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = 'http://127.0.0.1:' + str(httpd.server_port)
    html = urllib.request.urlopen(base + '/').read().decode()
    token = re.search("const token='([^']+)'", html)[1]

    def get(path):
        return json.load(urllib.request.urlopen(base + path))

    def post(path, body, tok=None, headers=None, raw=None):
        h = {'Content-Type': 'application/json', **(headers or {})}
        if tok is None:
            h['X-Curio-Token'] = token
        elif tok:
            h['X-Curio-Token'] = tok
        req = urllib.request.Request(base + path, raw if raw is not None else json.dumps(body).encode(), headers=h)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    yield dict(base=base, html=html, token=token, get=get, post=post)
    httpd.shutdown()
    httpd.server_close()


# ---------- 瀑布流 ----------

def test_feed_order_rescued_first_then_newest_not_by_score(db, cfg, http):
    stocked(db, cfg)
    d = http['get']('/api/triage/feed')
    ids = [c['item_id'] for c in d['items']]
    assert ids == ['bad', 'n0', 'n1', 'n2', 'n3', 'n4']      # 捞回的第一；高分的 n4 仍按上架时间排在最后
    assert d['next_cursor'] is None and d['counts'] == {'new': 6, 'auto_removed': 0, 'seen': 0, 'auction': 0, 'auction_soon': 0, 'auction_pool': 0, 'auction_rank': 0}
    for c in d['items']:
        # 故事 27：卡片带分数与理由（界面在点要或记为看到之后才显示，见 triage.js）；排序仍不按分数。
        assert {'item_id', 'name', 'price', 'platform', 'image', 'code', 'tags', 'score', 'score_reason'} <= set(c)
    # 详情里照旧显示分数
    assert http['get']('/api/triage/item?id=n4')['score'] == 99
    # 旧接口 /api/triage 的分数排序不变（其他调用方仍用）
    assert http['get']('/api/triage?limit=2')['queue'][1]['item_id'] == 'n4'


def test_feed_cursor_two_batches_no_duplicates(db, cfg, http):
    stocked(db, cfg)
    a = http['get']('/api/triage/feed?limit=4')
    assert len(a['items']) == 4 and a['next_cursor']
    b = http['get']('/api/triage/feed?limit=4&cursor=' + a['next_cursor'])
    ids = [c['item_id'] for c in a['items'] + b['items']]
    assert ids == ['bad', 'n0', 'n1', 'n2', 'n3', 'n4'] and len(set(ids)) == 6
    assert b['next_cursor'] is None


def test_feed_new_arrivals_do_not_shift_later_batches(db, cfg, http):
    stocked(db, cfg)
    a = http['get']('/api/triage/feed?limit=3')
    db.ingest(item('fresh'), cfg)                        # 浏览中采集入库：first_seen 为现在，排在最前
    seen_at(db, 'fresh', '2026-11-30T00:00:00+00:00')
    b = http['get']('/api/triage/feed?limit=3&cursor=' + a['next_cursor'])
    assert [c['item_id'] for c in a['items']] == ['bad', 'n0', 'n1']
    assert [c['item_id'] for c in b['items']] == ['n2', 'n3', 'n4']
    assert http['get']('/api/triage/feed?limit=3')['items'][1]['item_id'] == 'fresh'


def test_feed_same_set_as_queue_and_default_batch(db, cfg, http):
    for k in range(45):
        db.ingest(item('m%02d' % k), cfg)
        seen_at(db, 'm%02d' % k, '2026-11-01T10:00:00+00:00')     # 同一时间：按 item_id
    db.ingest(item('gone'), cfg)
    db.mark_gone('gone')
    db.ingest(item('kept'), cfg)
    db.triage_act('keep', 'kept', 'r-keep')
    d = http['get']('/api/triage/feed')
    assert len(d['items']) == 40 and d['next_cursor']
    rest = http['get']('/api/triage/feed?cursor=' + d['next_cursor'])
    ids = [c['item_id'] for c in d['items'] + rest['items']]
    assert ids == ['m%02d' % k for k in range(45)]
    with db.connect() as c:
        assert sorted(ids) == sorted(r['item_id'] for r in triage.queue(c, 200))


def test_feed_bad_params(http):
    for q in ('limit=0', 'limit=101', 'limit=x', 'cursor=@@', 'cursor=' + 'WzIsIiIsIiIsIiJd'):
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(http['base'] + '/api/triage/feed?' + q)
        assert e.value.code == 400


def test_page_is_waterfall_without_card_scores(http):
    html = http['html']
    assert 'id="feed"' in html and '<ol class="feed"' in html and '未能加载' in html and '今天的新货已处理完' in html
    assert 'columns:' not in html.replace('grid-template-columns', '')
    assert 'id="seenView"' in html and '看过未选' in html and '今日已留下' in html and '商品池共' in html and '放回' in html
    js = urllib.request.urlopen(http['base'] + '/triage.js').read().decode()
    assert '/api/triage/feed' in js and '/api/triage/seen' in js and "type: 'restore'" in js and '/api/owner-events' in js and 'sendBeacon' in js and 'IntersectionObserver' in js
    assert 'scoreText' not in js


# ---------- 行为记录 ----------

def ev(eid, kind, iid, **params):
    return dict(event_id=eid, type=kind, item_id=iid, client_ts='2026-11-04T10:00:00Z', session_id='s1',
                params=params or None)


def rows(db):
    with db.connect() as c:
        return {r['event_id']: dict(r) for r in c.execute('SELECT * FROM owner_events')}


def test_owner_events_store_dedupe_and_server_time(db, cfg, http):
    db.ingest(item('a'), cfg)
    batch = [ev('e1', 'seen', 'a'), ev('e2', 'open', 'a'), ev('e3', 'detail_dwell', 'a', seconds=40)]
    assert http['post']('/api/owner-events', dict(events=batch)) == (200, dict(stored=3, duplicate=0, dropped=0))
    assert http['post']('/api/owner-events', dict(events=batch[:1])) == (200, dict(stored=0, duplicate=1, dropped=0))
    r = rows(db)
    assert set(r) == {'e1', 'e2', 'e3'}
    assert r['e1']['type'] == 'seen' and r['e1']['session_id'] == 's1' and r['e1']['params'] is None
    assert r['e1']['client_ts'] == '2026-11-04T10:00:00Z' and r['e1']['at'].startswith('20') and r['e1']['at'] != r['e1']['client_ts']
    assert json.loads(r['e3']['params']) == {'seconds': 40.0}
    # 留下 / 去掉不进 owner_events
    db.triage_act('keep', 'a', 'r-k')
    assert len(rows(db)) == 3


def test_owner_events_dwell_clamped(db, cfg, http):
    db.ingest(item('a'), cfg)
    batch = [ev('d1', 'dwell', 'a', seconds=12), ev('d2', 'dwell', 'a', seconds=900), ev('d3', 'dwell', 'a', seconds=0.2),
             ev('d4', 'detail_dwell', 'a', seconds=5000)]
    assert http['post']('/api/owner-events', dict(events=batch))[1]['stored'] == 4
    secs = {k: json.loads(v['params'])['seconds'] for k, v in rows(db).items()}
    assert secs == {'d1': 12.0, 'd2': 600.0, 'd3': 1.0, 'd4': 1800.0}


def test_owner_events_bad_rows_dropped_not_whole_batch(db, cfg, http):
    db.ingest(item('a'), cfg)
    batch = [ev('ok', 'seen', 'a'), ev('b1', 'share', 'a'), ev('b2', 'seen', 'nope'), ev('b3', 'dwell', 'a'),
             ev('b4', 'dwell', 'a', seconds='12'), dict(type='seen', item_id='a', session_id='s1'), 'junk',
             dict(ev('b5', 'seen', 'a'), session_id=''), dict(ev('b6', 'seen', 'a'), params=[1])]
    status, out = http['post']('/api/owner-events', dict(events=batch))
    assert status == 200 and out == dict(stored=1, duplicate=0, dropped=8)
    assert set(rows(db)) == {'ok'}


def test_owner_events_batch_limit_and_format(db, cfg, http):
    db.ingest(item('a'), cfg)
    many = [ev('x%d' % k, 'seen', 'a') for k in range(101)]
    assert http['post']('/api/owner-events', dict(events=many))[0] == 400
    assert http['post']('/api/owner-events', dict(events=many[:100])) == (200, dict(stored=100, duplicate=0, dropped=0))
    assert http['post']('/api/owner-events', dict(events='x'))[0] == 400
    assert http['post']('/api/owner-events', None, raw=b'not json')[0] == 400


def test_owner_events_requires_token_and_same_origin(db, cfg, http):
    db.ingest(item('a'), cfg)
    body = dict(events=[ev('t1', 'seen', 'a')])
    assert http['post']('/api/owner-events', body, tok='')[0] == 403
    assert http['post']('/api/owner-events', body, tok='wrong')[0] == 403
    assert http['post']('/api/owner-events', dict(body, token='wrong'), tok='')[0] == 403
    assert http['post']('/api/owner-events', body, headers={'Origin': 'https://evil.example'})[0] == 403
    assert http['post']('/api/owner-events', body, headers={'Host': 'evil.example'})[0] == 403
    assert rows(db) == {}
    # sendBeacon 补发：令牌在请求体里，text/plain，同源 Origin
    origin = http['base']
    status, out = http['post']('/api/owner-events', dict(body, token=http['token']), tok='',
                               headers={'Content-Type': 'text/plain;charset=UTF-8', 'Origin': origin})
    assert status == 200 and out['stored'] == 1


# ---------- 看过即不要、看过未选与放回、页头计数 ----------

def report(http, *events):
    status, out = http['post']('/api/owner-events', dict(events=list(events)))
    assert status == 200 and out['dropped'] == 0
    return out


def set_at(db, eid, at):
    with db.connect() as c:
        c.execute('UPDATE owner_events SET at=?,eff_at=? WHERE event_id=?', (at, at, eid))


def feed_ids(http, session):
    return [c['item_id'] for c in http['get']('/api/triage/feed?session=' + session)['items']]


def test_seen_in_earlier_session_excluded_same_session_kept(db, cfg, http):
    stocked(db, cfg)
    report(http, dict(ev('s-n1', 'seen', 'n1'), session_id='A'), dict(ev('d-n2', 'dwell', 'n2', seconds=3), session_id='A'),
           dict(ev('s-n3', 'seen', 'n3'), session_id='A'))
    db.triage_act('keep', 'n3', 'r-keep-n3')               # 看过且留下：本来就不在新货里
    # 本次打开（A）里看过的保持原位
    assert feed_ids(http, 'A') == ['bad', 'n0', 'n1', 'n2', 'n4']
    # 下次打开（B）：看过没留下的 n1 不再出现；只有停留、没到 seen 的 n2 照常出现；拣选状态不变
    d = http['get']('/api/triage/feed?session=B')
    assert [c['item_id'] for c in d['items']] == ['bad', 'n0', 'n2', 'n4']
    assert d['counts']['seen'] == 1 and d['counts']['new'] == 4      # 01 计数 = 瀑布流能看到的件数
    assert db.triage_state()['counts']['new'] == 5                     # backoffice_counts 不变
    with db.connect() as c:
        assert triage.state(c, 'n1') == 'new'
    # 游标续载同样排除
    a = http['get']('/api/triage/feed?session=B&limit=2')
    b = http['get']('/api/triage/feed?session=B&limit=2&cursor=' + a['next_cursor'])
    assert [c['item_id'] for c in a['items'] + b['items']] == ['bad', 'n0', 'n2', 'n4']


def test_seen_unpicked_list_and_restore(db, cfg, http):
    stocked(db, cfg)
    report(http, dict(ev('s1', 'seen', 'n1'), session_id='A'), dict(ev('s2', 'seen', 'n2'), session_id='A'),
           dict(ev('s4', 'seen', 'n4'), session_id='A'))
    set_at(db, 's1', '2026-11-04T08:00:00+00:00')
    set_at(db, 's2', '2026-11-04T09:00:00+00:00')
    set_at(db, 's4', '2026-11-04T07:00:00+00:00')
    d = http['get']('/api/triage/seen?session=B')
    assert [c['item_id'] for c in d['items']] == ['n2', 'n1', 'n4'] and d['total'] == 3     # 最近看到的在前
    assert all(c['seen_at'] for c in d['items'])
    a = http['get']('/api/triage/seen?session=B&limit=2')
    b = http['get']('/api/triage/seen?session=B&limit=2&cursor=' + a['next_cursor'])
    assert [c['item_id'] for c in a['items'] + b['items']] == ['n2', 'n1', 'n4'] and b['next_cursor'] is None
    # 本次打开（A）看来，这些货还在瀑布流里，「看过未选」为空
    assert http['get']('/api/triage/seen?session=A')['items'] == []
    # 放回：restore 事件（按 event_id 幂等）→ 重新出现在瀑布流
    restore = dict(ev('rs1', 'restore', 'n1'), session_id='B')
    report(http, restore)
    assert report(http, restore)['duplicate'] == 1
    with db.connect() as c:
        assert c.execute("SELECT count(*) FROM owner_events WHERE type='restore'").fetchone()[0] == 1
    assert feed_ids(http, 'B') == ['bad', 'n0', 'n1', 'n3']
    assert [c['item_id'] for c in http['get']('/api/triage/seen?session=B')['items']] == ['n2', 'n4']
    # 放回之后在别的打开里又看过、仍没留下：再次排除
    report(http, dict(ev('s1b', 'seen', 'n1'), session_id='B'))
    set_at(db, 's1b', '2099-01-01T00:00:00+00:00')
    assert 'n1' not in feed_ids(http, 'C')
    # 「看过未选」里直接留下：离开列表
    db.triage_act('keep', 'n2', 'r-keep-n2')
    assert [c['item_id'] for c in http['get']('/api/triage/seen?session=C')['items']] == ['n1', 'n4']


def test_rescue_after_seen_brings_item_back(db, cfg, http):
    db.ingest(item('bad', title='Meissen defekt Tasse'), cfg)
    report(http, dict(ev('sb', 'seen', 'bad'), session_id='A'))
    set_at(db, 'sb', '2026-01-01T00:00:00+00:00')
    db.triage_act('rescue', 'bad', 'r-rescue')
    assert feed_ids(http, 'B') == ['bad']


def test_feed_header_counts(db, cfg, http):
    from datetime import date
    from curio.store import feed_header
    for k in range(5):
        db.ingest(item('h%d' % k), cfg)
    for k in range(4):
        db.triage_act('keep', 'h%d' % k, 'rk%d' % k)
    db.triage_act('undo', None, 'ru')                       # 撤销了 h3 的留下
    with db.connect() as c:
        # 昨天（柏林）留下的一件：计入商品池，不计今日
        c.execute("UPDATE triage_events SET at='2026-11-03T22:30:00+00:00' WHERE item_id='h0' AND action='keep'")   # 柏林 11-03 23:30
        c.execute("UPDATE triage_events SET at='2026-11-03T23:30:00+00:00' WHERE item_id='h1' AND action='keep'")   # 柏林 11-04 00:30
        c.execute("UPDATE triage_events SET at='2026-11-04T12:00:00+00:00' WHERE item_id='h2' AND action='keep'")
        assert feed_header(c, date(2026, 11, 4)) == dict(kept_today=2, pool=3)
    h = http['get']('/api/triage/header')
    # h0–h2 的留下事件已改到别的时间：只有当天真实时间内没有未撤销的留下（h3 已撤销）
    assert h == dict(kept_today=feed_header_today(db), pool=3)
    assert http['get']('/api/triage/feed')['header'] == h
    # 商品池件数与 02 商品池计数同一口径
    assert h['pool'] == http['get']('/api/pool')['counts']['kept']
    db.ingest(item('h9'), cfg)
    db.triage_act('keep', 'h9', 'rk9')                      # 现在留下：计入今日
    h2 = http['get']('/api/triage/header')
    assert h2['kept_today'] == h['kept_today'] + 1 and h2['pool'] == 4 == http['get']('/api/pool')['counts']['kept']


def feed_header_today(db):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from curio.store import feed_header
    with db.connect() as c:
        return feed_header(c, datetime.now(ZoneInfo('Europe/Berlin')).date())['kept_today']


def test_rescued_items_most_recent_first_and_cursor_walk(db, cfg, http):
    for k in range(3):
        db.ingest(item('n%d' % k), cfg)
        seen_at(db, 'n%d' % k, '2026-11-%02dT10:00:00+00:00' % (20 - k))
    for name in ('r1', 'r2', 'r3'):
        db.ingest(item(name, title='Meissen defekt Tasse'), cfg)
        db.triage_act('rescue', name, 'rr-' + name)
    with db.connect() as c:
        for name, ts in (('r1', '2026-11-04T08:00:00+00:00'), ('r2', '2026-11-04T09:00:00+00:00'), ('r3', '2026-11-04T07:00:00+00:00')):
            c.execute('UPDATE triage SET rescued_at=? WHERE item_id=?', (ts, name))
    want = ['r2', 'r1', 'r3', 'n0', 'n1', 'n2']
    assert [c['item_id'] for c in http['get']('/api/triage/feed')['items']] == want
    walked, cur = [], None
    while True:
        d = http['get']('/api/triage/feed?limit=1' + ('&cursor=' + cur if cur else ''))
        walked += [c['item_id'] for c in d['items']]
        cur = d['next_cursor']
        if not cur:
            break
    assert walked == want


def test_late_seen_with_older_client_ts_does_not_rehide_restored(db, cfg, http):
    db.ingest(item('a'), cfg)
    report(http, dict(ev('s0', 'seen', 'a'), session_id='A'))
    set_at(db, 's0', '2026-11-04T10:00:00+00:00')
    report(http, dict(ev('rs', 'restore', 'a'), session_id='B'))
    set_at(db, 'rs', '2026-11-04T12:00:00+00:00')
    # 放回之后才到达（重发 / beacon）、但实际在放回之前看到的 seen：不再把它藏起来
    report(http, dict(ev('s1', 'seen', 'a'), session_id='A', client_ts='2026-11-04T11:00:00.000Z'))
    with db.connect() as c:
        assert c.execute("SELECT eff_at FROM owner_events WHERE event_id='s1'").fetchone()[0] <= '2026-11-04T11:00:00+00:00'
    assert feed_ids(http, 'C') == ['a']


def test_eff_at_rules():
    from curio.store import _eff_at
    at = '2026-11-04T12:00:00+00:00'
    assert _eff_at('2026-11-04T11:00:00.123Z', at) == '2026-11-04T11:00:00+00:00'
    assert _eff_at('2026-11-04T13:00:00+01:00', at) == at          # = 12:00 UTC，不早于 at
    assert _eff_at('2026-11-04T10:00:00+01:00', at) == '2026-11-04T09:00:00+00:00'
    assert _eff_at('2099-01-01T00:00:00Z', at) == at                # 未来时间
    assert _eff_at('garbage', at) == at and _eff_at('2026-11-04T11:00:00', at) == at and _eff_at(None, at) == at


def test_restore_only_for_items_still_new(db, cfg, http):
    for name in ('k', 'r', 'g', 'n'):
        db.ingest(item(name), cfg)
    db.triage_act('keep', 'k', 'r-k')
    db.triage_act('remove', 'r', 'r-r')
    db.mark_gone('g')
    out = http['post']('/api/owner-events', dict(events=[ev('x' + n, 'restore', n) for n in ('k', 'r', 'g', 'n')]))
    assert out == (200, dict(stored=1, duplicate=0, dropped=3))
    assert set(rows(db)) == {'xn'}


def test_owner_events_overflow_and_deep_json(db, cfg, http):
    db.ingest(item('a'), cfg)
    out = http['post']('/api/owner-events', dict(events=[ev('big', 'dwell', 'a', seconds=10 ** 400), ev('ok', 'seen', 'a')]))
    assert out == (200, dict(stored=1, duplicate=0, dropped=1))
    deep = b'{"events":' + b'[' * 100000 + b']' * 100000 + b'}'
    assert http['post']('/api/owner-events', None, raw=deep[:150000])[0] == 400
    nested = ('{"token":"x","events":' + '[' * 20000 + ']' * 20000 + '}').encode()
    assert http['post']('/api/owner-events', None, raw=nested)[0] == 400
