"""故事 10：发布快照、隐藏、图片与客人首页（TestClient，不连任何外部主机）。

覆盖 I/O 矩阵的服务器一侧：首次发布、缺图拒收、隐藏即时、隐藏晚于快照、旧快照、未登录、筛选、续载、白名单。
"""
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid

import pytest
from fastapi.testclient import TestClient

import app as server
import snapshot

TOKEN = 'test-admin-token-not-real'
AUTH = {'Authorization': 'Bearer ' + TOKEN}
SAME_ORIGIN = {'Origin': 'http://testserver'}
FORMS = [('FN-001', '杯碟'), ('FN-044', '花瓶'), ('FN-009', '盘')]


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def db(tmp_path):
    return tmp_path / 'server.db'


@pytest.fixture
def client(db, clock):
    return TestClient(server.create_app(db_path=db, admin_token=TOKEN, clock=clock, cookie_secure=False,
                                        public_url='https://curio.test'),
                      follow_redirects=False, headers=SAME_ORIGIN)


def jpeg(seed):
    data = b'\xff\xd8\xff\xe0' + seed.encode() + os.urandom(8)
    return hashlib.sha256(data).hexdigest()[:16] + '.jpg', data


def put_image(client, seed):
    name, data = jpeg(seed)
    r = client.put('/admin/images/' + name, content=data, headers=dict(AUTH, **{'Content-Type': 'image/jpeg'}))
    assert r.status_code == 200, r.text
    return name


def make_item(n, images, price=None, form=None, brand=None, pattern=None):
    code = 'CO-%04d' % n
    form_id, form_label = form or FORMS[n % len(FORMS)]
    it = {'code': code, 'std_name': f'Meissen梅森 {form_label} {n}', 'title': f'Meissen梅森 {form_label} {n}',
          'brand': brand or ('Meissen' if n % 3 else 'KPM Berlin'), 'form_id': form_id, 'form': form_label,
          'price_eur': price if price is not None else 100 + n * 37, 'images': [], 'seller_text': 'Sehr guter Zustand.',
          'seller_lang': 'de'}
    it['price_band'] = ('lt200' if it['price_eur'] < 200 else '200-500' if it['price_eur'] < 500
                        else '500-1000' if it['price_eur'] < 1000 else 'gte1000')
    if pattern:
        it['pattern_id'], it['pattern'] = pattern
    for name, thumb in images:
        it['images'].append({'name': name, 'w': 1600, 'h': 1200, 'thumb': thumb, 'tw': 600, 'th': 450,
                             'alt': f'{it["title"]} · 第 1 张 / 共 1 张'})
    return it


def build(publish_no, items, watermark=None, rank=None, tombstones=()):
    pool = [it['code'] for it in items]
    return {'schema_version': 1, 'publish_no': publish_no,
            'hide_watermark': publish_no if watermark is None else watermark,
            'rank': rank or pool[:12], 'pool': pool, 'items': items, 'tombstones': list(tombstones)}


def send(client, kind, payload):
    env = {'schema_version': 1, 'event_id': str(uuid.uuid4()), 'kind': kind,
           'created_at': '2026-11-03T08:00:00.000Z', 'payload': payload}
    return client.post('/admin/events', json=env, headers=AUTH)


def publish(client, publish_no, n_items=30, upload=True, **kw):
    items = []
    for n in range(1, n_items + 1):
        if upload:
            imgs = [(put_image(client, f'big{n}-{publish_no}'), put_image(client, f'thumb{n}-{publish_no}'))]
        else:       # 从未上传的图
            imgs = [(jpeg(f'big{n}')[0], jpeg(f'th{n}')[0])]
        items.append(make_item(n, imgs, **kw))
    r = send(client, 'snapshot', build(publish_no, items))
    return r, items


def hide(client, code, seq, action='hide', reason='sold'):
    payload = {'schema_version': 1, 'code': code, 'hide_seq': seq, 'action': action, 'at': '2026-11-03T09:00:00.000Z'}
    if action == 'hide':
        payload['reason'] = reason
    return send(client, 'hide', payload)


def sign_in(client):
    env = {'schema_version': 1, 'event_id': str(uuid.uuid4()), 'kind': 'set_invite_code',
           'created_at': '2026-11-03T08:00:00.000Z',
           'payload': {'schema_version': 1, 'command_id': str(uuid.uuid4()), 'target_seq': 1, 'code': 'MOMO2026'}}
    assert client.post('/admin/events', json=env, headers=AUTH).status_code == 200
    r = client.post('/register', data={'invite': 'MOMO2026', 'username': 'wangjie', 'password': 'meissen1710',
                                       'nickname': '王姐', 'email': '', 'consent': 'yes', 'next': '/'})
    assert r.status_code == 303


def codes_in(html, cls='card'):
    return re.findall(r'<li class="%s"><a class="[a-z-]+" href="/item/(CO-\d+)"' % cls, html)


def pool_codes(html):
    return codes_in(html, 'card')


def rank_codes(html):
    return codes_in(html, 'rk')


# ---------- 图片 ----------

def test_image_upload_check_and_ack(client, db):
    name, data = jpeg('a')
    r = client.post('/admin/images/check', json={'schema_version': 1, 'names': [name]}, headers=AUTH)
    assert r.status_code == 200 and r.json() == {'schema_version': 1, 'missing': [name]}
    r = client.put('/admin/images/' + name, content=data, headers=AUTH)
    assert r.status_code == 200 and r.json() == {'schema_version': 1, 'name': name, 'bytes': len(data)}
    assert (snapshot.image_dir(db) / name).read_bytes() == data
    again = client.put('/admin/images/' + name, content=data, headers=AUTH)
    assert again.status_code == 200 and again.json() == r.json()
    r = client.post('/admin/images/check', json={'schema_version': 1, 'names': [name]}, headers=AUTH)
    assert r.json()['missing'] == []


@pytest.mark.parametrize('name,data,status', [
    ('0123456789abcdef.jpg', b'\xff\xd8\xff' + b'x', 400),       # 内容与文件名不符
    (None, b'GIF89a-not-jpeg', 400),                              # 不是 JPEG
    ('ABCDEF0123456789.jpg', b'\xff\xd8\xff', 400),               # 文件名格式不对
    ('../etc.jpg', b'\xff\xd8\xff', 405),                         # 路径被规范成 /admin/etc.jpg：没有这个 PUT 路由
])
def test_image_upload_rejects(client, db, name, data, status):
    name = name or hashlib.sha256(data).hexdigest()[:16] + '.jpg'
    assert client.put('/admin/images/' + name, content=data, headers=AUTH).status_code == status
    assert not snapshot.image_dir(db).exists() or not any(snapshot.image_dir(db).iterdir())


def test_image_endpoints_need_admin_token(client):
    name, data = jpeg('b')
    assert client.put('/admin/images/' + name, content=data).status_code == 401
    assert client.post('/admin/images/check', json={'schema_version': 1, 'names': []}).status_code == 401
    bad = client.post('/admin/images/check', json={'schema_version': 1, 'names': ['x.png']}, headers=AUTH)
    assert bad.status_code == 400


# ---------- 快照与隐藏 ----------

def test_first_publish_shows_rank_and_pool(client):
    r, items = publish(client, 5)
    assert r.status_code == 200 and r.json()['applied'] is True and r.json()['current_publish_no'] == 5
    sign_in(client)
    html = client.get('/').text
    assert rank_codes(html) == [it['code'] for it in items[:12]]
    assert 'aria-label="MyR12，12 件"' in html and '全部藏品 <span class="mono cnt">30</span>' in html
    assert pool_codes(html) == [it['code'] for it in items[:20]]          # 首批 20
    assert 'aria-label="第 1 名，Meissen梅森' in html
    assert ' 更新</span>' in html and '墨墨正在准备' not in html
    img = re.search(r'src="/img/([0-9a-f]{16}\.jpg)"', html)[1]
    got = client.get('/img/' + img)
    assert got.status_code == 200 and got.headers['content-type'] == 'image/jpeg'
    assert 'immutable' in got.headers['cache-control'] and 'private' in got.headers['cache-control']


def test_missing_image_rejected_and_old_snapshot_kept(client, db):
    publish(client, 1, n_items=12)
    r, _ = publish(client, 2, n_items=12, upload=False)
    assert r.status_code == 400 and '尚未上传' in r.json()['detail']
    with sqlite3.connect(db) as c:
        assert c.execute('SELECT max(publish_no) FROM snapshots').fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM events WHERE kind='snapshot'").fetchone()[0] == 1


def test_old_snapshot_returns_current_number_not_error(client):
    publish(client, 7, n_items=12)
    r, _ = publish(client, 7, n_items=12)
    assert r.status_code == 200 and r.json()['applied'] is False and r.json()['current_publish_no'] == 7
    r, _ = publish(client, 3, n_items=12)
    assert r.status_code == 200 and r.json()['applied'] is False and r.json()['current_publish_no'] == 7


def test_hide_takes_effect_immediately_and_unhide_restores(client):
    _, items = publish(client, 10, n_items=14)
    sign_in(client)
    first = items[0]['code']
    r = hide(client, first, 11)
    assert r.status_code == 200 and r.json()['applied'] is True
    html = client.get('/').text
    assert first not in pool_codes(html) and first not in rank_codes(html)
    assert 'aria-label="MyR12，11 件"' in html
    page = client.get('/item/' + first)
    assert page.status_code == 200 and '此件已售出。' in page.text
    api = client.get('/api/pool').json()
    assert first not in api['html']
    # 旧序号的取消隐藏不生效；新序号的才生效。
    assert hide(client, first, 11, 'unhide').json()['applied'] is False
    assert hide(client, first, 12, 'unhide').json()['applied'] is True
    assert first in pool_codes(client.get('/').text)
    assert '卖家原价' in client.get('/item/' + first).text


def test_hide_later_than_snapshot_survives_replacement(client):
    """生成快照（水位 20）后、提交前又隐藏一件（hide_seq 21）：隐藏先到，新快照替换后仍隐藏。"""
    _, items = publish(client, 15, n_items=14)
    sign_in(client)
    late = items[13]['code']
    early = items[12]['code']
    assert hide(client, early, 16, reason='removed').json()['applied'] is True
    assert hide(client, late, 21).json()['applied'] is True
    r, _ = publish(client, 20, n_items=14)
    assert r.json()['applied'] is True
    html = client.get('/').text
    codes = pool_codes(html)
    assert late not in codes            # 21 > 水位 20：保留
    assert early in codes               # 16 ≤ 水位 20：由新快照决定（新快照里有它）
    assert hide(client, early, 19).json()['applied'] is False      # 水位以下的迟到隐藏忽略


def test_tombstone_and_unknown_item_pages(client):
    items = [make_item(n, [(put_image(client, f'b{n}'), put_image(client, f't{n}'))]) for n in range(1, 13)]
    tomb = [{'code': 'CO-0099', 'status': 'removed', 'at': '2026-11-01T08:00:00.000Z'}]
    assert send(client, 'snapshot', build(3, items, tombstones=tomb)).status_code == 200
    sign_in(client)
    assert '此件已移出。' in client.get('/item/CO-0099').text
    r = client.get('/item/CO-0500')
    assert r.status_code == 404 and '此件已不在展出' in r.text
    ok = client.get('/item/CO-0001')
    assert ok.status_code == 200 and '<title>Meissen梅森' in ok.text and '货号 CO-0001' in ok.text


@pytest.mark.parametrize('mutate,detail', [
    (lambda p: p.update(rank=p['rank'][:11]), '不符合要求'),
    (lambda p: p['items'].append(dict(p['items'][0])), '重复'),
    (lambda p: p.update(rank=p['rank'][:11] + ['CO-9999']), '不在商品池'),
    (lambda p: p['items'][0].update(url='https://www.ebay.de/itm/1'), '不符合要求'),
    (lambda p: p['items'][0].update(score=90), '不符合要求'),
    (lambda p: p.update(tombstones=[{'code': p['pool'][0], 'status': 'sold', 'at': '2026-11-01T08:00:00.000Z'}]),
     '重叠'),
    (lambda p: p.update(publish_no=0), '超出范围'),
])
def test_bad_snapshot_rejected(client, mutate, detail):
    items = [make_item(n, []) for n in range(1, 13)]
    payload = build(1, items)
    mutate(payload)
    r = send(client, 'snapshot', payload)
    assert r.status_code == 400 and detail in r.json()['detail']


# ---------- 访问边界 ----------

def test_unauthenticated_gets_no_data(client):
    _, items = publish(client, 1, n_items=12)
    name = items[0]['images'][0]['thumb']
    assert client.get('/api/pool').status_code == 401
    assert client.get('/img/' + name).status_code == 401
    page = client.get('/')
    assert page.status_code == 200 and 'action="/register"' in page.text
    assert 'CO-0' not in page.text and '/img/' not in page.text


def test_disabled_account_gets_no_data(client, db):
    publish(client, 1, n_items=12)
    sign_in(client)
    with sqlite3.connect(db) as c:
        c.execute("UPDATE accounts SET status='disabled'")
    assert client.get('/api/pool').status_code == 403
    assert 'CO-0' not in client.get('/').text


def test_no_origin_platform_urls_anywhere(client):
    publish(client, 1, n_items=30)
    sign_in(client)
    html = client.get('/').text
    api = client.get('/api/pool?offset=20').text
    for text in (html, api):
        assert not re.search(r'ebay|etsy|kleinanzeigen|https?://', text, re.I)
    assert 'style=' not in html and '<script>' not in html


# ---------- 筛选与续载 ----------

def test_filter_by_form_updates_summary_and_list(client):
    _, items = publish(client, 1, n_items=30)
    sign_in(client)
    html = client.get('/').text
    assert 'href="#sheet-form"' in html and 'role="radiogroup"' in html
    cups = [it['code'] for it in items if it['form'] == '杯碟']
    r = client.get('/?form=FN-001')
    assert pool_codes(r.text) == cups[:20]
    assert '已选杯碟，显示 <span class="mono">%d</span> 件' % len(cups) in r.text
    assert 'role="radio" aria-checked="true" href="/?form=FN-001#pool"' in r.text
    assert '器型：杯碟' in r.text
    # 与品牌、价格区间组合
    r = client.get('/?form=FN-001&brand=KPM+Berlin&band=gte1000')
    expect = [it['code'] for it in items if it['form'] == '杯碟' and it['brand'] == 'KPM Berlin'
              and it['price_band'] == 'gte1000']
    assert pool_codes(r.text) == expect
    # 不认识的取值忽略
    assert len(pool_codes(client.get('/?form=FN-777').text)) == 20


def test_filter_without_results_shows_empty_state(client):
    items = [make_item(n, [(put_image(client, f'b{n}'), put_image(client, f't{n}'))], price=150) for n in range(1, 13)]
    send(client, 'snapshot', build(1, items))
    sign_in(client)
    r = client.get('/?band=gte1000')
    assert '此条件下暂无藏品' in r.text and '清除全部筛选' in r.text and '可放宽价格区间后再看。' in r.text
    assert pool_codes(r.text) == []
    # 价格区间四档都列出；器型只列商品池里有的。
    band_sheet = r.text[r.text.index('id="sheet-band"'):r.text.index('id="sheet-pattern"')]
    assert all(label in band_sheet for label in ('€200 以下', '€200–500', '€500–1000', '€1000 以上'))
    assert '花瓶' in r.text and '茶壶' not in r.text


def test_brand_tabs_from_pool_with_counts(client):
    _, items = publish(client, 1, n_items=30)
    sign_in(client)
    html = client.get('/').text
    meissen = sum(it['brand'] == 'Meissen' for it in items)
    assert f'aria-label="Meissen，{meissen} 件"' in html and f'aria-label="KPM Berlin，{30 - meissen} 件"' in html
    assert html.index('Meissen，') < html.index('KPM Berlin，')
    r = client.get('/?brand=Meissen')
    assert 'aria-pressed="true" aria-label="Meissen' in r.text
    assert 'href="/#pool" data-filter><span class="idx mono" aria-hidden="true">01' in r.text    # 再点一次取消


def test_load_more_in_batches_of_20_keeps_order(client):
    _, items = publish(client, 1, n_items=45)
    sign_in(client)
    html = client.get('/').text
    assert pool_codes(html) == [it['code'] for it in items[:20]]
    assert 'data-next="20"' in html and 'href="/?n=40#pool">加载更多' in html
    a = client.get('/api/pool?offset=20').json()
    b = client.get('/api/pool?offset=40').json()
    assert (a['count'], a['shown'], a['total'], a['next_offset']) == (20, 40, 45, 40)
    assert (b['count'], b['shown'], b['next_offset']) == (5, 45, None)
    got = pool_codes(html) + pool_codes(a['html']) + pool_codes(b['html'])
    assert got == [it['code'] for it in items]
    # 不跑 JS 的"加载更多"
    assert pool_codes(client.get('/?n=40').text) == [it['code'] for it in items[:40]]


def test_load_more_respects_filter(client):
    _, items = publish(client, 1, n_items=45)
    sign_in(client)
    meissen = [it['code'] for it in items if it['brand'] == 'Meissen']
    html = client.get('/?brand=Meissen').text
    assert 'data-query="brand=Meissen"' in html
    rest = client.get('/api/pool?brand=Meissen&offset=20').json()
    assert pool_codes(html) + pool_codes(rest['html']) == meissen


def test_never_published_shows_preparing(client):
    sign_in(client)
    html = client.get('/').text
    assert '墨墨正在准备今天的藏品，请稍后再来。' in html and 'class="wf"' not in html


# ---------- 旧图清理 ----------

def age(path, days):
    t = time.time() - days * 86400
    os.utime(path, (t, t))


def test_old_images_kept_until_two_later_publishes(client, db):
    _, first = publish(client, 1, n_items=12)
    old = first[0]['images'][0]['name']
    path = snapshot.image_dir(db) / old
    age(path, 3)
    publish(client, 2, n_items=12)
    publish(client, 3, n_items=12)
    assert path.exists()                 # 其后两次发布之后仍在
    publish(client, 4, n_items=12)
    assert not path.exists()             # 第三次之后清理
    with sqlite3.connect(db) as c:
        assert [r[0] for r in c.execute('SELECT publish_no FROM snapshots ORDER BY 1')] == [2, 3, 4]


def test_snapshot_event_stored_as_digest_only(client, db):
    publish(client, 1, n_items=12)
    with sqlite3.connect(db) as c:
        payload = json.loads(c.execute("SELECT payload FROM events WHERE kind='snapshot'").fetchone()[0])
    assert set(payload) == {'schema_version', 'publish_no', 'sha256'}


def test_fresh_or_reconfirmed_unreferenced_images_survive_cleanup(client, db):
    publish(client, 1, n_items=12)
    fresh = put_image(client, 'fresh-unreferenced')               # 刚传、还没被快照引用
    old = put_image(client, 'old-unreferenced')
    age(snapshot.image_dir(db) / old, 3)
    name, data = jpeg('reconfirmed')
    client.put('/admin/images/' + name, content=data, headers=AUTH)
    age(snapshot.image_dir(db) / name, 3)
    assert client.put('/admin/images/' + name, content=data, headers=AUTH).status_code == 200   # 再传一次：算新的
    for no in (2, 3, 4):
        publish(client, no, n_items=12)
    d = snapshot.image_dir(db)
    assert (d / fresh).exists() and (d / name).exists() and not (d / old).exists()


def test_duplicate_event_replies_like_the_first(client):
    items = [make_item(n, [(put_image(client, f'b{n}'), put_image(client, f't{n}'))]) for n in range(1, 13)]
    env = {'schema_version': 1, 'event_id': str(uuid.uuid4()), 'kind': 'snapshot',
           'created_at': '2026-11-03T08:00:00.000Z', 'payload': build(4, items)}
    first = client.post('/admin/events', json=env, headers=AUTH).json()
    assert client.post('/admin/events', json=env, headers=AUTH).json() == first
    assert first['applied'] is True and first['current_publish_no'] == 4
    henv = {'schema_version': 1, 'event_id': str(uuid.uuid4()), 'kind': 'hide', 'created_at': '2026-11-03T08:00:00.000Z',
            'payload': {'schema_version': 1, 'code': 'CO-0001', 'hide_seq': 9, 'action': 'hide', 'reason': 'sold',
                        'at': '2026-11-03T09:00:00.000Z'}}
    a = client.post('/admin/events', json=henv, headers=AUTH).json()
    b = client.post('/admin/events', json=henv, headers=AUTH).json()
    assert a == b and a['applied'] is True


def test_load_more_stops_at_max_shown(client, monkeypatch):
    import guest
    monkeypatch.setattr(guest, 'MAX_SHOWN', 30)
    _, items = publish(client, 1, n_items=45)
    sign_in(client)
    html = client.get('/?n=30').text
    assert len(pool_codes(html)) == 30 and 'data-next=""' in html and '加载更多' not in html
    last = client.get('/api/pool?offset=20').json()
    assert last['count'] == 10 and last['next_offset'] is None
    assert client.get('/api/pool?offset=30').json()['count'] == 0
