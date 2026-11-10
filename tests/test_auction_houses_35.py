"""故事 35：Bruun Rasmussen、Schuler、Döbritz、Dobiaschofsky。样本为 2026-11-10 取回后裁剪的真实页面（tests/fixtures/auction_houses/）；
日期在过去或超窗口的，用替换造「窗口内」的版本。全部用假会话与临时库，不连外部主机。"""
import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import urlsplit

import pytest

from curio import ai, auction_houses as ah, triage, vision
from curio.config import load
from curio.schedule import BERLIN
from curio.store import Store

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / 'tests/fixtures/auction_houses'
NOON = datetime(2026, 11, 10, 12, tzinfo=BERLIN).timestamp()
TODAY = date(2026, 11, 10)
OPEN = 'User-agent: *\nAllow: /\n'
BR = 'https://bruun-rasmussen.dk'
SCH = 'https://www.schulerauktionen.ch'
DOE = 'https://auktionen.doebritz.de'
DOBI = 'https://www.dobiaschofsky.com'
DOE_SEARCH = DOE + '/de/objekte/au-26/nachverkauf_jubilaeumsauktion?Q=%s&Lstatus=0&pagenumber=%d'
SCH_NEXT = SCH + '/de/auctions/a182?category=tischkultur&online_only_sale_message=true&page=2'


def fx(path):
    return (FIX / path).read_text(encoding='utf-8')


def fxb(path):
    return (FIX / path).read_bytes()


# ---------- 各家的路由（裁剪样本 + 替换） ----------

def bruun_empty():
    """同一场的瓷器分类页去掉全部拍品（其他场没有瓷器时的样子）。"""
    page = re.sub(r'<li id="lot_(?!placeholder).*?</li>', '', fx('bruun/auction_1009096.html'), flags=re.S)
    return page.replace('data-item-count="39"', 'data-item-count="0"')


def bruun_routes():
    lst = BR + '/m/auctions/%s?category_id=492&cas=guest&locale=en'
    return {
        BR + '/robots.txt': (200, 'User-agent: *\nDisallow: /m/search\n'),
        BR + '/m/auctions?cas=guest&locale=en': (200, fx('bruun/auctions.html')),     # 11-17 三场
        lst % '1009096': (200, fx('bruun/auction_1009096.html')),                    # 古董场：瓷器 39 件
        lst % '1009097': (200, bruun_empty()),
        lst % '1008672': (200, bruun_empty()),
    }


def schuler_routes(online=False):
    routes = {SCH + '/robots.txt': (200, OPEN), SCH + '/de/auctions': (200, fx('schuler/auctions.html'))}
    if online:
        # A182 改成 12-01 至 12-04、图录已上线；第 1 页两件改成迈森（一件已成交），第 2 页一件 KPM、没有下一页
        routes[SCH + '/de/auctions'] = (200, fx('schuler/auctions.html').replace('8.9.2026 – 12.9.2026', '1.12.2026 – 4.12.2026'))
        p1 = fx('schuler/a182_tischkultur_p1.html').replace('9.9.2026, 09:00', '2.12.2026, 09:00') \
            .replace('1000. Saint-Louis', '1000. Meissen, Saint-Louis').replace('1001. Saint-Louis', '1001. Meissen, Saint-Louis')
        p2 = re.sub(r'<a class="invisible" rel="next"[^>]*>Next Page</a>', '', p1.replace('1000. Meissen', '1100. Meissen')
                    .replace('1001. Meissen', '1101. Mokkaservice KPM'))
        routes[SCH + '/de/auctions/a182?category=tischkultur&page=1'] = (200, p1)
        routes[SCH_NEXT] = (200, p2)
    return routes


def doebritz_routes():
    meissen = fx('doebritz/au26_meissen.html').replace('data-maxpage="9"', 'data-maxpage="2"')
    kpm = fx('doebritz/au26_kpm.html').replace('data-maxpage="3"', 'data-maxpage="1"')
    # kpm 第一页开头插一件 meissen 结果里已有的拍品：两次搜索都出现的只收一次
    block = re.search(r'<div id="pos_1".*?(?=<div id="pos_2")', meissen, re.S)[0]
    kpm = kpm.replace('<div id="pos_1"', block.replace('pos_1', 'pos_0') + '<div id="pos_1"', 1)
    return {
        DOE + '/robots.txt': (200, OPEN),
        DOE + '/de/Auktionen/Aktuelle': (200, fx('doebritz/aktuelle.html')),     # au-26 拍后补售，到 12-21
        DOE_SEARCH % ('meissen', 1): (200, meissen),
        DOE_SEARCH % ('meissen', 2): (200, meissen),                             # 第 2 页与第 1 页同样的拍品：去重
        DOE_SEARCH % ('kpm', 1): (200, kpm),
    }


def unsold(page, number):
    """去掉一件拍品的 Zuschlag 行（样本 A142 已成交，造在拍样本）。"""
    start = page.index('<h4>%s</h4>' % number)
    end = page.index('</li>', start)
    chunk = re.sub(r'<tr>\s*<td[^>]*>Zuschlag</td>.*?</tr>\s*<tr>\s*<td colspan=3[^>]*><span[^>]*>Zuschlag.*?</tr>', '',
                   page[start:end], flags=re.S)
    return page[:start] + chunk + page[end:]


def dobi_routes(in_window=False):
    home = fxb('dobi/home.html')
    kat = fxb('dobi/d147_kat525.html')
    if in_window:
        # 「Letzte Auktion」改成 11-24 至 11-27（窗口内、有图录链接）；1417 去掉 Zuschlag
        home = home.replace(b"4.-</span><span style='white-space:nowrap'>7. November 2026",
                            b"24.-</span><span style='white-space:nowrap'>27. November 2026")
        kat = unsold(kat.decode('cp1252'), 1417).encode('cp1252')
    return {DOBI + '/robots.txt': (404, ''), DOBI + '/': (200, home), DOBI + '/d147.html?kat=525&setp=1': (200, kat)}


class FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def request(self, method, url, headers=None, timeout=None, allow_redirects=True, json=None):
        assert urlsplit(url).scheme == 'https'
        self.calls.append((method, url, dict(headers or {})))
        code, text = self.routes.get(url, (404, 'not found'))
        content = text if isinstance(text, bytes) else text.encode('utf-8')
        if isinstance(text, bytes):
            text = text.decode('latin-1')
        return Mock(status_code=code, text=text, content=content, headers={})

    def urls(self):
        return [u for _, u, _ in self.calls]


class Clock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s


@pytest.fixture
def cfg():
    return dict(load(), keywords=['Meissen'])


@pytest.fixture
def db(tmp_path):
    return Store(tmp_path / 'data' / 'curio.db')


def only(cfg, *keys):
    return dict(cfg, auction_houses=[h for h in cfg['auction_houses'] if h['key'] in keys])


def run(db, cfg, routes, clock=NOON):
    session, c = FakeSession(routes), Clock()
    return ah.run_due(db, cfg, lambda: clock, session, c.sleep, c.monotonic), session


def collect(fn, routes, today=TODAY):
    out, session = [], FakeSession(routes)
    note = fn(ah.Fetcher(session, sleep=lambda s: None), today, 90, out, now=datetime(2026, 11, 10, 11, tzinfo=timezone.utc))
    return note, out, session


def items(db, prefix):
    with db.connect() as c:
        return {r['item_id']: dict(r) for r in c.execute('SELECT * FROM items WHERE item_id LIKE ?', (prefix + '%',))}


# ---------- 通用 ----------

def test_row_currency_defaults_and_maker_keywords():
    r = ah.row('x', 1, '2', 't', '', 10.0, '', '', '', '')
    assert (r['currency'], r['country']) == ('EUR', 'DE')
    r = ah.row('x', 1, '2', 't', '', 10.0, '', '', '', '', currency='CHF', country='CH')
    assert (r['currency'], r['country']) == ('CHF', 'CH')
    for t in ('Meissen, um 1900', 'MEISSEN teapot', 'Meißen Teller', 'Vase, KPM Berlin', 'K.P.M. Tasse',
              'Königliche Porzellan Manufaktur', 'Königliche Porzellan-Manufaktur Berlin', 'Porzellanmanufaktur Berlin'):
        assert ah.maker_wanted(t), t
    assert not ah.maker_wanted('Royal Copenhagen, Flora Danica', 'Herend', '')
    assert ah.numeric_days('So 18.10.2026&nbsp;bis&nbsp;Mo 21.12.2026') == [date(2026, 10, 18), date(2026, 12, 21)]
    assert ah.plain_number("CHF  1&#39;000 ") == 1000.0 and ah.plain_number('1’200.00') == 1200.0 and ah.plain_number('250') == 250.0


def test_big_image_bruun_and_doebritz():
    assert ah.big_image('https://img.bruun-rasmussen.dk/g/BRFull/Live/936/7133-936_470x470.jpg?v=7') == \
        'https://img.bruun-rasmussen.dk/g/BRFull/Live/936/7133-936_1000x1000.jpg?v=7'
    assert ah.big_image('https://auktionen.doebritz.de/Lots/Images/1-214NV-482.jpg?crop=True') == \
        'https://auktionen.doebritz.de/Lots/Images/1-214NV-482.jpg'
    assert ah.big_image('https://images.schulerauktionen.ch/item_images/a182/1/x/lg_1000.webp') == \
        'https://images.schulerauktionen.ch/item_images/a182/1/x/lg_1000.webp'


def test_image_hosts_whitelisted():
    for u in ('https://img.bruun-rasmussen.dk/g/BRFull/Live/936/7133-936_1000x1000.jpg?v=7',
              'https://images.schulerauktionen.ch/item_images/a182/1/x/lg_1000.webp',
              'https://auktionen.doebritz.de/Lots/Images/1-214NV-482.jpg',
              'https://www.dobiaschofsky.com/abblot/A142/500/684085_1.jpg'):
        assert urlsplit(u).hostname in vision.HOUSE_IMAGE_HOSTS and ai.vision_url(u) == u


def test_list_story35():
    houses = load()['auction_houses']
    keys = {h['key']: h for h in houses}
    for k in ('bruun_rasmussen', 'schuler', 'doebritz', 'dobiaschofsky'):
        assert (keys[k]['status'], keys[k]['adapter'], keys[k]['enabled']) == ('allowed', True, True) and ah.runnable(keys[k])
    assert (keys['jordaens']['adapter'], keys['jordaens']['enabled']) == (False, False)
    assert '看不出属于哪场' in keys['jordaens']['terms'] and not ah.runnable(keys['jordaens'])
    assert [h['key'] for h in houses if ah.runnable(h)][-1] == 'historia'


# ---------- Bruun Rasmussen ----------

def test_bruun_parse():
    sales = ah.bruun_sales(fx('bruun/auctions.html'))
    assert [(s['id'], s['start']) for s in sales] == [('1009096', date(2026, 11, 17)), ('1009097', date(2026, 11, 17)),
                                                      ('1008672', date(2026, 11, 17))]
    assert sales[0]['title'] == 'Antiques – Decorative art, furniture and carpets'
    lots, cards = ah.bruun_lots(fx('bruun/auction_1009096.html'))
    assert cards == 39 and len(lots) == 39
    assert ah.bruun_lots('<html>')[1] is None and ah.bruun_lots(bruun_empty()) == ([], 0)
    lot = next(l for l in lots if l['lot'] == '313')               # 只有单个估价（currency-amount）
    assert (lot['lo'], lot['hi'], lot['currency']) == (20000.0, None, 'DKK')


def test_bruun_only_meissen_dkk_big_image(db, cfg):
    summary, session = run(db, only(cfg, 'bruun_rasmussen'), bruun_routes())
    assert summary['bruun_rasmussen']['error'] is None and summary['bruun_rasmussen']['found'] == 5
    rows = items(db, 'ah-bruun_rasmussen-')
    assert set(rows) == {'ah-bruun_rasmussen-936-%d' % n for n in (296, 325, 346, 422, 459)}
    r = rows['ah-bruun_rasmussen-936-325']
    assert (r['currency'], r['country'], r['listed_price'], r['price_text'], r['buying']) == ('DKK', 'DK', 40000.0, '估价 40000–60000 DKK', 'AUCTION')
    assert r['title'] == 'J.J. Kändler: A Meissen porcelain teapot c. 1735-40.' and r['description'].endswith('L. 21 cm.')
    assert r['image'] == 'https://img.bruun-rasmussen.dk/g/BRFull/Live/936/7105-936_1000x1000.jpg?v=8'
    assert r['url'] == 'https://bruun-rasmussen.dk/m/lots/9DD57F47D224' and r['ends'] == '2026-11-17T15:00:00.000Z'
    assert all(row['image'].endswith(('_1000x1000.jpg?v=7', '_1000x1000.jpg?v=8', '_1000x1000.jpg?v=6')) for row in rows.values())
    # 只读场次列表与瓷器分类：不访问 /m/search、不打开拍品页
    urls = session.urls()
    assert not any('/m/search' in u or '/m/lots/' in u for u in urls)
    assert all('cas=guest' in u for u in urls if '/m/auctions' in u)
    assert all(h['User-Agent'] == 'CurioObjectBot/0.1' for _, _, h in session.calls)


def test_bruun_robots_search_denied():
    with pytest.raises(ah.RobotsDenied):
        ah.Fetcher(FakeSession(bruun_routes()), sleep=lambda s: None).get(BR + '/m/search?q=meissen')


def test_bruun_one_sale_missing_others_continue():
    routes = bruun_routes()
    del routes[BR + '/m/auctions/1009097?category_id=492&cas=guest&locale=en']
    note, out, _ = collect(ah.collect_bruun_rasmussen, routes)
    assert len(out) == 5 and note == '1 场的瓷器分类没读到'


# ---------- Schuler ----------

def test_schuler_catalogue_not_online(db, cfg):
    summary, session = run(db, only(cfg, 'schuler'), schuler_routes())
    st = summary['schuler']
    assert st['error'] is None and '图录 2026-11-15 上线' in st['note'] and st['found'] == 0
    assert session.urls() == [SCH + '/robots.txt', SCH + '/de/auctions']            # 不读列表


def test_schuler_online_pages_skip_sold_only_maker(db, cfg):
    summary, session = run(db, only(cfg, 'schuler'), schuler_routes(online=True))
    st = summary['schuler']
    assert st['error'] is None and st['found'] == 2 and 'A183 场图录 2026-11-15 上线' in st['note']
    assert session.urls()[-2:] == [SCH + '/de/auctions/a182?category=tischkultur&page=1', SCH_NEXT]
    rows = items(db, 'ah-schuler-')
    assert set(rows) == {'ah-schuler-182-1001', 'ah-schuler-182-1101'}                 # 1000、1100 已有 Zuschlag
    r = rows['ah-schuler-182-1001']
    assert (r['currency'], r['country'], r['listed_price'], r['price_text']) == ('CHF', 'CH', 1000.0, '估价 1000–2000 CHF')
    assert r['title'] == 'Meissen, Saint-Louis, 1 Karaffe und 6 Whiskygläser "Thistle"' and r['ends'] == '2026-12-02T08:00:00.000Z'
    assert r['url'] == SCH + '/de/items/182-1001-saint-louis-1-karaffe-und-6-whiskyglaeser-thistle'
    assert r['image'].startswith('https://images.schulerauktionen.ch/item_images/a182/') and '/lg_' in r['image']


# ---------- Döbritz ----------

def test_doebritz_nachverkauf_two_searches_dedup(db, cfg):
    summary, session = run(db, only(cfg, 'doebritz'), doebritz_routes())
    st = summary['doebritz']
    assert st['error'] is None and st['found'] == 44                      # 24 件迈森 + 20 件未售 KPM（4 件已售不收）
    assert session.urls()[2:] == [DOE_SEARCH % ('meissen', 1), DOE_SEARCH % ('meissen', 2), DOE_SEARCH % ('kpm', 1)]
    rows = items(db, 'ah-doebritz-')
    r = rows['ah-doebritz-26-482-10110']
    assert (r['currency'], r['listed_price'], r['price_text'], r['ends']) == ('EUR', 1000.0, '拍后补售 · 起价 1000 €', '2026-12-21T22:59:00.000Z')
    assert r['image'] == DOE + '/Lots/Images/1-214NV-482.jpg' and r['url'] == DOE + '/de/l/10110/hentschelkind_auf_spielzeugpferd_meissen_um_1910'
    assert {'ah-doebritz-26-698-3970', 'ah-doebritz-26-698-4755'} <= set(rows)          # 补售场里两件不同的 698
    assert not any(k.startswith('ah-doebritz-26-694') for k in rows)                    # 已售
    assert rows['ah-doebritz-26-1072-13799']['image'] == ''                             # 「暂无图片」占位不收


def test_doebritz_regular_sale_price_text():
    sale = dict(sale=221, nachverkauf=False, end=date(2027, 2, 28))
    r = ah.doebritz_row(sale, dict(lot='10', id='', title='Tasse, Meissen', start=1200.0, url='', image=''))
    assert (r['item_id'], r['price_text'], r['ends']) == ('ah-doebritz-221-10', '起拍 1200 €', '2027-02-28T22:59:00.000Z')


# ---------- Dobiaschofsky ----------

def test_dobi_next_sale_out_of_window(db, cfg):
    summary, session = run(db, only(cfg, 'dobiaschofsky'), dobi_routes())
    assert summary['dobiaschofsky']['note'] == '下一场 2027-04-28，未到 90 天' and summary['dobiaschofsky']['error'] is None
    assert session.urls() == [DOBI + '/robots.txt', DOBI + '/']


def test_dobi_in_window_cp1252_skip_sold(db, cfg):
    summary, session = run(db, only(cfg, 'dobiaschofsky'), dobi_routes(in_window=True))
    assert summary['dobiaschofsky']['error'] is None and summary['dobiaschofsky']['found'] == 4
    rows = items(db, 'ah-dobiaschofsky-')
    assert set(rows) == {'ah-dobiaschofsky-142-%d' % n for n in (1390, 1397, 1407, 1417)}
    r = rows['ah-dobiaschofsky-142-1397']
    assert (r['currency'], r['country'], r['listed_price'], r['price_text'], r['ends']) == (
        'CHF', 'CH', 500.0, '估价 500 CHF', '2026-11-27T22:59:00.000Z')
    assert r['title'] == 'PAAR FIGURENLEUCHTER – Meissen, 1850-1924 (Königliche Porzellan Manufaktur).'   # cp1252 解码正确
    assert r['image'] == DOBI + '/abblot/A142/500/685929.jpg' and r['url'].startswith(DOBI + '/d147-A142-11397-')
    assert session.urls()[-1] == DOBI + '/d147.html?kat=525&setp=1'


def test_dobi_pages_and_missing_catalogue():
    page = ah.dobi_decode(fxb('dobi/d147_kat525.html'))
    assert ah.dobi_lots(page)[2] is False
    more = page.replace('</ul>', '</ul><a href="/d147.html?kat=525&setp=2">2</a>')
    assert ah.dobi_lots(more, 1)[2] is True and ah.dobi_lots(more, 2)[2] is False
    assert ah.dobi_lots('<html>')[3] is None
    # 窗口内的场还没有图录链接：只写说明，不读列表
    home = fxb('dobi/home.html').replace(b'28. April - ', b'1. Dezember - ').replace(b'1. Mai 2027', b'3. Dezember 2026')
    note, out, session = collect(ah.collect_dobiaschofsky, {DOBI + '/robots.txt': (404, ''), DOBI + '/': (200, home)})
    assert out == [] and note == 'FRÜHJAHRSAUKTION 2027 图录未上线' and len(session.calls) == 2


# ---------- 四家一起：入 01 新货 ----------

def test_four_houses_run_into_new_feed(db, cfg):
    routes = {**bruun_routes(), **schuler_routes(online=True), **doebritz_routes(), **dobi_routes(in_window=True)}
    summary, session = run(db, only(cfg, 'bruun_rasmussen', 'schuler', 'doebritz', 'dobiaschofsky'), routes)
    assert {k: v['found'] for k, v in summary.items()} == {'doebritz': 44, 'bruun_rasmussen': 5, 'dobiaschofsky': 4, 'schuler': 2}
    assert all(v['error'] is None for v in summary.values())
    with db.connect() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM items WHERE platform='auction_house'")]
        assert len(rows) == 55 and all(r['buying'] == 'AUCTION' for r in rows)
        assert all(ah.maker_wanted(r['title'], r['description']) for r in rows)
        fresh = {r['item_id'] for r in rows if triage.state(c, r['item_id']) == 'new'}
    assert {r['currency'] for r in rows if r['item_id'].startswith(('ah-schuler-', 'ah-dobiaschofsky-'))} == {'CHF'}
    assert {r['currency'] for r in rows if r['item_id'].startswith('ah-bruun_rasmussen-')} == {'DKK'}
    assert {r['currency'] for r in rows if r['item_id'].startswith('ah-doebritz-')} == {'EUR'}
    feed, cursor = {}, None
    while True:
        page = db.triage_feed(limit=100, cursor=cursor, session='s')
        feed.update((i['item_id'], i) for i in page['items'])
        cursor = page['next_cursor']
        if not cursor or not page['items']:
            break
    assert fresh and fresh <= set(feed)
    names = {feed[i]['platform'] for i in fresh}
    assert names <= {'布鲁恩·拉斯穆森拍卖行', '舒勒拍卖行', '德布里茨拍卖行', '多比亚肖夫斯基拍卖行'}
    st = json.loads(db.get('ah_status'))
    assert set(st) == {'bruun_rasmussen', 'schuler', 'doebritz', 'dobiaschofsky'}


# ---------- 复审补充 ----------

BROKEN = '<html><div class="neu">umgebaut</div></html>'
BR_LIST = BR + '/m/auctions/%s?category_id=492&cas=guest&locale=en'


def test_parse_failure_recorded_not_raised(db, cfg):
    routes = {**bruun_routes(), **schuler_routes(online=True), **doebritz_routes(), **dobi_routes(in_window=True)}
    for sale in ('1009096', '1009097', '1008672'):
        routes[BR_LIST % sale] = (200, BROKEN)
    routes[SCH + '/de/auctions/a182?category=tischkultur&page=1'] = (200, BROKEN)
    for url in (DOE_SEARCH % ('meissen', 1), DOE_SEARCH % ('kpm', 1)):
        routes[url] = (200, BROKEN)
    routes[DOBI + '/d147.html?kat=525&setp=1'] = (200, BROKEN.encode())
    summary, _ = run(db, only(cfg, 'bruun_rasmussen', 'schuler', 'doebritz', 'dobiaschofsky'), routes)
    assert {k: v['error'] for k, v in summary.items()} == dict.fromkeys(
        ('bruun_rasmussen', 'schuler', 'doebritz', 'dobiaschofsky'), ah.PARSE_FAILED)


def test_bruun_every_sale_404_is_error(db, cfg):
    routes = bruun_routes()
    for sale in ('1009096', '1009097', '1008672'):
        del routes[BR_LIST % sale]
    summary, _ = run(db, only(cfg, 'bruun_rasmussen'), routes)
    assert summary['bruun_rasmussen']['error'].startswith('采集未完成：HTTP 404') and summary['bruun_rasmussen']['found'] == 0


def test_bruun_broken_and_partial_sale_notes():
    routes = bruun_routes()
    routes[BR_LIST % '1009097'] = (200, BROKEN)
    routes[BR_LIST % '1009096'] = (200, fx('bruun/auction_1009096.html').replace('data-item-count="39"', 'data-item-count="50"'))
    note, out, _ = collect(ah.collect_bruun_rasmussen, routes)
    assert len(out) == 5 and note == '1 场的瓷器分类页面结构变了；1009096 场只显示了 39 / 50 件'


def test_bruun_row_without_hammer_time_ends_at_day_end():
    sale = ah.bruun_sales(fx('bruun/auctions.html'))[0]
    lot = dict(sale='936', lot='1', title='Meissen', description='', lo=None, hi=None, currency='DKK', at=None, url='', image='')
    assert ah.bruun_row(sale, lot)['ends'] == '2026-11-17T22:59:00.000Z'


def test_schuler_swiss_dash_estimate():
    assert ah.plain_number("1'000.–") == 1000.0 and ah.plain_number('2’000.-') == 2000.0
    page = fx('schuler/a182_tischkultur_p1.html').replace('CHF  1&#39;000 –  2&#39;000', 'CHF 1&#39;000.– – 2&#39;000.–', 1)
    lot = next(l for l in ah.schuler_lots(page, 'a182')[0] if l['lot'] == '1001')
    assert (lot['lo'], lot['hi']) == (1000.0, 2000.0)


def test_doebritz_query_without_list_is_no_hits():
    routes = doebritz_routes()
    routes[DOE_SEARCH % ('kpm', 1)] = (200, BROKEN)                  # 如零结果的搜索页：没有 #lotlist
    note, out, _ = collect(ah.collect_doebritz, routes)
    assert len(out) == 24 and note is None


def test_dobi_collector_pages_through_setp():
    routes = dobi_routes(in_window=True)
    p1 = routes[DOBI + '/d147.html?kat=525&setp=1'][1].decode('cp1252')
    p1 = p1.replace('</ul>', '</ul><a href="/d147.html?kat=525&setp=2">2</a>', 1)
    p2 = p1.replace('<a href="/d147.html?kat=525&setp=2">2</a>', '').replace('<h4>1390</h4>', '<h4>1490</h4>')
    routes[DOBI + '/d147.html?kat=525&setp=1'] = (200, p1.encode('cp1252'))
    routes[DOBI + '/d147.html?kat=525&setp=2'] = (200, p2.encode('cp1252'))
    note, out, session = collect(ah.collect_dobiaschofsky, routes)
    assert session.urls()[-2:] == [DOBI + '/d147.html?kat=525&setp=1', DOBI + '/d147.html?kat=525&setp=2']
    assert len(session.calls) == 4 and note is None                  # robots、首页、两页；第 2 页没有下一页就停
    ids = {r['item_id'] for r in out}
    assert 'ah-dobiaschofsky-142-1490' in ids and 'ah-dobiaschofsky-142-1390' in ids


def test_dobi_missing_sale_number_is_parse_failure():
    routes = dobi_routes(in_window=True)
    kat = routes[DOBI + '/d147.html?kat=525&setp=1'][1].decode('cp1252')
    kat = kat.replace('<h2>A-142', '<h2>Herbst').replace('-A142-', '-X142-')
    routes[DOBI + '/d147.html?kat=525&setp=1'] = (200, kat.encode('cp1252'))
    with pytest.raises(ValueError, match=ah.PARSE_FAILED):
        collect(ah.collect_dobiaschofsky, routes)
