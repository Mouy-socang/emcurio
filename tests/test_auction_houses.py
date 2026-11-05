"""故事 32：拍卖行名单与每日采集（Wendl、Mehlis、Lempertz）。样本为裁剪后的真实页面（tests/fixtures/auction_houses/）；
全部用假会话与临时库，不连外部主机。"""
import json
import shutil
from datetime import datetime, date
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

import pytest

from curio import auction_houses as ah, curio_monitor, publish, triage, vision
from curio.config import load, auction_houses as validate_houses
from curio.schedule import BERLIN, Budget
from curio.store import Store

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / 'tests/fixtures/auction_houses'
NOON = datetime(2026, 11, 5, 12, tzinfo=BERLIN).timestamp()
NEXT_DAY = datetime(2026, 11, 6, 12, tzinfo=BERLIN).timestamp()
OPEN = 'User-agent: *\nAllow: /\n'


def fx(path):
    return (FIX / path).read_text(encoding='utf-8')


def pages():
    """URL → (状态码, 文本)。robots.txt 默认全部放行。"""
    m, w, l = 'https://www.mehlis.eu', 'https://auktionen.auktionshauswendl.de', 'https://www.lempertz.com'
    return {
        'https://www.auktionshauswendl.de/robots.txt': (200, OPEN),
        w + '/robots.txt': (200, OPEN),
        'https://www.auktionshauswendl.de/tcms/auktionen/termine': (200, fx('wendl/termine.html')),
        w + '/de/listpage/116/138/0?bidResultStatus=Unknown': (200, fx('wendl/porzellan_p0.html')),
        w + '/de/listpage/116/138/1?bidResultStatus=Unknown': (200, fx('wendl/porzellan_p1.html')),
        m + '/robots.txt': (404, ''),
        m + '/de/catalogs/': (200, fx('mehlis/catalogs.html')),
        m + '/de/catalogs/timeline/': (200, fx('mehlis/timeline.html')),
        m + '/de/catalogs/12426/9/?page=1': (200, fx('mehlis/porzellan_p1.html')),
        m + '/de/catalogs/12426/9/?page=2': (200, fx('mehlis/porzellan_p2.html')),
        l + '/robots.txt': (200, 'User-agent: *\nDisallow: /typo3/\n'),
        l + '/de/auktionskataloge.html': (200, fx('lempertz/auktionskataloge.html')),
        ('POST', l + '/de/?endpoint=catalogue', '754'): (200, fx('lempertz/catalogue_754.json')),
        ('POST', l + '/de/?endpoint=catalogue', '726'): (200, json.dumps({'lots': [], 'categories': {}})),
        ('POST', l + '/de/?endpoint=catalogue', '739'): (200, json.dumps({'lots': [], 'categories': {}})),
    }


class FakeSession:
    def __init__(self, routes=None):
        self.routes = pages() if routes is None else routes
        self.calls = []
        self.clock, self.stamps = None, []      # 设了 clock 时记下每次请求的（主机, 时间）

    def request(self, method, url, headers=None, timeout=None, allow_redirects=True, json=None):
        assert urlsplit(url).scheme == 'https'
        self.calls.append((method, url, dict(headers or {}), json))
        key = ('POST', url, (json or {}).get('catalogue_uid')) if method == 'POST' else url
        hit = self.routes.get(key, (404, 'not found'))
        code, text = hit[0], hit[1]
        if self.clock is not None:
            self.stamps.append((urlsplit(url).hostname, self.clock.t))
        return Mock(status_code=code, text=text, headers={'location': hit[2]} if len(hit) > 2 else {})

    def hosts(self):
        return [urlsplit(u).hostname for _, u, _, _ in self.calls]


class Clock:
    """假的 monotonic / sleep：记下每次等待，检查请求间隔。"""
    def __init__(self):
        self.t = 1000.0
        self.slept = []

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


@pytest.fixture
def cfg():
    return dict(load(), keywords=['Meissen'])


@pytest.fixture
def db(tmp_path):
    return Store(tmp_path / 'data' / 'curio.db')


def run(db, cfg, session, clock=NOON):
    c = Clock()
    session.clock = c
    return ah.run_due(db, cfg, lambda: clock, session, c.sleep, c.monotonic), c


def status(db):
    return json.loads(db.get('ah_status'))


# ---------- 纯解析 ----------

def test_wendl_parse():
    sales = ah.wendl_sales(fx('wendl/termine.html'))
    assert [(s['sale'], s['start'].isoformat(), s['end'].isoformat()) for s in sales] == [
        (116, '2026-10-28', '2026-10-31'), (117, '2027-02-24', '2027-02-27'), (118, '2027-06-16', '2027-06-19')]
    lots, last, cards = ah.wendl_lots(fx('wendl/porzellan_p0.html'))
    assert last == 1 and cards == 3
    assert [l['lot'] for l in lots] == ['1300', '1320']        # 已成交（带隐藏重复块）的 1301 不取
    assert lots[0] == dict(id='86302', lot='1300', title='Barocker russischer Teller. St. Petersburg',
                           description='Suppenteller mit unterglasurblauem Girlanden- und Schleifendekor', limit=300.0,
                           url='https://auktionen.auktionshauswendl.de/de/lot/lot-details/86302/barocker-russischer-teller',
                           image='https://cdn.aeman.nl/AE_Wendl/image/420/420/5cb25f33-825b-4361-a655-c8a44d8403cd/jpg')
    row = ah.wendl_row(sales[1], lots[0])
    assert row['item_id'] == 'ah-wendl-117-1300' and row['price_text'] == 'Limit 300 €' and row['buying'] == 'AUCTION'
    assert row['ends'] == '2027-02-27T22:59:00.000Z'            # 最后一天 23:59 柏林（冬令时 UTC+1）


def test_wendl_collect_pages_through_due_sale():
    session, out = FakeSession(), []
    f = ah.Fetcher(session, sleep=lambda s: None)
    assert ah.collect_wendl(f, date(2026, 10, 1), 90, out) is None
    assert [r['item_id'] for r in out] == ['ah-wendl-116-1300', 'ah-wendl-116-1320', 'ah-wendl-116-1321']
    lists = [u for _, u, _, _ in session.calls if 'listpage' in u]
    assert lists == ['https://auktionen.auktionshauswendl.de/de/listpage/116/138/%d?bidResultStatus=Unknown' % n for n in (0, 1)]


def test_german_days_cross_month_and_year():
    assert ah.german_days('30. | 31. Dezember | 2. Januar 2027') == [date(2026, 12, 30), date(2026, 12, 31), date(2027, 1, 2)]
    assert ah.german_days('nichts') == []


def test_mehlis_parse():
    sales = ah.mehlis_sales(fx('mehlis/catalogs.html'))
    assert [(s['sale'], s['cid'], s['start'].isoformat(), s['end'].isoformat()) for s in sales] == [
        (124, '12426', '2026-11-19', '2026-11-21'), (123, '12326', '2026-08-27', '2026-08-29')]
    timeline = ah.mehlis_timeline(fx('mehlis/timeline.html'))
    assert timeline[0] == 124
    porz = [r for r in timeline[1] if r['category'] == 'Porzellan'][0]
    assert (porz['lo'], porz['hi'], porz['at']) == (3200, 3425, datetime(2026, 11, 21, 10, 0))
    lots, last, cards = ah.mehlis_lots(fx('mehlis/porzellan_p1.html'))
    assert cards == 2 and ah.mehlis_lots('<html></html>')[2] is None
    assert last == 2 and [l['lot'] for l in lots] == ['3200', '3201']
    row = ah.mehlis_row(sales[0], timeline, lots[0])
    assert row['item_id'] == 'ah-mehlis-124-3200' and row['price'] == 480.0 and row['price_text'] == 'Limit 480 €'
    assert row['ends'] == '2026-11-21T09:00:00.000Z'            # 11-21 10:00 柏林
    assert row['image'] == 'https://www.mehlis.eu/media/catalog/12426/00003200/0003200.jpg'   # 换成清晰的原图
    assert row['url'] == 'https://www.mehlis.eu/de/catalogs/12426/item/3200/'


def test_lempertz_parse():
    cats = ah.lempertz_catalogues(fx('lempertz/auktionskataloge.html'))
    assert [(c['sale'], c['cid']) for c in cats] == [(1298, '726'), (1299, '754'), (1301, '739')]   # 没有目录号的在线专场不取
    assert cats[1]['start'] == datetime(2026, 11, 20, 15, 0)
    lots = ah.lempertz_lots(fx('lempertz/catalogue_754.json'))
    assert [l['lot'] for l in lots] == ['829', '877', '782a']   # 只取 Porzellan 类；拍品号带 position
    row = ah.lempertz_row(cats[1], lots[0])
    assert row['item_id'] == 'ah-lempertz-1299-829' and row['currency'] == 'EUR'
    assert row['price_text'].startswith('估价 ') and row['price_text'].endswith(' €') and '–' in row['price_text']
    assert row['ends'] == '2026-11-20T17:00:00.000Z'            # session_end 18:00 柏林
    with pytest.raises(ValueError):
        ah.lempertz_lots('<html>')


# ---------- 每日运行 ----------

def test_run_ingests_due_sales_and_skips_far_ones(db, cfg):
    session = FakeSession()
    summary, clock = run(db, cfg, session)
    st = status(db)
    assert st['wendl']['note'] == '下一场 2027-02-24，未到 90 天' and st['wendl']['new'] == 0
    assert summary['mehlis']['new'] == 3 and summary['lempertz']['new'] == 3 and not st['mehlis']['error']
    with db.connect() as c:
        rows = {r['item_id']: dict(r) for r in c.execute("SELECT * FROM items WHERE platform='auction_house'")}
    assert set(rows) == {'ah-mehlis-124-3200', 'ah-mehlis-124-3201', 'ah-mehlis-124-3202',
                         'ah-lempertz-1299-829', 'ah-lempertz-1299-877', 'ah-lempertz-1299-782a'}
    r = rows['ah-mehlis-124-3200']
    assert (r['buying'], r['currency'], r['listed_price'], r['total_price'], r['ends']) == (
        'AUCTION', 'EUR', 480.0, 480.0, '2026-11-21T09:00:00.000Z')      # 拍卖行不加 eBay 费率
    # 只读列表页与目录 JSON：没有打开任何拍品页、PDF 或图片
    assert not any('/item/' in u or 'lot-details' in u or '/kataloge/lot/' in u or u.endswith('.pdf')
                   or 'cdn.aeman.nl' in u for _, u, _, _ in session.calls)
    assert all(h['User-Agent'] == 'CurioObjectBot/0.1' for _, _, h, _ in session.calls)
    # 墨墨 2026-11-05：拍卖行的拍品进 01 新货瀑布流，不进拍卖新品列表
    auc = {i['item_id'] for i in db.auction_list(at='2026-11-05T11:00:00+00:00')['items']}
    with db.connect() as c:
        fresh = {iid for iid in rows if triage.state(c, iid) == 'new'}     # 其余被规则自动移出（破损等）
    feed = {i['item_id']: i for i in db.triage_feed(session='s')['items']}
    assert 'ah-mehlis-124-3200' in fresh and fresh <= set(feed) and not set(rows) & auc
    assert feed['ah-mehlis-124-3200']['platform'] == '梅利斯拍卖行'
    # 留下后进拍卖商品池（仍按纯拍卖处理），不进普通池
    with db.connect() as c:
        triage.keep(c, 'ah-mehlis-124-3200')
        assert triage.is_pure_auction(c, 'ah-mehlis-124-3200')
    # 同一柏林日再跑：不发请求
    n = len(session.calls)
    assert run(db, cfg, session)[0] == {} and len(session.calls) == n


def test_polite_spacing_and_lempertz_slower(db, cfg):
    session = FakeSession()
    _, clock = run(db, cfg, session)
    # 同一主机相邻两次请求：至少 5 秒；Lempertz 至少 10 秒
    gaps = {}
    last = {}
    for host, t in session.stamps:
        if host in last:
            gaps.setdefault(host, []).append(t - last[host])
        last[host] = t
    assert min(gaps['www.lempertz.com']) >= 10 and min(gaps['www.mehlis.eu']) >= 5
    assert all(min(v) >= 5 for v in gaps.values())
    c2 = Clock()
    ah.Fetcher(FakeSession(), interval=10, sleep=c2.sleep, monotonic=c2.monotonic).get('https://www.lempertz.com/de/auktionskataloge.html')
    assert c2.slept == [10]                                     # robots.txt 与目录页之间等 10 秒
    c3 = Clock()
    f = ah.Fetcher(FakeSession(), interval=5, sleep=c3.sleep, monotonic=c3.monotonic)
    f.get('https://www.mehlis.eu/de/catalogs/'); f.get('https://www.mehlis.eu/de/catalogs/timeline/')
    assert c3.slept == [5, 5]
    assert clock.slept and min(clock.slept) >= 5


def test_crawl_delay_from_robots_is_honoured():
    routes = {'https://www.mehlis.eu/robots.txt': (200, 'User-agent: *\nCrawl-delay: 30\n'),
              'https://www.mehlis.eu/de/catalogs/': (200, 'x')}
    c = Clock()
    ah.Fetcher(FakeSession(routes), interval=5, sleep=c.sleep, monotonic=c.monotonic).get('https://www.mehlis.eu/de/catalogs/')
    assert c.slept == [30]


def test_next_day_known_lots_not_new(db, cfg):
    run(db, cfg, FakeSession())
    with db.connect() as c:
        before = c.execute("SELECT count(*) FROM items WHERE platform='auction_house'").fetchone()[0]
    routes = pages()
    summary, _ = run(db, cfg, FakeSession(routes), NEXT_DAY)
    assert summary['mehlis']['new'] == 0 and summary['mehlis']['found'] == 3 and summary['lempertz']['new'] == 0
    with db.connect() as c:
        assert c.execute("SELECT count(*) FROM items WHERE platform='auction_house'").fetchone()[0] == before


@pytest.mark.parametrize('code', [403, 429])
def test_blocked_house_stops_others_continue(db, cfg, code):
    routes = pages()
    routes['https://www.mehlis.eu/de/catalogs/timeline/'] = (code, 'nope')
    session = FakeSession(routes)
    summary, _ = run(db, cfg, session)
    assert summary['mehlis']['error'] == '被拦（HTTP %d），今天停止' % code and summary['mehlis']['new'] == 0
    assert summary['lempertz']['new'] == 3
    mehlis = [u for _, u, _, _ in session.calls if 'mehlis' in u]
    assert mehlis.count('https://www.mehlis.eu/de/catalogs/timeline/') == 1          # 不重试
    assert not any('page=' in u for u in mehlis)
    n = len(session.calls)
    run(db, cfg, session)
    assert len(session.calls) == n                               # 当天不再试


def test_challenge_page_counts_as_blocked(db, cfg):
    routes = pages()
    routes['https://www.mehlis.eu/de/catalogs/'] = (200, '<html><div id="challenge-platform">verify you are human</div></html>')
    summary, _ = run(db, cfg, FakeSession(routes))
    assert summary['mehlis']['error'] == '被拦（出现验证页面），今天停止'


def test_robots_disallow_not_visited(db, cfg):
    routes = pages()
    routes['https://www.mehlis.eu/robots.txt'] = (200, 'User-agent: *\nDisallow: /de/catalogs/12426/\n')
    session = FakeSession(routes)
    summary, _ = run(db, cfg, session)
    assert summary['mehlis']['error'] == 'robots 禁止：/de/catalogs/12426/9/'
    assert not any('/12426/9/' in u for _, u, _, _ in session.calls)


def test_robots_for_our_bot_name():
    routes = {'https://www.mehlis.eu/robots.txt': (200, 'User-agent: CurioObjectBot\nDisallow: /\n'),
              'https://www.mehlis.eu/de/catalogs/': (200, 'x')}
    with pytest.raises(ah.RobotsDenied):
        ah.Fetcher(FakeSession(routes), ua=ah.user_agent('mail@example.org'), sleep=lambda s: None).get('https://www.mehlis.eu/de/catalogs/')
    assert ah.user_agent('mail@example.org') == 'CurioObjectBot/0.1 (+mail@example.org)'


def test_parse_failure_recorded_not_raised(db, cfg):
    routes = pages()
    routes['https://www.mehlis.eu/de/catalogs/12426/9/?page=1'] = (200, '<html><div class="neu">umgebaut</div></html>')
    summary, _ = run(db, cfg, FakeSession(routes))
    assert summary['mehlis']['error'] == '解析不到拍品，可能改版' and summary['lempertz']['new'] == 3


def test_request_cap_per_house():
    routes = {'https://www.mehlis.eu/robots.txt': (200, OPEN), 'https://www.mehlis.eu/x': (200, 'ok')}
    session = FakeSession(routes)
    f = ah.Fetcher(session, max_requests=40, sleep=lambda s: None)
    with pytest.raises(ah.LimitReached):
        for _ in range(50):
            f.get('https://www.mehlis.eu/x')
    assert len(session.calls) == 40


def test_only_allowed_enabled_with_adapter_visited(db, cfg):
    houses = [dict(h, status='ask') if h['key'] == 'mehlis' else dict(h, enabled=False) if h['key'] == 'lempertz' else h
              for h in cfg['auction_houses']]
    session = FakeSession()
    summary, _ = run(db, dict(cfg, auction_houses=houses), session)
    assert set(summary) == {'wendl'}
    assert set(session.hosts()) == {'www.auktionshauswendl.de'}
    visited = {h['key'] for h in cfg['auction_houses'] if ah.runnable(h)}
    assert visited == {'wendl', 'mehlis', 'lempertz'}            # 仓库名单：只有这三家会访问


def test_list_validation():
    keys = {h['key']: h for h in load()['auction_houses']}
    assert len(keys) >= 16
    assert all(keys[k]['status'] == 'forbidden' for k in ('lottissimo', 'the_saleroom', 'invaluable', 'liveauctioneers', 'catawiki', 'nagel'))
    assert keys['lempertz']['interval'] == 10
    with pytest.raises(ValueError, match='禁止程序采集'):
        validate_houses([dict(key='lottissimo', name='洛蒂西莫', site='', status='allowed', terms='x')])
    with pytest.raises(ValueError, match='status'):
        validate_houses([dict(key='x', name='某', site='', status='maybe', terms='x')])
    with pytest.raises(ValueError, match='interval'):
        validate_houses([dict(key='x', name='某', site='', status='allowed', terms='x', interval=1)])


# ---------- 纯拍卖口径、不可转换、不上客人页 ----------

def house_item(db, cfg, iid='ah-mehlis-124-3200', ends='2026-11-21T09:00:00.000Z'):
    db.ingest(dict(item_id=iid, platform='auction_house', title='KPM Berlin Tasse', price=480, currency='EUR', price_text='Limit 480 €',
                   buying='AUCTION', ends=ends, url='https://www.mehlis.eu/de/catalogs/12426/item/3200/',
                   image='https://www.mehlis.eu/media/catalog/12426/00003200/0003200.preview.jpg'), cfg)
    return iid


def test_pure_auction_not_convertible(db, cfg):
    iid = house_item(db, cfg)
    with db.connect() as c:
        assert triage.is_pure_auction(c, iid) and triage.is_auction_only(c.execute('SELECT * FROM items WHERE item_id=?', (iid,)).fetchone())
    with pytest.raises(Exception, match='拍卖行的拍品不能转为普通货'):
        db.auction_convert(iid, True, 'cv1')


def test_auction_rank_refused_by_default(db, cfg, monkeypatch):
    iid = house_item(db, cfg)
    db.triage_act('keep', iid, 'k1')
    now = datetime(2026, 11, 5, 12, tzinfo=BERLIN).timestamp()
    with db.connect() as c:
        code = triage.code(c, iid)
        assert publish.why_not_auction(c, code, now) == code + ' 拍卖行的照片和描述版权属对方，暂不上客人页'
        with pytest.raises(publish.PublishError, match='版权属对方'):
            publish.set_rank_auction(c, [code], now=now)
    monkeypatch.setattr('curio.publish.allow_auction_house', lambda root=None: True)
    with db.connect() as c:
        assert publish.why_not_auction(c, code, now) is None
        assert publish.set_rank_auction(c, [code], now=now) == [code]


def test_pool_archive_downloads_main_image_only(db, cfg, tmp_path):
    iid = house_item(db, cfg)
    db.triage_act('keep', iid, 'k1')
    got = []

    def dl(url, folder, headers=None):
        got.append((url, headers))
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'a.jpg').write_bytes(b'img')
        return dict(file='images/a.jpg', url=url, mime='image/jpeg')
    budget = Budget(db, cfg['api_budget'], lambda: NOON)
    shutil.copytree(ROOT / 'config', tmp_path / 'config')
    with patch('curio.vision.download', dl), patch('curio.curio_monitor.ka_curl') as ka, \
            patch('curio.archive_price.archive_price', return_value={}):
        summary = curio_monitor.archive_pool(db, tmp_path, None, cfg, budget, clock=lambda: NOON)
    assert summary['archived'] == 1 and not ka.called
    assert got == [('https://www.mehlis.eu/media/catalog/12426/00003200/0003200.preview.jpg', {'User-Agent': 'CurioObjectBot/0.1'})]
    assert all(v['check'] == 0 and v['fetch'] == 0 for v in budget.snapshot().values() if isinstance(v, dict) and 'check' in v)
    with db.connect() as c:
        assert vision.pool_archived(c, iid, tmp_path)


def test_ended_house_lot_closed_and_dropped_from_rank(db, cfg, monkeypatch):
    monkeypatch.setattr('curio.publish.allow_auction_house', lambda root=None: True)
    iid = house_item(db, cfg, ends='2026-11-05T09:00:00.000Z')
    db.triage_act('keep', iid, 'k1')
    with db.connect() as c:
        code = triage.code(c, iid)
        publish.set_rank_auction(c, [code], now=datetime(2026, 11, 5, 8, tzinfo=BERLIN).timestamp())
    rows = curio_monitor.close_ended_auctions(db, lambda: NOON)
    assert [r['item_id'] for r in rows] == [iid]
    with db.connect() as c:
        assert c.execute('SELECT status FROM items WHERE item_id=?', (iid,)).fetchone()[0] == 'gone'
        assert publish.rank_auction(c) == []


def test_house_not_checked_by_platform_checks(db, cfg):
    house_item(db, cfg)
    api = Mock()
    summary = curio_monitor.check_existing(db, api, dict(cfg, kleinanzeigen_enabled=False), None, None)
    assert not api.detail.called and summary['errors'] == 0
    assert curio_monitor.recheck_item(db, 'ah-mehlis-124-3200', cfg)['outcome'] == 'unknown'


# ---------- 采集轮与后台 ----------

def test_monitor_run_collects_once_per_day(tmp_path):
    shutil.copytree(ROOT / 'config', tmp_path / 'config')
    (tmp_path / '.env').write_text('EBAY_CLIENT_ID=dummy\nEBAY_CLIENT_SECRET=dummy\n')
    session = FakeSession()
    with patch('curio.curio_monitor.collect_ebay', return_value=0), \
            patch('curio.curio_monitor.collect_ka', side_effect=lambda s, c, b=None: s.set('ka_status', 'ok')), \
            patch('curio.curio_monitor.check_existing', return_value={'errors': 0}), \
            patch('curio.auction_houses.make_session', return_value=session), patch('curio.auction_houses.time.sleep'):
        result = curio_monitor.run(tmp_path, lambda: NOON)
        assert result['auction_house_new'] == 6 and result['ebay_status'] == 'ok'
        assert result['auction_houses']['wendl']['note'] == '下一场 2027-02-24，未到 90 天'
        n = len(session.calls)
        store = Store(tmp_path / 'data/curio.db')
        assert ah.run_due(store, load(tmp_path), lambda: NOON + 600) == {} and len(session.calls) == n
    view = ah.status_view(store, tmp_path, lambda: NOON)
    assert [(v['name'], v['checked_today'], v['new']) for v in view] == [('文德尔拍卖行', True, 0), ('梅利斯拍卖行', True, 3),
                                                                        ('伦佩茨拍卖行', True, 3)]
    assert ah.status_view(store, tmp_path, lambda: NEXT_DAY)[1]['checked_today'] is False


def test_house_error_does_not_break_run(tmp_path):
    shutil.copytree(ROOT / 'config', tmp_path / 'config')
    (tmp_path / '.env').write_text('EBAY_CLIENT_ID=dummy\nEBAY_CLIENT_SECRET=dummy\n')
    with patch('curio.curio_monitor.collect_ebay', return_value=0), \
            patch('curio.curio_monitor.collect_ka', side_effect=lambda s, c, b=None: s.set('ka_status', 'ok')), \
            patch('curio.curio_monitor.check_existing', return_value={'errors': 0}):
        result = curio_monitor.run(tmp_path, lambda: NOON)        # conftest 让建会话直接失败
    assert result['ebay_status'] == 'ok' and result['kleinanzeigen_status'] == 'ok'
    assert all(v['error'] for v in result['auction_houses'].values())


# ---------- 复审补充 ----------

def test_crawl_delay_capped():
    routes = {'https://www.mehlis.eu/robots.txt': (200, 'User-agent: *\nCrawl-delay: 3600\n'),
              'https://www.mehlis.eu/de/catalogs/': (200, 'x')}
    c = Clock()
    ah.Fetcher(FakeSession(routes), sleep=c.sleep, monotonic=c.monotonic).get('https://www.mehlis.eu/de/catalogs/')
    assert c.slept == [60]


def test_redirects():
    base = 'https://www.mehlis.eu'
    # robots.txt 的 301 照常跟随；跳进 robots 禁止的路径：不请求它
    routes = {base + '/robots.txt': (301, '', '/r/robots.txt'), base + '/r/robots.txt': (200, 'User-agent: *\nDisallow: /secret/\n'),
              base + '/a': (302, '', '/secret/x')}
    session = FakeSession(routes)
    with pytest.raises(ah.RobotsDenied):
        ah.Fetcher(session, sleep=lambda s: None).get(base + '/a')
    assert [u for _, u, _, _ in session.calls] == [base + '/robots.txt', base + '/r/robots.txt', base + '/a']
    # Mehlis 实况：robots.txt 302 到同站 http 地址 → 改回 https 再读（404 视为不限制），之后照常取页
    mehlis = {base + '/robots.txt': (302, '', 'http://www.mehlis.eu/de/robots.txt'), base + '/de/robots.txt': (404, ''),
              base + '/de/catalogs/': (200, 'ok')}
    session = FakeSession(mehlis)
    assert ah.Fetcher(session, sleep=lambda s: None).get(base + '/de/catalogs/') == 'ok'
    assert [u for _, u, _, _ in session.calls] == [base + '/robots.txt', base + '/de/robots.txt', base + '/de/catalogs/']
    # 跳转链超过 MAX_REDIRECTS
    loop = {base + '/robots.txt': (404, '')}
    loop.update({base + '/l%d' % i: (302, '', '/l%d' % (i + 1)) for i in range(10)})
    with pytest.raises(ah.FetchError, match='跳转过多'):
        ah.Fetcher(FakeSession(loop), sleep=lambda s: None).get(base + '/l0')
    # 跳到非 https
    plain = {base + '/robots.txt': (404, ''), base + '/p': (302, '', 'http://www.mehlis.eu/p')}
    with pytest.raises(ah.FetchError, match='非 https'):
        ah.Fetcher(FakeSession(plain), sleep=lambda s: None).get(base + '/p')
    # POST 遇 302 改为 GET、不带请求体
    post = {base + '/robots.txt': (404, ''), ('POST', base + '/q', '1'): (302, '', '/done'), base + '/done': (200, 'ok')}
    session = FakeSession(post)
    assert ah.Fetcher(session, sleep=lambda s: None).post_json(base + '/q', {'catalogue_uid': '1'}) == 'ok'
    assert session.calls[-1][0] == 'GET' and session.calls[-1][3] is None


def test_block_re_ignores_recaptcha_forms():
    page = '<html><title>Mehlis</title><div class="g-recaptcha" data-sitekey="x"></div>captcha Access denied text</html>'
    routes = {'https://www.mehlis.eu/robots.txt': (404, ''), 'https://www.mehlis.eu/n': (200, page)}
    assert ah.Fetcher(FakeSession(routes), sleep=lambda s: None).get('https://www.mehlis.eu/n') == page
    assert ah.BLOCK_RE.search('<title>Just a moment...</title>')


def test_contact_in_user_agent_on_every_request(db, cfg):
    session = FakeSession()
    run(db, dict(cfg, auction_house_bot=dict(cfg['auction_house_bot'], contact='x@y')), session)
    assert session.calls and all(h['User-Agent'] == 'CurioObjectBot/0.1 (+x@y)' for _, _, h, _ in session.calls)
    from curio.config import auction_house_bot
    with pytest.raises(ValueError, match='拉丁'):
        auction_house_bot({'contact': '墨墨'})


def test_wendl_sale_in_window_end_to_end(db, cfg):
    summary, _ = run(db, cfg, FakeSession(), datetime(2026, 10, 1, 12, tzinfo=BERLIN).timestamp())
    assert summary['wendl']['new'] == 3 and summary['wendl']['error'] is None
    with db.connect() as c:
        r = c.execute("SELECT * FROM items WHERE item_id='ah-wendl-116-1300'").fetchone()
    assert (r['platform'], r['price_text'], r['ends']) == ('auction_house', 'Limit 300 €', '2026-10-31T22:59:00.000Z')


def sold_only(page):
    """把 Wendl 列表页里的卡片都改成已成交（加 lot-zuschlag）。"""
    return page.replace('<span class="lot-limit">', '<span class="lot-zuschlag">Kaufpreis 1 €</span><span class="lot-limit">')


def test_all_sold_page_then_page_with_lots():
    routes = pages()
    w = 'https://auktionen.auktionshauswendl.de/de/listpage/116/138/%d?bidResultStatus=Unknown'
    routes[w % 0] = (200, sold_only(fx('wendl/porzellan_p0.html')))                       # 整页已成交，说末页是 1
    routes[w % 1] = (200, fx('wendl/porzellan_p1.html').replace('/138/1?', '/138/2?'))   # 这页说末页是 2
    routes[w % 2] = (200, fx('wendl/porzellan_p1.html').replace('1321', '1399').replace('86', '96'))
    out = []
    note = ah.collect_wendl(ah.Fetcher(FakeSession(routes), sleep=lambda s: None), date(2026, 10, 1), 90, out)
    assert note is None and [r['item_id'] for r in out] == ['ah-wendl-116-1321', 'ah-wendl-116-1399']


def test_zero_unsold_lots_is_note_not_error(db, cfg):
    routes = pages()
    w = 'https://auktionen.auktionshauswendl.de/de/listpage/116/138/%d?bidResultStatus=Unknown'
    routes[w % 0] = (200, sold_only(fx('wendl/porzellan_p0.html')))
    routes[w % 1] = (200, sold_only(fx('wendl/porzellan_p1.html')))
    summary, _ = run(db, cfg, FakeSession(routes), datetime(2026, 10, 1, 12, tzinfo=BERLIN).timestamp())
    assert summary['wendl']['error'] is None and summary['wendl']['note'] == '90 天内的场次里没有未成交的瓷器拍品'
    routes[w % 0] = (200, '<html>umgebaut</html>')
    summary, _ = run(Store(db.path.parent / 'other.db'), cfg, FakeSession(routes), datetime(2026, 10, 1, 12, tzinfo=BERLIN).timestamp())
    assert summary['wendl']['error'] == '解析不到拍品，可能改版'


def test_mehlis_timeline_failure_falls_back(db, cfg):
    routes = pages()
    routes['https://www.mehlis.eu/de/catalogs/timeline/'] = (500, 'oops')
    summary, _ = run(db, cfg, FakeSession(routes))
    assert summary['mehlis']['error'] is None and summary['mehlis']['found'] == 3
    with db.connect() as c:
        assert c.execute("SELECT ends FROM items WHERE item_id='ah-mehlis-124-3200'").fetchone()[0] == '2026-11-21T22:59:00.000Z'


def test_lempertz_tolerates_odd_values():
    data = json.loads(fx('lempertz/catalogue_754.json'))
    data['lots'][0].update(hammerprice='0', estimate='600', estimate_to='800', position=None)
    data['lots'][1].update(hammerprice='1200')                  # 已成交（字符串）：不取
    data['lots'].insert(0, {'uid': data['lots'][0]['uid'], 'number': object(), 'position': 'a'})   # 坏的一件：跳过
    lots = ah.lempertz_lots(json.dumps(data, default=str))
    assert [l['lot'] for l in lots][-2:] == ['829', '782a'] and '877' not in [l['lot'] for l in lots]
    cat = ah.lempertz_catalogues(fx('lempertz/auktionskataloge.html'))[1]
    row = ah.lempertz_row(cat, [l for l in lots if l['lot'] == '829'][0])
    assert row['price'] == 600.0 and row['price_text'] == '估价 600–800 €'


def test_ended_rows_not_reingested(db, cfg):
    house_item(db, cfg, 'ah-mehlis-124-3200', ends='2026-11-21T09:00:00.000Z')
    with db.connect() as c:
        c.execute("UPDATE items SET status='gone' WHERE item_id='ah-mehlis-124-3200'")
    summary, _ = run(db, cfg, FakeSession(), datetime(2026, 11, 22, 12, tzinfo=BERLIN).timestamp())
    with db.connect() as c:
        assert c.execute("SELECT status FROM items WHERE item_id='ah-mehlis-124-3200'").fetchone()[0] == 'gone'
    assert summary['mehlis']['found'] == 0 and summary['mehlis']['new'] == 0


def test_run_time_budget_leaves_rest_for_next_round(db, cfg):
    session = FakeSession()
    c = Clock()
    summary = ah.run_due(db, cfg, lambda: NOON, session, c.sleep, c.monotonic, run_seconds=1)
    assert list(summary) == ['wendl']                            # 第一家用掉时限，其余两家本轮不开始
    assert set(status(db)) == {'wendl'}
    summary = ah.run_due(db, cfg, lambda: NOON, session, c.sleep, c.monotonic)
    assert set(summary) == {'mehlis', 'lempertz'}


def test_vision_download_house_hosts(tmp_path):
    seen = []

    class Resp:
        status_code = 200
        headers = {'Content-Type': 'image/jpeg'}
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def iter_content(self, n): return [b'\xff\xd8img']

    def get(url, **kw):
        seen.append((url, kw.get('headers')))
        return Resp()
    urls = ['https://cdn.aeman.nl/AE_Wendl/image/420/420/x/jpg', 'https://www.mehlis.eu/media/catalog/1/a.jpg',
            'https://www.lempertz.com/lempertz_api/images/smedium/a.jpg']
    with patch('curio.vision.requests.get', get):
        for u in urls:
            assert vision.download(u, tmp_path, {'User-Agent': ah.BOT})['url'] == u
    assert seen == [(u, {'User-Agent': 'CurioObjectBot/0.1'}) for u in urls]


def test_vision_url_keeps_house_images():
    from curio import ai
    for u in ('https://cdn.aeman.nl/AE_Wendl/image/420/420/x/jpg', 'https://www.mehlis.eu/media/catalog/1/a.jpg',
              'https://www.lempertz.com/lempertz_api/images/smedium/a.jpg'):
        assert ai.vision_url(u) == u


def test_idle_round_archives_ended_house_lot_without_ebay(tmp_path):
    shutil.copytree(ROOT / 'config', tmp_path / 'config')
    (tmp_path / '.env').write_text('EBAY_CLIENT_ID=dummy\nEBAY_CLIENT_SECRET=dummy\n')
    cfg = dict(load(tmp_path), keywords=['Meissen'])
    patches = [patch('curio.curio_monitor.collect_ebay', return_value=0),
               patch('curio.curio_monitor.collect_ka', side_effect=lambda s, c, b=None: s.set('ka_status', 'ok')),
               patch('curio.curio_monitor.check_existing', return_value={'errors': 0}),
               patch('curio.auction_houses.make_session', return_value=FakeSession())]
    for p in patches:
        p.start()
    try:
        curio_monitor.run(tmp_path, lambda: NOON)                # 先跑一轮建库
        db = Store(tmp_path / 'data/curio.db')
        iid = house_item(db, cfg, 'ah-mehlis-124-9999', ends='2026-11-05T09:00:00.000Z')
        db.triage_act('keep', iid, 'k9')
        got = []

        def dl(url, folder, headers=None):
            got.append(url)
            folder.mkdir(parents=True, exist_ok=True)
            (folder / 'b.jpg').write_bytes(b'img')
            return dict(file='images/b.jpg', url=url, mime='image/jpeg')
        with patch('curio.curio_monitor.EbayAPI', side_effect=RuntimeError('no creds')), patch('curio.vision.download', dl), \
                patch('curio.archive_price.archive_price', return_value={}), patch('curio.sync._notify', create=True), \
                patch('curio.schedule.Scheduler.fetch_due', return_value=False), \
                patch('curio.schedule.Scheduler.check_due', return_value=False), \
                patch('curio.schedule.Scheduler.published_check_due', return_value=False):
            result = curio_monitor.run(tmp_path, lambda: NOON + 300)    # 空闲轮
    finally:
        for p in patches:
            p.stop()
    assert result['auction_ended'] == 1 and result.get('status') == 'idle'
    assert got == ['https://www.mehlis.eu/media/catalog/12426/00003200/0003200.preview.jpg']
    with db.connect() as c:
        assert vision.pool_archived(c, iid, tmp_path)


def test_big_image_for_first_picture():
    assert ah.big_image('https://www.mehlis.eu/media/catalog/12426/00003200/0003200.preview.jpg') == \
        'https://www.mehlis.eu/media/catalog/12426/00003200/0003200.jpg'
    assert ah.big_image('https://www.lempertz.com/lempertz_api/images/smedium/1299-700-A.jpg') == \
        'https://www.lempertz.com/lempertz_api/images/large/1299-700-A.jpg'
    assert ah.big_image('https://cdn.aeman.nl/AE_Wendl/image/420/420/5cb2/jpg') == 'https://cdn.aeman.nl/AE_Wendl/image/5cb2/jpg'
    assert ah.big_image('https://example.org/a.jpg') == 'https://example.org/a.jpg'
    assert ah.big_image('') == ''
