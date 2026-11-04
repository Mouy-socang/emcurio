"""故事 20：拍卖新品池——纯拍卖不进 01 瀑布流 / 封存 pool / 发布入选，单列"拍卖新品"；留下的标拍卖中、不能上 R12；
转为普通货 / 改回拍卖；结束的拍卖由采集判下架；02 / 03 卡片的"竞拍 · … 结束"标签。
全部用临时库，不连外部主机。"""
import json
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

import pytest
import requests

from curio import agreement, curio_monitor, publish, ranking, triage
from curio.config import load
from curio.store import Store, auction_tag

ROOT = Path(__file__).resolve().parents[1]
_real_request = requests.sessions.Session.request
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
AT = NOW.isoformat(timespec='seconds')


@pytest.fixture(autouse=True)
def only_localhost():
    def guard(self, method, url, *a, **kw):
        assert urlsplit(url).hostname == '127.0.0.1', '测试不得连接外部主机'
        return _real_request(self, method, url, *a, **kw)
    with patch('requests.sessions.Session.request', guard):
        yield


@pytest.fixture
def cfg():
    return dict(load(), keywords=['Meissen'])


@pytest.fixture
def db(tmp_path):
    return Store(tmp_path / 'data' / 'curio.db')


def z(hours):
    """NOW 之后 hours 小时（eBay 的 ends 格式：UTC、毫秒、Z）。"""
    return (NOW + timedelta(hours=hours)).strftime('%Y-%m-%dT%H:%M:%S.000Z')


def add(db, cfg, iid, buying='AUCTION', ends='', price=50):
    db.ingest(dict(item_id=iid, platform='ebay', title='Meissen Tasse ' + iid, price=price, currency='EUR', buying=buying,
                   ends=ends, url='https://www.ebay.de/itm/1' + str(abs(hash(iid)) % 10**8),
                   image='https://i.ebayimg.com/x.jpg'), cfg)
    return iid


def ids(rows):
    return [r['item_id'] for r in rows]


def mixed(db, cfg):
    add(db, cfg, 'auc1', 'AUCTION', z(30))
    add(db, cfg, 'auc2', 'AUCTION,BEST_OFFER', z(5))
    add(db, cfg, 'both', 'FIXED_PRICE,AUCTION', z(5))
    add(db, cfg, 'fix1', 'FIXED_PRICE')
    add(db, cfg, 'fix2', 'FIXED_PRICE,BEST_OFFER')


# ---------- 口径：三者互不重叠 ----------

def test_feed_auction_list_and_counts_disjoint(db, cfg):
    mixed(db, cfg)
    feed = db.triage_feed(session='s1')
    auc = db.auction_list(at=AT)
    feed_ids, auc_ids = set(ids(feed['items'])), set(ids(auc['items']))
    assert feed_ids == {'both', 'fix1', 'fix2'}
    assert auc_ids == {'auc1', 'auc2'}
    assert not feed_ids & auc_ids
    assert feed['counts']['new'] == 3 and feed['counts']['auction'] == 2
    assert auc['counts'] == {'auction': 2, 'auction_soon': 1}            # auc2 在 24 小时内结束
    with db.connect() as c:
        assert set(ids(triage.queue(c))) == {'both', 'fix1', 'fix2'}
        assert {r[0] for r in agreement.prediction_rows(c, 'pool')} == {'both', 'fix1', 'fix2'}   # 封存 pool 不含纯拍卖
    row = {r['item_id']: r for r in auc['items']}['auc2']
    assert row['bid'] == '€50' and row['ends'] == z(5) and row['ended'] is False and row['url'].startswith('https://www.ebay.de/')
    assert row['title'] == 'Meissen Tasse auc2' and 'score' in row


def test_seen_unpicked_excludes_auctions(db, cfg):
    mixed(db, cfg)
    events = [dict(event_id='e-' + i, type='seen', item_id=i, client_ts=None, session_id='old', params=None)
              for i in ('auc1', 'fix1')]
    assert db.record_owner_events(events)['stored'] == 2
    seen = db.seen_unpicked(session='new')
    assert ids(seen['items']) == ['fix1'] and seen['total'] == 1


# ---------- 排序与续载 ----------

def test_auction_order_upcoming_then_ended_then_cursor(db, cfg):
    add(db, cfg, 'a_far', ends=z(48))
    add(db, cfg, 'a_near', ends=z(2))
    add(db, cfg, 'a_mid', ends=z(10))
    add(db, cfg, 'a_none', ends='')                          # 无结束时间：未结束组最后
    add(db, cfg, 'e_old', ends=z(-0.8))
    add(db, cfg, 'e_new', ends=z(-0.2))
    d = db.auction_list(at=AT)
    assert ids(d['items']) == ['a_near', 'a_mid', 'a_far', 'a_none', 'e_new', 'e_old']
    assert [r['ended'] for r in d['items']] == [False] * 4 + [True] * 2
    assert d['next_cursor'] is None and d['counts'] == {'auction': 6, 'auction_soon': 2}
    got, cursor = [], None
    while True:
        page = db.auction_list(limit=2, cursor=cursor, at=AT)
        got += ids(page['items'])
        cursor = page['next_cursor']
        if not cursor:
            break
    assert got == ids(d['items'])
    with pytest.raises(ValueError):
        db.auction_list(cursor='bm90LWpzb24', at=AT)


# ---------- 留下：进 02、拍卖中、不能上 R12、不进发布 ----------

def test_kept_auction_in_pool_but_not_rankable(db, cfg, tmp_path):
    add(db, cfg, 'auc', ends=z(30))
    add(db, cfg, 'fix', 'FIXED_PRICE')
    db.triage_act('keep', 'auc', 'r1')
    db.triage_act('keep', 'fix', 'r2')
    rows = {r['item_id']: r for r in ranking.pool(db, tmp_path)['items']}
    assert rows['auc']['auction'] is True and rows['auc']['auction_only'] is True
    assert rows['auc']['rank_reason'] == publish.AUCTION_REASON == '拍卖中的货不能上 R12，拍下后可转为普通货'
    assert any(t.startswith('竞拍 · ') and t.endswith(' 结束') for t in rows['auc']['tags'])
    assert rows['fix']['auction'] is False and rows['fix']['rank_reason'] is None
    with db.connect() as c:
        code = triage.code(c, 'auc')
        assert publish.why_not(c, code) == code + ' ' + publish.AUCTION_REASON
        assert ids(publish.eligible(c)) == ['fix']
        assert {r[0] for r in agreement.prediction_rows(c, 'rank')} == {'fix'}
        with pytest.raises(publish.PublishError, match='拍卖中'):
            publish.set_rank(c, [code])
    assert ranking.detail_rank_reason(db, 'auc') == publish.AUCTION_REASON
    assert db.item_detail('auc')['auction'] is True


def test_auction_already_on_rank_blocks_publish(db, cfg, tmp_path):
    add(db, cfg, 'auc', ends=z(30))
    db.triage_act('keep', 'auc', 'r1')
    with db.connect() as c:
        code = triage.code(c, 'auc')
        c.execute('INSERT INTO publish_rank(pos,code) VALUES(1,?)', (code,))   # 本故事之前就在榜上
        with pytest.raises(publish.PublishError, match='拍卖中的货不能上 R12'):
            publish.check_rank(c)
    row = ranking.rank_state(db, tmp_path)['rows'][0]
    assert row['off'] is True and '拍卖中' in row['reason']
    assert any(t.startswith('竞拍 · ') for t in row['tags'])


# ---------- 转为普通货 / 改回拍卖 ----------

def test_convert_and_revert(db, cfg, tmp_path):
    add(db, cfg, 'new_auc', ends=z(30))
    add(db, cfg, 'kept_auc', ends=z(30))
    db.triage_act('keep', 'kept_auc', 'k1')
    r = db.auction_convert('new_auc', True, 'c1')
    assert r['converted'] is True and r['auction'] is False and r['state'] == 'new' and r['counts']['new'] == 1
    assert db.auction_convert('new_auc', True, 'c1') == r                  # 同一 request_id 重放
    assert db.auction_convert('new_auc', True, 'c2')['converted'] is True  # 重复点击幂等
    with pytest.raises(ValueError, match='重复请求内容不同'):
        db.auction_convert('new_auc', False, 'c1')
    assert ids(db.triage_feed()['items']) == ['new_auc'] and db.auction_list(at=AT)['items'] == []
    db.auction_convert('kept_auc', True, 'c3')
    with db.connect() as c:
        code = triage.code(c, 'kept_auc')
        assert publish.why_not(c, code) is None and ids(publish.eligible(c)) == ['kept_auc']
        publish.set_rank(c, [code])
    row = ranking.pool(db, tmp_path)['items'][0]
    assert row['auction'] is False and row['auction_only'] is True and not any(t.startswith('竞拍') for t in row['tags'])
    # 改回拍卖：恢复纯拍卖口径
    back = db.auction_convert('new_auc', False, 'c4')
    assert back['converted'] is False and back['auction'] is True
    assert db.triage_feed()['items'] == [] and ids(db.auction_list(at=AT)['items']) == ['new_auc']
    with pytest.raises(ValueError, match='不是纯拍卖'):
        add(db, cfg, 'fix', 'FIXED_PRICE,AUCTION')
        db.auction_convert('fix', True, 'c5')
    # 拣选状态与事件不变
    with db.connect() as c:
        assert triage.state(c, 'new_auc') == 'new'
        assert c.execute("SELECT count(*) FROM triage_events WHERE item_id='new_auc' AND actor='human'").fetchone()[0] == 0


def test_list_keep_and_remove_use_triage_actions_without_seen(db, cfg):
    add(db, cfg, 'a1', ends=z(3))
    add(db, cfg, 'a2', ends=z(4))
    r = db.triage_act('keep', 'a1', 'k1')
    assert r['state'] == 'kept' and r['code']
    db.triage_act('remove', 'a2', 'k2')
    assert db.auction_list(at=AT)['items'] == []
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM owner_events').fetchone()[0] == 0


# ---------- 标签 ----------

def test_auction_tag_formats(db, cfg):
    add(db, cfg, 'a', ends='2026-11-05T20:05:00.000Z')
    add(db, cfg, 'e', ends=z(-3))
    add(db, cfg, 'n', ends='')
    add(db, cfg, 'b', 'FIXED_PRICE,AUCTION', z(3))
    with db.connect() as c:
        get = lambda i: c.execute('SELECT * FROM items WHERE item_id=?', (i,)).fetchone()
        assert auction_tag(c, get('a'), NOW) == '竞拍 · 11月5日 21:05 结束'     # 柏林时间（冬令时 +1）
        assert auction_tag(c, get('e'), NOW) == '竞拍 · 已结束'
        assert auction_tag(c, get('n'), NOW) == '竞拍'
        assert auction_tag(c, get('b'), NOW) is None
        triage.set_converted(c, 'a', True)
        assert auction_tag(c, get('a'), NOW) is None


# ---------- 结束的拍卖判下架 ----------

def test_close_ended_auctions(db, cfg):
    add(db, cfg, 'long_ago', ends=z(-3))
    add(db, cfg, 'just_now', ends=z(-0.5))                   # 未满 1 小时：不判
    add(db, cfg, 'conv', ends=z(-2))
    add(db, cfg, 'both', 'FIXED_PRICE,AUCTION', z(-5))       # 一口价兼拍卖：不判
    add(db, cfg, 'future', ends=z(5))
    add(db, cfg, 'none', ends='')
    add(db, cfg, 'kept', ends=z(-2))
    db.triage_act('keep', 'kept', 'k1')
    db.auction_convert('conv', True, 'c1')                   # 转为普通货的不判（交给普通在售检查）
    rows = curio_monitor.close_ended_auctions(db, lambda: NOW.timestamp())
    assert ids(rows) == ['kept', 'long_ago']
    assert {r['item_id']: r['state'] for r in rows}['kept'] == 'kept'
    with db.connect() as c:
        st = dict(c.execute('SELECT item_id,status FROM items').fetchall())
        ev = c.execute("SELECT item_id,outcome,evidence FROM status_check_events ORDER BY item_id").fetchall()
    assert sorted(k for k, v in st.items() if v == 'gone') == ['kept', 'long_ago'] and st['conv'] == 'active'
    assert [tuple(e) for e in ev] == [(i, 'gone', 'auction ended (ends passed)') for i in ('kept', 'long_ago')]
    assert curio_monitor.close_ended_auctions(db, lambda: NOW.timestamp()) == []      # 已下架的不再处理


@pytest.fixture
def root(tmp_path):
    shutil.copytree(ROOT / 'config', tmp_path / 'config')
    (tmp_path / '.env').write_text('EBAY_CLIENT_ID=dummy\nEBAY_CLIENT_SECRET=dummy\n')
    return tmp_path


def test_idle_run_closes_ended_auctions_and_archives_pool_items(root):
    from curio.schedule import KV_LAST_CHECK, KV_LAST_FETCH, KV_LAST_PUBLISHED
    cfg = load(root)
    db = Store(root / 'data' / 'curio.db')
    now = time.time()
    ended = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M:%S.000Z')
    for iid in ('pool_auc', 'new_auc'):
        db.ingest(dict(item_id=iid, platform='ebay', title='Meissen Tasse', price=10, currency='EUR', buying='AUCTION',
                       ends=ended, url='https://www.ebay.de/itm/1', image='https://i.ebayimg.com/x.jpg'), cfg)
    db.triage_act('keep', 'pool_auc', 'k1')
    curio_monitor._ensure_check_events(db)
    with db.connect() as c:     # 池货刚查过，本轮不到期（空闲轮）
        c.execute("INSERT INTO status_check_events(item_id,platform,checked_at,outcome,evidence) VALUES('pool_auc','ebay',?,'active','t')",
                  (datetime.now(timezone.utc).isoformat(timespec='seconds'),))
    db.set(KV_LAST_FETCH, now - 60); db.set('schedule_fetch_interval_min', 60)
    db.set(KV_LAST_CHECK, now - 60); db.set(KV_LAST_PUBLISHED, now - 60)
    fake = lambda amount, currency, root, archive_date=None: {'original_amount': amount, 'status': 'pending_rate'}
    archived = Mock(return_value=True)
    with patch('curio.sync._notify'), patch('curio.archive_price.archive_price', fake), \
         patch('curio.curio_monitor.EbayAPI', return_value=Mock()), patch('curio.curio_monitor._archive_gone', archived):
        result = curio_monitor.run(root, lambda: now)
    assert result['status'] == 'idle' and result['auction_ended'] == 2, result
    assert [call.args[1] for call in archived.call_args_list] == ['pool_auc']     # 只对池里的货做下架即存档
    with db.connect() as c:
        assert dict(c.execute('SELECT item_id,status FROM items').fetchall()) == {'pool_auc': 'gone', 'new_auc': 'gone'}


# ---------- HTTP ----------

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

    def post(path, body, tok=True):
        h = {'Content-Type': 'application/json'}
        if tok:
            h['X-Curio-Token'] = token
        req = urllib.request.Request(base + path, json.dumps(body).encode(), headers=h)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)
    yield dict(base=base, html=html, get=get, post=post)
    httpd.shutdown()


def test_http_auctions_and_convert(db, cfg, http):
    far = (datetime.now(timezone.utc) + timedelta(days=3)).strftime('%Y-%m-%dT%H:%M:%S.000Z')
    add(db, cfg, 'auc', ends=far)
    add(db, cfg, 'fix', 'FIXED_PRICE')
    assert 'id="openAuc"' in http['html'] and 'id="aucView"' in http['html']
    d = http['get']('/api/auctions')
    assert ids(d['items']) == ['auc'] and d['counts'] == {'auction': 1, 'auction_soon': 0}
    assert http['get']('/api/triage/feed?limit=40')['counts']['auction'] == 1
    assert http['post']('/api/auction/convert', dict(item_id='auc', on=True, request_id='x1'), tok=False)[0] == 403
    status, r = http['post']('/api/auction/convert', dict(item_id='auc', on=True, request_id='x1'))
    assert status == 200 and r['converted'] is True
    assert http['get']('/api/auctions')['items'] == []
    assert set(ids(http['get']('/api/triage/feed?limit=40')['items'])) == {'auc', 'fix'}
    status, err = http['post']('/api/auction/convert', dict(item_id='fix', on=True, request_id='x2'))
    assert status == 400 and '不是纯拍卖' in err['error']
    assert http['post']('/api/auction/convert', dict(item_id='auc', on='yes', request_id='x3'))[0] == 400


def test_close_ended_auctions_skips_unchanged_and_keeps_partial_on_error(db, cfg):
    add(db, cfg, 'a1', ends=z(-3))
    add(db, cfg, 'a2', ends=z(-3))
    add(db, cfg, 'a3', ends=z(-3))
    real = db.mark_gone_changed
    calls = []

    def flaky(iid):
        calls.append(iid)
        if iid == 'a1':
            real(iid)
            return False                     # 并发已改：本次没有改动
        if iid == 'a3':
            raise RuntimeError('boom')
        return real(iid)
    done = []
    with patch.object(db, 'mark_gone_changed', flaky), pytest.raises(RuntimeError):
        curio_monitor.close_ended_auctions(db, lambda: NOW.timestamp(), done)
    assert ids(done) == ['a2']                                      # 已改的部分仍交给调用方
    with db.connect() as c:
        assert [r[0] for r in c.execute('SELECT item_id FROM status_check_events')] == ['a2']


def test_fetch_round_closes_ended_auctions_and_archives_with_round_api(root):
    cfg = load(root)
    db = Store(root / 'data' / 'curio.db')
    now = time.time()
    ended = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M:%S.000Z')
    for iid in ('pool_auc', 'new_auc'):
        db.ingest(dict(item_id=iid, platform='ebay', title='Meissen Tasse', price=10, currency='EUR', buying='AUCTION',
                       ends=ended, url='https://www.ebay.de/itm/1', image='https://i.ebayimg.com/x.jpg'), cfg)
    db.triage_act('keep', 'pool_auc', 'k1')
    api = Mock()
    fake = lambda amount, currency, root, archive_date=None: {'original_amount': amount, 'status': 'pending_rate'}
    archived = Mock(return_value=True)
    with patch('curio.sync._notify'), patch('curio.archive_price.archive_price', fake), \
         patch('curio.curio_monitor.EbayAPI', return_value=api), \
         patch('curio.curio_monitor.collect_ebay', Mock(return_value=0)), \
         patch('curio.curio_monitor.collect_ka', side_effect=lambda store, config, budget=None: store.set('ka_status', 'ok') or 0), \
         patch('curio.curio_monitor.check_existing', Mock(return_value={'attempted': 0, 'errors': 0})), \
         patch('curio.curio_monitor.check_pool', Mock(return_value={'attempted': 0, 'errors': 0})), \
         patch('curio.curio_monitor.archive_pool', Mock(return_value={})), \
         patch('curio.curio_monitor._archive_gone', archived):
        result = curio_monitor.run(root, lambda: now)
    assert result.get('status') != 'idle' and result['fetch_ran'] and result['auction_ended'] == 2, result
    assert [(call.args[1], call.args[3]) for call in archived.call_args_list] == [('pool_auc', api)]


def test_unconvert_reports_on_rank_and_why_not_order(db, cfg):
    add(db, cfg, 'auc', ends=z(30))
    db.triage_act('keep', 'auc', 'k1')
    db.auction_convert('auc', True, 'c1')
    with db.connect() as c:
        code = triage.code(c, 'auc')
        publish.set_rank(c, [code])
    r = db.auction_convert('auc', False, 'c2')
    assert r['on_rank'] is True and r['auction'] is True
    db.mark_gone('auc')                                             # 已下架的拍卖：说已下架，不说拍卖中
    with db.connect() as c:
        assert publish.why_not(c, code) == code + ' 原平台已下架'


def test_auction_cursor_rejects_non_finite(db, cfg):
    add(db, cfg, 'a', ends=z(3))
    with db.connect() as c:
        for bad in (float('nan'), float('inf')):
            with pytest.raises(ValueError, match='游标无效'):
                triage.auctions(c, cursor=[0, bad, 'a'])


def test_http_auctions_bad_limit(db, cfg, http):
    try:
        urllib.request.urlopen(http['base'] + '/api/auctions?limit=abc')
        raise AssertionError('应返回 400')
    except urllib.error.HTTPError as e:
        assert e.code == 400 and json.load(e) == {'error': '参数无效'}
