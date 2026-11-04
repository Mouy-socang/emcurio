"""故事 7（CAP-6）：后台一页拣选的服务端接口——新页与旧页路由、队列与计数、留下 / 去掉 / 捞回 / 撤销（幂等）、
货详情（图片、替代文字、点评、原文 lang）、复查（单件在售检查，全部 mock，无真实网络）、令牌与本机绑定。
界面手势与朗读见故事 Verification 的人工检查。"""
import json
import re
import threading
import urllib.error
import urllib.request
from unittest.mock import Mock, patch

import pytest

from curio import triage
from curio.config import load
from curio.store import Store, guess_lang, price_label


@pytest.fixture
def cfg():
    return dict(load(), keywords=['Meissen'])


@pytest.fixture
def db(tmp_path):
    return Store(tmp_path / 'curio.db')


def item(i='one', price=40, title='Meissen rote Rose Tasse', platform='ebay', **kw):
    return dict(dict(item_id=i, platform=platform, title=title, price=price, currency='EUR', buying='FIXED_PRICE',
                     url='https://www.ebay.de/itm/example', image='https://i.ebayimg.com/x.jpg'), **kw)


def state(db, iid):
    with db.connect() as c:
        return triage.state(c, iid)


def events(db):
    with db.connect() as c:
        return c.execute("SELECT count(*) FROM triage_events WHERE actor='human'").fetchone()[0]


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

    def post(path, body, tok=None, headers=None):
        h = {'Content-Type': 'application/json', 'X-Curio-Token': token if tok is None else tok, **(headers or {})}
        if tok == '':
            del h['X-Curio-Token']
        req = urllib.request.Request(base + path, json.dumps(body).encode(), headers=h)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    yield dict(base=base, html=html, get=get, post=post)
    httpd.shutdown()
    httpd.server_close()


def test_new_page_at_root_and_old_page_at_old(http):
    html = http['html']
    assert '<nav class="tabs"' in html and '/triage.js' in html and '__TOKEN__' not in html
    for name in ('新货', '商品池', 'R12', '匹配', '待审核', '客人'):
        assert '<b>' + name + '</b>' in html
    assert html.count('暂不可用') == 0 and 'href="/#rules"' in html and 'href="/#guests"' in html and 'aria-current="page"' in html
    assert 'maximum-scale' not in html and 'user-scalable' not in html
    old = urllib.request.urlopen(http['base'] + '/old').read().decode()
    assert 'Curio · 找货审核' in old and "const token='" in old
    js = urllib.request.urlopen(http['base'] + '/triage.js')
    assert js.headers['Content-Type'].startswith('application/javascript')
    body = js.read().decode()
    assert 'X-Curio-Token' in body and 'request_id' in body and 'http://' not in body.replace("'http", '')


def test_page_uses_only_design_colors(http):
    html = http['html']
    allowed = {'#F6F4EF', '#ECE9E2', '#1F1D1A', '#625E56', '#D9D5CC', '#B9B4AA'}
    assert set(c.upper() for c in re.findall(r'#[0-9A-Fa-f]{6}\b', html)) <= allowed
    assert 'box-shadow' not in html and not re.search(r'border-radius:\s*[1-9]', html)


def test_queue_order_counts_and_card(db, cfg, http):
    db.ingest(item('a', title='Meissen Tasse'), cfg)
    db.ingest(item('b', title='Meissen rote Rose Tasse Teller'), cfg)
    db.ingest(item('bad', title='Meissen defekt Tasse'), cfg)
    db.ingest(item('usd', price=99.5, title='Meissen Teller', currency='USD'), cfg)
    d = http['get']('/api/triage')
    assert d['counts'] == {'new': 3, 'auto_removed': 1, 'auction': 0, 'auction_soon': 0, 'auction_pool': 0, 'auction_rank': 0}
    with db.connect() as c:
        expected = [r['item_id'] for r in triage.queue(c)]
    assert [x['item_id'] for x in d['queue']] == expected
    first = d['queue'][0]
    assert {'name', 'price', 'platform', 'score', 'score_reason', 'image', 'tags', 'code'} <= set(first)
    assert first['platform'] == 'eBay' and first['image'] == 'https://i.ebayimg.com/x.jpg'
    usd = next(x for x in d['queue'] if x['item_id'] == 'usd')
    assert usd['price'] == '99.50 USD'
    assert next(x for x in d['queue'] if x['item_id'] == 'a')['price'] == '€40'


def test_keep_remove_replay_and_undo_chain(db, cfg, http):
    db.ingest(item('a'), cfg)
    db.ingest(item('b', title='Meissen Teller'), cfg)
    post = http['post']
    s, r = post('/api/triage/action', dict(action='keep', item_id='a', request_id='r1'))
    assert s == 200 and r['state'] == 'kept' and re.fullmatch(r'CO-\d{4}', r['code']) and r['remaining'] == 1
    code = r['code']
    # 重放：同一结果，不产生第二个事件
    n = events(db)
    s, again = post('/api/triage/action', dict(action='keep', item_id='a', request_id='r1'))
    assert s == 200 and again == r and events(db) == n
    # 同一 request_id 内容不同 → 400
    s, err = post('/api/triage/action', dict(action='remove', item_id='a', request_id='r1'))
    assert s == 400 and err['error'] == '重复请求内容不同'
    s, r = post('/api/triage/action', dict(action='remove', item_id='b', request_id='r2'))
    assert s == 200 and r['state'] == 'removed' and r['remaining'] == 0
    # 连续撤销：B、A 依次回到 new，A 的货号保留不复用
    s, u1 = post('/api/triage/action', dict(action='undo', request_id='u1'))
    assert s == 200 and u1['item_id'] == 'b' and u1['state'] == 'new' and u1['card']['name']
    s, u2 = post('/api/triage/action', dict(action='undo', request_id='u2'))
    assert s == 200 and u2['item_id'] == 'a' and u2['state'] == 'new' and u2['code'] == code
    assert state(db, 'a') == state(db, 'b') == 'new'
    # 撤销重放不再撤一步
    n = events(db)
    assert post('/api/triage/action', dict(action='undo', request_id='u2'))[1] == u2 and events(db) == n
    s, err = post('/api/triage/action', dict(action='undo', request_id='u3'))
    assert s == 400 and err['error'] == '没有可撤销的操作'
    # 再次留下 A：仍是原货号
    assert post('/api/triage/action', dict(action='keep', item_id='a', request_id='r3'))[1]['code'] == code
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM item_codes').fetchone()[0] == 1


def test_failed_action_is_not_recorded_and_server_error_is_500(db, cfg, http, monkeypatch):
    db.ingest(item('a'), cfg)
    post = http['post']
    s, err = post('/api/triage/action', dict(action='rescue', item_id='a', request_id='x1'))
    assert s == 400 and '不能捞回' in err['error']
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM triage_requests').fetchone()[0] == 0

    def boom(*a, **k):
        raise RuntimeError('db down')
    monkeypatch.setattr(Store, 'triage_act', boom)
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(urllib.request.Request(http['base'] + '/api/triage/action',
            json.dumps(dict(action='keep', item_id='a', request_id='x2')).encode(),
            headers={'X-Curio-Token': re.search("const token='([^']+)'", http['html'])[1]}))
    assert e.value.code == 500
    monkeypatch.undo()
    assert state(db, 'a') == 'new'
    # 重试（同一 request_id）成功
    assert post('/api/triage/action', dict(action='keep', item_id='a', request_id='x2'))[1]['state'] == 'kept'


def test_auto_removed_list_and_rescue_to_front(db, cfg, http):
    db.ingest(item('a'), cfg)
    db.ingest(item('b', title='Meissen Teller'), cfg)
    db.ingest(item('bad', title='Meissen defekt Tasse'), cfg)
    lst = http['get']('/api/triage/auto-removed')
    assert lst['total'] == 1
    row = lst['items'][0]
    assert row['item_id'] == 'bad' and row['reason'] and row['removed_at'] and row['price'] == '€40'
    s, r = http['post']('/api/triage/action', dict(action='rescue', item_id='bad', request_id='res1'))
    assert s == 200 and r['state'] == 'new' and r['auto_removed'] == 0 and r['remaining'] == 3
    assert http['get']('/api/triage')['queue'][0]['item_id'] == 'bad'
    assert http['get']('/api/triage/auto-removed') == {'items': [], 'total': 0}
    with db.connect() as c:
        ev = c.execute("SELECT * FROM triage_events WHERE item_id='bad' AND action='rescue'").fetchone()
        assert ev['actor'] == 'human' and ev['source'] == 'backoffice'


def test_detail_comment_and_alt_texts(db, cfg, http, tmp_path):
    db.ingest(item('a', description='Keine Chips, keine Risse, nicht restauriert.'), cfg)
    d = http['get']('/api/triage/item?id=a')
    assert d['description'] == 'Keine Chips, keine Risse, nicht restauriert.' and d['description_lang'] == 'de'
    assert d['url'] == 'https://www.ebay.de/itm/example' and d['agent_reason'] and d['code'] is None
    assert d['images'] == [dict(src='https://i.ebayimg.com/x.jpg', alt=d['name'] + ' · 第 1 张 / 共 1 张',
                                default_alt=d['name'] + ' · 第 1 张 / 共 1 张')]
    assert d['comment'] == '' and d['state'] == 'new'
    post = http['post']
    assert post('/api/triage/comment', dict(item_id='a', text='钴蓝沉稳\n出水口利落'))[0] == 200
    assert post('/api/triage/alt', dict(item_id='a', index=0, text='口沿特写'))[0] == 200
    d = http['get']('/api/triage/item?id=a')
    assert d['comment'] == '钴蓝沉稳\n出水口利落' and d['images'][0]['alt'] == '口沿特写'
    # 空替代文字恢复默认；空点评删除
    post('/api/triage/alt', dict(item_id='a', index=0, text=' '))
    post('/api/triage/comment', dict(item_id='a', text=''))
    d = http['get']('/api/triage/item?id=a')
    assert d['images'][0]['alt'] == d['images'][0]['default_alt'] and d['comment'] == ''
    assert post('/api/triage/alt', dict(item_id='a', index=-1, text='x'))[0] == 400
    assert post('/api/triage/alt', dict(item_id='a', index=1, text='x'))[0] == 400   # 只有 1 张图
    assert post('/api/triage/alt', dict(item_id=5, index=0, text='x'))[0] == 400
    assert post('/api/triage/comment', dict(item_id=['a'], text='x'))[0] == 400
    assert post('/api/triage/comment', dict(item_id='nope', text='x'))[0] == 400
    # 留下后详情有货号
    post('/api/triage/action', dict(action='keep', item_id='a', request_id='k'))
    assert http['get']('/api/triage/item?id=a')['code'].startswith('CO-')
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(http['base'] + '/api/triage/item?id=nope')
    assert e.value.code == 400


def test_detail_prefers_downloaded_snapshot_images(db, cfg, tmp_path):
    from curio.store import item_detail
    db.ingest(item('a'), cfg)
    sha = 'a' * 64
    (tmp_path / 'data/porcelain/images').mkdir(parents=True)
    (tmp_path / 'data/porcelain/images' / (sha + '.jpg')).write_bytes(b'x')
    with db.connect() as c:
        c.execute('CREATE TABLE IF NOT EXISTS product_snapshots(id INTEGER PRIMARY KEY,item_id TEXT,fingerprint TEXT UNIQUE,created_at TEXT,data TEXT)')
        c.execute("INSERT INTO product_snapshots(item_id,fingerprint,created_at,data) VALUES('a','f','t',?)",
                  (json.dumps(dict(images=[dict(file='images/' + sha + '.jpg'), dict(file='images/' + 'b' * 64 + '.jpg')])),))
        d = item_detail(c, 'a', tmp_path)
    assert [i['src'] for i in d['images']] == ['/archive/images/' + sha + '.jpg']


def test_overpaint_and_kleinanzeigen_tags(db, cfg):
    from curio.store import card
    db.ingest(item('p', title='Meissen Teller Hausmalerei Blumen'), cfg)
    db.ingest(dict(item('ka:1', title='Meissen Tasse', platform='kleinanzeigen'), url='https://www.kleinanzeigen.de/s-anzeige/x/1'), cfg)
    with db.connect() as c:
        p = card(c, c.execute("SELECT * FROM items WHERE item_id='p'").fetchone())
        k = card(c, c.execute("SELECT * FROM items WHERE item_id='ka:1'").fetchone())
    assert '外画' in p['tags'] and p['name'] != 'Meissen Teller Hausmalerei Blumen'
    assert k['tags'] == ['仅后台 · Kleinanzeigen'] and k['platform'] == 'Kleinanzeigen'


def test_helpers():
    assert price_label(185, 'EUR') == '€185' and price_label(12.5, 'eur') == '€12.50'
    assert price_label(30, 'GBP') == '30 GBP' and price_label(None, 'EUR') == '价格待询'
    assert guess_lang('Keine Risse') == 'de' and guess_lang('No chips, no cracks') == 'en'
    assert guess_lang('') is None and guess_lang('蓝洋葱') is None and guess_lang('Porcellana Capodimonte') is None


def test_recheck_endpoint_uses_single_item_check(db, cfg, http):
    db.ingest(item('a'), cfg)
    with patch('curio.curio_monitor.recheck_item', return_value=dict(outcome='gone', evidence='x')) as m:
        s, r = http['post']('/api/triage/recheck', dict(item_id='a'))
    assert s == 200 and r['outcome'] == 'gone' and m.call_args[0][1] == 'a'


def test_recheck_item_ebay_gone_marks_and_records(db, cfg):
    from curio.curio_monitor import recheck_item
    from curio.schedule import Budget
    db.ingest(item('a'), cfg)
    api = Mock()
    api.detail.return_value = None
    budget = Budget(db, cfg['api_budget'])
    r = recheck_item(db, 'a', cfg, api=api, budget=budget)
    assert r['outcome'] == 'gone' and api.purpose == 'check'
    with db.connect() as c:
        assert c.execute("SELECT status FROM items WHERE item_id='a'").fetchone()[0] == 'gone'
        ev = c.execute('SELECT * FROM status_check_events').fetchall()
        assert len(ev) == 1 and ev[0]['outcome'] == 'gone' and ev[0]['evidence'] == 'API HTTP 404/410'


def test_recheck_item_active_refreshes_price(db, cfg):
    from curio.curio_monitor import recheck_item
    db.ingest(item('a'), cfg)
    api = Mock()
    api.detail.return_value = dict(itemId='a', title='Meissen rote Rose Tasse', price={'value': '30', 'currency': 'EUR'}, buyingOptions=['FIXED_PRICE'])
    assert recheck_item(db, 'a', cfg, api=api, budget=Mock(available=Mock(return_value=True)))['outcome'] == 'active'
    with db.connect() as c:
        assert c.execute("SELECT listed_price,status FROM items WHERE item_id='a'").fetchone()[:] == (30, 'active')


def test_recheck_item_budget_exhausted_and_failure(db, cfg):
    from curio.curio_monitor import recheck_item
    from curio.schedule import Budget
    db.ingest(item('a'), cfg)
    api = Mock()
    budget = Budget(db, dict(cfg['api_budget'], ebay=0))
    assert recheck_item(db, 'a', cfg, api=api, budget=budget)['outcome'] == 'budget'
    api.detail.assert_not_called()
    api.detail.side_effect = RuntimeError('net')
    assert recheck_item(db, 'a', cfg, api=api, budget=Budget(db, cfg['api_budget']))['outcome'] == 'error'
    with db.connect() as c:
        assert c.execute("SELECT status FROM items WHERE item_id='a'").fetchone()[0] == 'active'


def test_recheck_item_counts_against_check_budget(db, cfg):
    """真实 EbayAPI 路径：请求计入当日 check 预算（session mock，无网络）。"""
    from curio.curio_monitor import recheck_item, EbayAPI
    from curio.schedule import Budget
    db.ingest(item('a'), cfg)
    budget = Budget(db, cfg['api_budget'])
    api = EbayAPI.__new__(EbayAPI)
    api.budget, api.purpose, api.token, api._cached, api.cache_path = budget, 'fetch', 'tok', False, None
    api.session = Mock()
    api.session.request.return_value = Mock(status_code=404)
    assert recheck_item(db, 'a', cfg, api=api, budget=budget)['outcome'] == 'gone'
    assert budget.snapshot()['ebay']['check'] == 1 and budget.snapshot()['ebay']['fetch'] == 0


def test_recheck_kleinanzeigen_paused(db, cfg):
    from curio.curio_monitor import recheck_item
    import time
    db.ingest(dict(item('ka:1', platform='kleinanzeigen'), url='https://www.kleinanzeigen.de/s-anzeige/x/1'), cfg)
    db.set('ka_pause_until', time.time() + 100)
    with patch('curio.curio_monitor.ka_curl') as curl:
        assert recheck_item(db, 'ka:1', cfg)['outcome'] == 'paused'
        curl.assert_not_called()


def test_recheck_etsy(db, cfg):
    from curio.curio_monitor import recheck_item
    db.ingest(dict(item('etsy:9', platform='etsy'), url='https://www.etsy.com/listing/9'), cfg)
    with patch('curio.etsy.judge_listings', return_value=({'etsy:9': ('gone', 'Etsy state=sold_out; sale unknown', {})}, None)):
        assert recheck_item(db, 'etsy:9', cfg, etsy=Mock())['outcome'] == 'gone'
    with db.connect() as c:
        assert c.execute("SELECT status FROM items WHERE item_id='etsy:9'").fetchone()[0] == 'gone'


def test_token_and_origin_required(db, cfg, http):
    db.ingest(item('a'), cfg)
    post = http['post']
    body = dict(action='keep', item_id='a', request_id='t1')
    for path in ('/api/triage/action', '/api/triage/comment', '/api/triage/alt', '/api/triage/recheck'):
        assert post(path, body, tok='')[0] == 403
        assert post(path, body, tok='wrong')[0] == 403
    assert post('/api/triage/action', body, headers={'Origin': 'https://evil.example'})[0] == 403
    host = http['base'].split('//')[1]
    assert post('/api/triage/action', body, headers={'Origin': 'http://' + host})[0] == 200
    # 伪造的 X-Forwarded-Host（非 *.ts.net）不能让外来 Origin 通过
    body['request_id'] = 't2'
    for fwd in ('evil.example', 'evil.example, mac.tail1.ts.net', 'mac.tail1.ts.net.evil.example'):
        assert post('/api/triage/action', body, headers={'Origin': 'https://evil.example', 'X-Forwarded-Host': fwd})[0] == 403
    assert state(db, 'a') == 'kept'


def test_host_allowlist_blocks_dns_rebinding(http):
    from curio.review import host_allowed
    for bad in ('evil.example', 'evil.example:8788', '127.0.0.1.evil.example', '.ts.net', '', None, '[::1]:80'):
        assert not host_allowed(bad)
    for good in ('127.0.0.1:8788', 'localhost:8788', 'localhost', 'mac.tail1.ts.net', 'Mac.Tail1.TS.net:443'):
        assert host_allowed(good)
    for path in ('/', '/api/triage', '/triage.js', '/old'):
        req = urllib.request.Request(http['base'] + path, headers={'Host': 'evil.example:8788'})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        assert e.value.code == 403 and 'const token' not in e.value.read().decode()
    req = urllib.request.Request(http['base'] + '/', headers={'Host': 'mac.tail1.ts.net'})
    with urllib.request.urlopen(req) as r:
        assert r.headers['Cache-Control'] == 'no-store'
    status, _ = http['post']('/api/triage/action', dict(action='undo', request_id='h1'), headers={'Host': 'evil.example'})
    assert status == 403


def test_get_errors_return_json_500(db, http, monkeypatch):
    def boom(*a, **k):
        raise TypeError('x')
    monkeypatch.setattr(Store, 'triage_state', boom)
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(http['base'] + '/api/triage')
    assert e.value.code == 500 and json.load(e.value)['error'] == '读取失败，请重试'
    assert urllib.request.urlopen(http['base'] + '/triage.js').headers['Cache-Control'] == 'no-store'


def test_server_refuses_non_local_host(db):
    from curio.review import server
    with pytest.raises(ValueError):
        server(db, host='0.0.0.0', port=0)


def test_recheck_unsupported_platform_is_unknown(db, cfg):
    from curio.curio_monitor import recheck_item
    with db.connect() as c:
        c.execute("INSERT INTO items(item_id,platform,title,status,url) VALUES('x:1','lottissimo','Meissen','active','https://example.com/1')")
    with patch('curio.curio_monitor.ka_curl') as curl:
        assert recheck_item(db, 'x:1', cfg)['outcome'] == 'unknown'
        curl.assert_not_called()
    with db.connect() as c:
        assert c.execute("SELECT status FROM items WHERE item_id='x:1'").fetchone()[0] == 'active'


def _ka(db, cfg):
    db.ingest(dict(item('ka:1', platform='kleinanzeigen'), url='https://www.kleinanzeigen.de/s-anzeige/x/1'), cfg)


def test_recheck_kleinanzeigen_active_refreshes_price(db, cfg):
    from curio.curio_monitor import recheck_item
    from curio.schedule import Budget
    _ka(db, cfg)
    budget = Budget(db, cfg['api_budget'])
    page = '<h2 id="viewad-price" class="x">120 €</h2>'
    with patch('curio.curio_monitor.ka_curl', return_value=(200, page, 'https://www.kleinanzeigen.de/s-anzeige/x/1', None)):
        assert recheck_item(db, 'ka:1', cfg, budget=budget)['outcome'] == 'active'
    assert budget.snapshot()['kleinanzeigen']['check'] == 1
    with db.connect() as c:
        assert c.execute("SELECT listed_price FROM items WHERE item_id='ka:1'").fetchone()[0] == 120
        assert c.execute('SELECT count(*) FROM status_check_events').fetchone()[0] == 1


def test_recheck_kleinanzeigen_gone_and_disabled(db, cfg):
    from curio.curio_monitor import recheck_item
    _ka(db, cfg)
    with patch('curio.curio_monitor.ka_curl') as curl:
        assert recheck_item(db, 'ka:1', dict(cfg, kleinanzeigen_enabled=False))['outcome'] == 'paused'
        curl.assert_not_called()
    with patch('curio.curio_monitor.ka_curl', return_value=(404, '', 'https://www.kleinanzeigen.de/s-anzeige/x/1', None)):
        assert recheck_item(db, 'ka:1', cfg)['outcome'] == 'gone'
    with db.connect() as c:
        assert c.execute("SELECT status FROM items WHERE item_id='ka:1'").fetchone()[0] == 'gone'


def test_recheck_etsy_real_api_charges_check_budget(db, cfg, tmp_path):
    from curio.curio_monitor import recheck_item
    from curio.etsy import EtsyAPI
    from curio.schedule import Budget
    db.ingest(dict(item('etsy:9', platform='etsy'), url='https://www.etsy.com/listing/9'), cfg)
    budget = Budget(db, cfg['api_budget'])
    resp = Mock(status_code=200)
    resp.json.return_value = {'results': [{'listing_id': 9, 'state': 'sold_out'}]}
    session = Mock()
    session.get.return_value = resp
    api = EtsyAPI('KEY', 'SECRET', budget, session)
    assert recheck_item(db, 'etsy:9', cfg, etsy=api, budget=budget)['outcome'] == 'gone'
    snap = budget.snapshot()['etsy']
    assert snap['check'] == 1 and snap['fetch'] == 0
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM status_check_events').fetchone()[0] == 1
        assert c.execute("SELECT status FROM items WHERE item_id='etsy:9'").fetchone()[0] == 'gone'
    # 没有 Etsy 凭据：不发请求，暂时无法确认
    (tmp_path / 'noenv').mkdir()
    assert recheck_item(db, 'etsy:9', cfg, root=tmp_path / 'noenv')['outcome'] == 'unknown'


def test_detail_survives_null_or_bad_snapshot_data(db, cfg, tmp_path):
    from curio.store import item_detail
    db.ingest(item('a'), cfg)
    with db.connect() as c:
        c.execute('CREATE TABLE IF NOT EXISTS product_snapshots(id INTEGER PRIMARY KEY,item_id TEXT,fingerprint TEXT UNIQUE,created_at TEXT,data TEXT)')
        c.execute("INSERT INTO product_snapshots(item_id,fingerprint,created_at,data) VALUES('a','f1','t',NULL)")
        c.execute("INSERT INTO product_snapshots(item_id,fingerprint,created_at,data) VALUES('a','f2','t',?)",
                  (json.dumps(dict(images=['images/x.jpg', None, 3, dict(file=None)])),))
        c.execute("INSERT INTO product_snapshots(item_id,fingerprint,created_at,data) VALUES('a','f3','t','[1,2]')")
        d = item_detail(c, 'a', tmp_path)
    assert [i['src'] for i in d['images']] == ['https://i.ebayimg.com/x.jpg']


def test_alt_text_follows_image_when_a_file_goes_missing(db, cfg, tmp_path):
    from curio.store import item_detail
    db.ingest(item('a'), cfg)
    folder = tmp_path / 'data/porcelain/images'
    folder.mkdir(parents=True)
    names = [ch * 64 + '.jpg' for ch in 'abc']
    for n in names:
        (folder / n).write_bytes(b'x')
    with db.connect() as c:
        c.execute('CREATE TABLE IF NOT EXISTS product_snapshots(id INTEGER PRIMARY KEY,item_id TEXT,fingerprint TEXT UNIQUE,created_at TEXT,data TEXT)')
        c.execute("INSERT INTO product_snapshots(item_id,fingerprint,created_at,data) VALUES('a','f','t',?)",
                  (json.dumps(dict(images=[dict(file='images/' + n) for n in names])),))
    db.save_alt('a', 2, '底款特写', root=tmp_path)
    (folder / names[0]).unlink()   # 第 1 张图丢失：其余图前移一位
    with db.connect() as c:
        d = item_detail(c, 'a', tmp_path)
    assert [i['src'].rsplit('/', 1)[-1] for i in d['images']] == names[1:]
    assert d['images'][1]['alt'] == '底款特写' and d['images'][0]['alt'] == d['images'][0]['default_alt']


def test_auto_removed_list_validates_paging_and_skips_missing_items(db, cfg):
    db.ingest(item('bad', title='Meissen defekt Tasse'), cfg)
    for args in ((0, 0), (201, 0), (10, -1), ('5', 0)):
        with pytest.raises(ValueError):
            db.auto_removed_list(*args)
    with db.connect() as c:
        c.execute("INSERT INTO triage(item_id,state,updated_at) VALUES('ghost','auto_removed','2026-11-01T00:00:00+00:00')")
    assert [r['item_id'] for r in db.auto_removed_list()['items']] == ['bad']
