"""故事 10：发布快照与即时隐藏（AD-5、AD-6、AD-13、AD-22）。

图片用系统 sips 真转码（源图在测试里现做的 PNG）；远程图片地址只经注入的假下载函数；
同步只连本机 127.0.0.1 上临时启动的真服务器（server/.venv），任何外部主机请求直接失败。
"""
import json
import os
import re
import shutil
import socket
import sqlite3
import struct
import subprocess
import time
import zlib
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
import requests

from curio import publish, sync, triage
from curio.config import load
from curio.store import Store

RANK = publish.RANK_SIZE

ROOT = Path(__file__).resolve().parents[1]
SERVER_PY = ROOT / 'server' / '.venv' / 'bin' / 'python'
TOKEN = 'test-admin-token-SENTINEL'
_real_request = requests.sessions.Session.request
pytestmark = pytest.mark.skipif(shutil.which('sips') is None, reason='需要 macOS 自带 sips')


@pytest.fixture(autouse=True)
def only_localhost():
    def guard(self, method, url, *a, **kw):
        assert urlsplit(url).hostname == '127.0.0.1', '测试不得连接外部主机'
        return _real_request(self, method, url, *a, **kw)
    with patch('requests.sessions.Session.request', guard), patch('curio.sync._notify') as note:
        yield note


def png(w, h, rgb=(40, 80, 160)):
    """纯标准库生成的 RGB PNG（sips 能读）。"""
    raw = b''.join(b'\x00' + bytes(rgb) * w for _ in range(h))

    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw, 9)) + chunk(b'IEND', b''))


class Fetch:
    """假下载：按地址返回现做的 PNG，并记下请求过的地址。"""

    def __init__(self):
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        n = int(re.search(r'(\d+)', url).group(1)) if re.search(r'(\d+)', url) else 1
        return png(2000, 1500, (n % 256, 90, 150))


@pytest.fixture
def cfg():
    return dict(load(), keywords=['Meissen'])


@pytest.fixture
def root(tmp_path):
    return tmp_path


@pytest.fixture
def db(tmp_path):
    return Store(tmp_path / 'data' / 'curio.db')


@pytest.fixture
def fetch():
    return Fetch()


FORMS = [('FN-001', '杯碟'), ('FN-044', '花瓶'), ('FN-009', '盘')]


def add(db, cfg, n, platform='ebay', keep=True, image=None, price=None, description=None, title=None):
    iid = ('ka:%d' % n) if platform == 'kleinanzeigen' else 'it%03d' % n
    db.ingest(dict(item_id=iid, platform=platform, title=title or f'Meissen Tasse Nr {n}', price=price or 100 + n * 37,
                   currency='EUR', buying='FIXED_PRICE', url=f'https://www.ebay.de/itm/{100000000000 + n}',
                   image=image if image is not None else 'https://i.ebayimg.com/images/g/shared.png',
                   description=description if description is not None else
                   'Sehr guter Zustand, keine Chips. Mehr in meinem Shop: https://www.ebay.de/str/porzellanfritz'), cfg)
    form_id, form = FORMS[n % 3]
    brand = 'Meissen' if n % 3 else 'KPM Berlin'
    with db.connect() as c:
        c.execute('''INSERT OR REPLACE INTO std_names(item_id,std_name,std_key,status,brand,pattern_id,form_id,function_id,
            era,seller_grade,segments,kb_status,kb_fingerprint,generated_at,overpaint)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)''',
                  (iid, f'{brand} {form}', f'{brand}|-|-|{form_id}', 'named', brand, 'PT-1' if n % 2 else None, None,
                   form_id, None, None,
                   json.dumps({'era': None, 'brand': brand, 'pattern': '蓝洋葱' if n % 2 else None, 'form': None,
                               'function': form}, ensure_ascii=False), 'ok', 'fp', '2026-11-03T00:00:00+00:00'))
        if keep:
            triage.keep(c, iid)
        return iid, triage.code(c, iid)


def pool(db, cfg, n=12, **kw):
    out = [add(db, cfg, i, **kw) for i in range(1, n + 1)]
    return [iid for iid, _ in out], [code for _, code in out]


def set_rank(db, codes):
    with db.connect() as c:
        return publish.set_rank(c, codes)


def outbox(db, kind=None):
    with db.connect() as c:
        rows = [dict(r) for r in c.execute('SELECT * FROM outbox ORDER BY seq')]
    return [dict(r, payload=json.loads(r['payload'])) for r in rows if kind is None or r['kind'] == kind]


def do_publish(db, root, fetch):
    return publish.publish(db, root, fetch=fetch)


# ---------- 榜与入选 ----------

def test_rank_needs_full_size(db, cfg, root, fetch):
    _, codes = pool(db, cfg, RANK - 1)
    set_rank(db, codes)
    with pytest.raises(publish.PublishError) as e:
        do_publish(db, root, fetch)
    assert f'榜需满 {RANK} 件才能发布' in str(e.value)
    assert outbox(db) == [] and fetch.urls == []


def test_set_rank_rejects_ineligible(db, cfg):
    _, codes = pool(db, cfg, RANK + 1)
    ka, ka_code = add(db, cfg, 50, platform='kleinanzeigen')
    with pytest.raises(publish.PublishError, match='Kleinanzeigen'):
        set_rank(db, codes[:RANK - 1] + [ka_code])
    with pytest.raises(publish.PublishError, match=f'最多 {RANK} 件'):
        set_rank(db, codes)
    with pytest.raises(publish.PublishError, match='重复'):
        set_rank(db, codes[:RANK - 1] + codes[:1])
    with pytest.raises(publish.PublishError, match='不存在'):
        set_rank(db, ['CO-9999'])
    iid = 'it%03d' % 3
    with db.connect() as c:
        triage.remove(c, iid)
    with pytest.raises(publish.PublishError, match='不在商品池'):
        set_rank(db, [codes[2]])
    db.mark_gone('it%03d' % 4)
    with pytest.raises(publish.PublishError, match='下架'):
        set_rank(db, [codes[3]])
    assert set_rank(db, [c.lower() for c in codes[4:6]]) == codes[4:6]


def test_first_publish_snapshot_is_whitelisted(db, cfg, root, fetch):
    iids, codes = pool(db, cfg, 30)
    ka, ka_code = add(db, cfg, 60, platform='kleinanzeigen')
    auto, _ = add(db, cfg, 61, keep=False, title='Meissen defekt Tasse')
    removed, removed_code = add(db, cfg, 62)
    with db.connect() as c:
        triage.remove(c, removed)
    set_rank(db, codes[:RANK])
    result = do_publish(db, root, fetch)
    assert result['rank'] == RANK and result['pool'] == 30 and result['image_failures'] == 0
    [row] = outbox(db, 'snapshot')
    snap = row['payload']
    assert snap['publish_no'] == result['publish_no'] == snap['hide_watermark']
    assert snap['rank'] == codes[:RANK]
    assert snap['pool'] == list(reversed(codes))            # 留下时间新到旧（同一秒按货号新到旧）
    assert ka_code not in snap['pool'] and removed_code not in snap['pool'] and snap['tombstones'] == []
    allowed = {'code', 'std_name', 'title', 'title_lang', 'brand', 'form_id', 'form', 'pattern_id', 'pattern',
               'price_band', 'price_eur', 'fee_tier', 'fee_amount', 'fee_label', 'images', 'seller_text', 'seller_lang', 'comment'}
    for it in snap['items']:
        assert set(it) <= allowed
    text = json.dumps(snap, ensure_ascii=False)
    for bad in ('http', 'ebay', 'ebayimg', 'it001', 'ka:', 'porzellanfritz', '1000000000', 'score', 'reason', 'GC0'):
        assert bad not in text.lower()
    first = next(it for it in snap['items'] if it['code'] == codes[0])
    assert first['seller_text'].startswith('Sehr guter Zustand, keine Chips.') and first['seller_lang'] == 'de'
    assert first['brand'] == 'Meissen' and first['title'] == first['std_name'] == 'Meissen 花瓶'
    assert (first['form_id'], first['form'], first['pattern']) == ('FN-044', '花瓶', '蓝洋葱')
    assert first['price_eur'] == 137 and first['price_band'] == 'lt200'
    [img] = first['images']
    assert (img['w'], img['h'], img['tw'], img['th']) == (1600, 1200, 600, 450)
    assert img['alt'] == 'Meissen 花瓶 · 第 1 张 / 共 1 张'
    folder = sync.image_dir(db)
    for name in (img['name'], img['thumb']):
        data = (folder / name).read_bytes()
        assert data[:3] == b'\xff\xd8\xff' and __import__('hashlib').sha256(data).hexdigest()[:16] + '.jpg' == name
    assert fetch.urls == ['https://i.ebayimg.com/images/g/shared.png']          # 同一张图只转一次
    # 再发布不重新转码
    do_publish(db, root, fetch)
    assert len(fetch.urls) == 1


def test_local_downloaded_images_used_and_alt_text_kept(db, cfg, root, fetch):
    iids, codes = pool(db, cfg, RANK)
    src = png(800, 1000)
    name = __import__('hashlib').sha256(src).hexdigest() + '.png'
    folder = root / 'data' / 'porcelain' / 'images'
    folder.mkdir(parents=True)
    (folder / name).write_bytes(src)
    with db.connect() as c:
        c.execute('CREATE TABLE IF NOT EXISTS product_snapshots(id INTEGER PRIMARY KEY, item_id TEXT, data TEXT)')
        c.execute('INSERT INTO product_snapshots(item_id,data) VALUES(?,?)',
                  (iids[0], json.dumps({'images': [{'file': f'data/porcelain/images/{name}'}]})))
        c.execute("INSERT INTO comments VALUES(?,?,?)", (iids[0], '钴蓝发色沉稳。', '2026-11-03'))
    db.save_alt(iids[0], 0, '口沿局部', root)
    set_rank(db, codes)
    do_publish(db, root, fetch)
    snap = outbox(db, 'snapshot')[0]['payload']
    it = next(i for i in snap['items'] if i['code'] == codes[0])
    [img] = it['images']
    assert img['alt'] == '口沿局部' and (img['w'], img['h']) == (800, 1000) and (img['tw'], img['th']) == (480, 600)
    assert it['comment'] == '钴蓝发色沉稳。'


def test_clean_seller_text():
    raw = ('Schöne Meissen Tasse, keine Chips.\nArtikelnummer: 123456789012\nVerkäufer: porzellan_fritz\n'
           'Siehe auch https://www.etsy.com/shop/MeissenWorld und www.example.de/x, eBay-Kleinanzeigen-Anzeige.\n'
           'Fragen an meissen.fan@example.com oder @meissenfan. Mehr in meinem Shop.\nItem number 3141592653')
    out = publish.clean_seller_text(raw)
    assert out.startswith('Schöne Meissen Tasse, keine Chips.')
    for bad in ('123456789012', 'porzellan_fritz', 'http', 'www.', 'etsy', 'MeissenWorld', 'Kleinanzeigen', 'eBay',
                '@', 'meissenfan', 'meinem Shop', '3141592653'):
        assert bad.lower() not in out.lower()
    assert publish.clean_seller_text('x' * 6000) == 'x' * 5000


@pytest.mark.parametrize('price,band', [(0, 'lt200'), (199.99, 'lt200'), (200, '200-500'), (499.5, '200-500'),
                                        (500, '500-1000'), (999.99, '500-1000'), (1000, 'gte1000'), (8000, 'gte1000')])
def test_price_bands_left_closed(price, band):
    assert publish.price_band(price) == band


def test_kleinanzeigen_never_in_snapshot(db, cfg, root, fetch):
    _, codes = pool(db, cfg, RANK)
    _, ka_code = add(db, cfg, 70, platform='kleinanzeigen')
    set_rank(db, codes)
    do_publish(db, root, fetch)
    snap = outbox(db, 'snapshot')[0]['payload']
    assert ka_code not in json.dumps(snap)


# ---------- 发布锁与序号 ----------

def test_concurrent_publish_is_refused(db, cfg, root, fetch):
    _, codes = pool(db, cfg, RANK)
    set_rank(db, codes)
    publish._lock(db, 'other-owner', time.time)
    with pytest.raises(publish.PublishBusy) as e:
        do_publish(db, root, fetch)
    assert '另一个发布正在进行' in str(e.value) and outbox(db) == []
    publish._unlock(db, 'other-owner')
    assert do_publish(db, root, fetch)['rank'] == RANK
    with db.connect() as c:
        assert c.execute("SELECT v FROM kv WHERE k='publish_lock'").fetchone() is None     # 发布后释放


def test_expired_lock_does_not_block(db, cfg, root, fetch):
    _, codes = pool(db, cfg, RANK)
    set_rank(db, codes)
    publish._lock(db, 'crashed', lambda: time.time() - publish.LOCK_SECONDS - 5)
    assert do_publish(db, root, fetch)['rank'] == RANK


def test_publish_and_hide_share_one_counter(db, cfg, root, fetch):
    iids, codes = pool(db, cfg, RANK)
    set_rank(db, codes)
    first = do_publish(db, root, fetch)['publish_no']
    db.mark_gone(iids[0])
    with db.connect() as c:
        triage.remove(c, iids[1])
    hides = [r['payload'] for r in outbox(db, 'hide')]
    assert [h['hide_seq'] for h in hides] == [first + 1, first + 2]
    assert [(h['code'], h['action'], h['reason']) for h in hides] == [(codes[0], 'hide', 'sold'),
                                                                    (codes[1], 'hide', 'removed')]
    # 榜里两件已离开（下架、去掉）：补位前不能再发布；隐藏不受"满 RANK 件"限制。
    with pytest.raises(publish.PublishError, match=f'榜里第 1 名 {codes[0]} 已不能发布.*请换一件'):
        do_publish(db, root, fetch)
    assert len(outbox(db, 'snapshot')) == 1


# ---------- 即时隐藏 ----------

def test_hide_and_unhide_follow_state_changes(db, cfg, root, fetch):
    iids, codes = pool(db, cfg, RANK + 1)
    set_rank(db, codes[:RANK])
    do_publish(db, root, fetch)
    a, b, c_ = iids[0], iids[1], iids[2]
    db.mark_gone(a)
    db.mark_gone(a)                                   # 已下架不重复推送
    with db.connect() as c:
        triage.remove(c, b)
        triage.undo(c)                                # 撤销去掉 → 取消隐藏
        triage.out_of_pool(c, c_)
        triage.back_to_pool(c, c_)
    db.ingest(dict(item_id=a, platform='ebay', title='Meissen Tasse Nr 1', price=137, currency='EUR',
                   buying='FIXED_PRICE', url='https://www.ebay.de/itm/1', image=''), cfg)   # 重新在售
    got = [(h['payload']['code'], h['payload']['action'], h['payload'].get('reason')) for h in outbox(db, 'hide')]
    assert got == [(codes[0], 'hide', 'sold'), (codes[1], 'hide', 'removed'), (codes[1], 'unhide', None),
                   (codes[2], 'hide', 'removed'), (codes[2], 'unhide', None), (codes[0], 'unhide', None)]
    seqs = [h['payload']['hide_seq'] for h in outbox(db, 'hide')]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


def test_unpublished_item_changes_do_not_enqueue(db, cfg, root, fetch):
    iids, codes = pool(db, cfg, RANK + 2)
    with db.connect() as c:
        triage.remove(c, iids[0])
    db.mark_gone(iids[1])
    assert outbox(db) == []
    set_rank(db, codes[2:])
    do_publish(db, root, fetch)
    with db.connect() as c:
        triage.undo(c)                     # 撤销的是快照之外的货：不推送
    assert outbox(db, 'hide') == []


def test_hide_is_in_same_transaction_as_state_change(db, cfg, root, fetch):
    iids, codes = pool(db, cfg, RANK)
    set_rank(db, codes)
    do_publish(db, root, fetch)
    with pytest.raises(RuntimeError):
        with db.connect() as c:
            triage.remove(c, iids[0])
            raise RuntimeError('之后的写入失败')
    assert outbox(db, 'hide') == []
    with db.connect() as c:
        assert triage.state(c, iids[0]) == 'kept'


def test_tombstones_for_left_items(db, cfg, root, fetch):
    iids, codes = pool(db, cfg, RANK + 2)
    set_rank(db, codes[2:])
    do_publish(db, root, fetch)
    db.mark_gone(iids[0])
    with db.connect() as c:
        triage.remove(c, iids[1])
    do_publish(db, root, fetch)
    snap = outbox(db, 'snapshot')[-1]['payload']
    assert {(t['code'], t['status']) for t in snap['tombstones']} == {(codes[0], 'sold'), (codes[1], 'removed')}
    assert codes[0] not in snap['pool'] and codes[1] not in snap['pool']
    with db.connect() as c:
        triage.undo(c)                                   # 去掉撤销：回到商品池，墓碑消失
    do_publish(db, root, fetch)
    snap = outbox(db, 'snapshot')[-1]['payload']
    assert codes[1] in snap['pool'] and [t['code'] for t in snap['tombstones']] == [codes[0]]


# ---------- 契约与校验器 ----------

def test_validator_supports_arrays():
    schema = {'type': 'array', 'minItems': 2, 'maxItems': 3, 'items': {'type': 'string', 'pattern': '^a'}}
    assert sync.validate(['a', 'ab'], schema) == []
    assert sync.validate(['a'], schema) and sync.validate(['a'] * 4, schema) and sync.validate(['a', 'b'], schema)
    with pytest.raises(sync.ContractError):
        sync.validate([], {'items': {'exclusiveMinimum': 1}})


def test_oversized_payload_refused(db):
    big = {'schema_version': 1, 'code': 'CO-0001', 'hide_seq': 1, 'action': 'hide', 'at': sync.utc_now()}
    with patch.object(sync, 'MAX_PAYLOAD', 10):
        with db.connect() as c:
            with pytest.raises(sync.ContractError, match='上限'):
                sync.enqueue(c, 'hide', big)


@pytest.mark.skipif(not SERVER_PY.exists(), reason='先在 server/ 下 uv sync')
def test_snapshot_and_hide_contract_agree_with_server(db, cfg, root, fetch):
    _, codes = pool(db, cfg, RANK)
    set_rank(db, codes)
    do_publish(db, root, fetch)
    snap = outbox(db, 'snapshot')[0]['payload']
    hide = {'schema_version': 1, 'code': 'CO-0001', 'hide_seq': 5, 'action': 'hide', 'reason': 'sold',
            'at': '2026-11-03T08:00:00.000Z'}
    cases = [('snapshot', snap), ('hide', hide),
             ('snapshot', dict(snap, rank=snap['rank'][:RANK - 1])),
             ('snapshot', dict(snap, items=[dict(snap['items'][0], url='https://x')] + snap['items'][1:])),
             ('snapshot', dict(snap, items=[dict(snap['items'][0], price_band='cheap')] + snap['items'][1:])),
             ('hide', dict(hide, action='delete')), ('hide', dict(hide, code='CO-1')),
             ('hide', {k: v for k, v in hide.items() if k != 'at'})]
    envs = [{'schema_version': 1, 'event_id': '0f8fad5b-d9cb-469f-a165-70867728950e', 'kind': k,
             'created_at': '2026-11-03T08:00:00.000Z', 'payload': p} for k, p in cases]
    probe = ('import json,sys\nimport app\nc=app.Contract(app.DEFAULT_CONTRACT)\nout=[]\n'
             'for e in json.load(sys.stdin):\n    try:c.check(e);out.append(True)\n'
             '    except app.BadPayload:out.append(False)\nprint(json.dumps(out))\n')
    r = subprocess.run([str(SERVER_PY), '-c', probe], cwd=ROOT / 'server', input=json.dumps(envs),
                       capture_output=True, text=True, timeout=60, check=True)
    mac = []
    for e in envs:
        try:
            sync.check_envelope(e)
            mac.append(True)
        except sync.ContractError:
            mac.append(False)
    assert mac == json.loads(r.stdout) == [True, True] + [False] * 6


# ---------- 运维命令 ----------

def test_publish_script_rank_and_publish(db, cfg, root, fetch):
    from scripts import publish as cli
    _, codes = pool(db, cfg, RANK)
    lines = []
    args = ['--db', str(db.path), '--root', str(root)]
    assert cli.main(args + ['rank'] + codes[:RANK - 1], out=lines.append, fetch=fetch) == 0
    assert cli.main(args + ['publish'], out=lines.append, fetch=fetch) == 1
    assert f'榜需满 {RANK} 件才能发布' in lines[-1]
    assert cli.main(args + ['rank'] + codes, out=lines.append, fetch=fetch) == 0
    assert cli.main(args + ['rank'], out=lines.append, fetch=fetch) == 0
    assert codes[0] in lines[-1]
    assert cli.main(args + ['publish'], out=lines.append, fetch=fetch) == 0
    assert '已入队发布' in lines[-1] and len(outbox(db, 'snapshot')) == 1
    assert cli.main(args + ['rank', 'CO-9999'], out=lines.append, fetch=fetch) == 1


# ---------- 端到端：本机真服务器 ----------

def _free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


@pytest.fixture
def live(tmp_path):
    if not SERVER_PY.exists():
        pytest.skip('先在 server/ 下 uv sync')
    port = _free_port()
    env = dict(os.environ, CURIO_ADMIN_TOKEN=TOKEN, CURIO_SERVER_DB=str(tmp_path / 'srv' / 'server.db'),
               CURIO_COOKIE_SECURE='0', CURIO_MAIL_DIR=str(tmp_path / 'mail'))
    proc = subprocess.Popen([str(SERVER_PY), 'app.py', '--port', str(port)], cwd=ROOT / 'server', env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f'http://127.0.0.1:{port}'
    try:
        for _ in range(100):
            try:
                if requests.get(url + '/healthz', timeout=1).status_code == 200:
                    break
            except requests.RequestException:
                time.sleep(0.1)
        else:
            pytest.fail('本机服务器未启动')
        (tmp_path / '.env').write_text(f'CURIO_SERVER_URL={url}\nCURIO_ADMIN_TOKEN={TOKEN}\n')
        yield url
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def guest(url, store, root):
    with store.connect() as c:
        sync.enqueue_invite(c, 'MOMO2026')
    assert sync.flush(store, root)['sync_status'] == 'ok'
    s = requests.Session()
    s.headers['Origin'] = url
    r = s.post(url + '/register', allow_redirects=False,
               data={'invite': 'MOMO2026', 'username': 'wangjie', 'password': 'meissen1710', 'nickname': '王姐',
                     'consent': 'yes', 'next': '/'})
    assert r.status_code == 303
    return s


def cards(html):
    return re.findall(r'<li class="card"><a class="card-a" href="/item/(CO-\d+)"', html)


def test_end_to_end_publish_hide_and_missing_image(db, cfg, root, fetch, live, only_localhost):
    iids, codes = pool(db, cfg, 30)
    set_rank(db, codes[:RANK])
    s = guest(live, db, root)
    assert 'action="/register"' in requests.get(live + '/').text and requests.get(live + '/api/pool').status_code == 401
    do_publish(db, root, fetch)
    assert sync.flush(db, root)['sync_status'] == 'ok'
    home = s.get(live + '/').text
    assert re.findall(r'<li class="rk"><a class="rk-a" href="/item/(CO-\d+)"', home) == codes[:RANK]
    assert cards(home) == list(reversed(codes))[:20]
    more = s.get(live + '/api/pool?offset=20').json()
    assert more['total'] == 30 and cards(more['html']) == list(reversed(codes))[20:]
    for text in (home, more['html']):
        assert not re.search(r'https?://|ebay|etsy|kleinanzeigen', text, re.I)
    img = re.search(r'src="(/img/[0-9a-f]{16}\.jpg)"', home)[1]
    assert s.get(live + img).headers['content-type'] == 'image/jpeg'
    assert requests.get(live + img).status_code == 401
    # 隐藏即时：下架后下一次 flush 客人页就不再出现，无需重新发布。
    db.mark_gone(iids[29])
    assert sync.flush(db, root)['sync_status'] == 'ok'
    assert codes[29] not in s.get(live + '/').text.split('全部藏品')[1]
    # 隐藏晚于快照：生成快照后、提交前又下架一件，新快照替换后该件仍隐藏。
    do_publish(db, root, fetch)
    db.mark_gone(iids[28])
    assert sync.flush(db, root)['sync_status'] == 'ok'
    page = s.get(live + '/').text
    assert codes[28] not in page and codes[29] not in page and len(cards(page)) == 20
    assert s.get(live + '/api/pool?offset=20').json()['total'] == 28
    # 缺图：本机图片被删，发件箱阻塞并通知，服务器上旧快照不变。
    shutil.rmtree(sync.image_dir(db))
    with db.connect() as c:
        c.execute('DELETE FROM publish_images')
    add(db, cfg, 99, image='https://i.ebayimg.com/images/g/new99.png')
    do_publish(db, root, fetch)
    for f in sync.image_dir(db).iterdir():
        f.unlink()
    before = s.get(live + '/api/pool?offset=0').json()['total']
    result = sync.flush(db, root)
    assert result['sync_status'] == 'blocked' and '本机缺少图片' in result['sync_error']
    only_localhost.assert_called()
    assert s.get(live + '/api/pool?offset=0').json()['total'] == before == 28


# ---------- 评审修正：送达确认、隐藏越过卡住的快照、分批查图、上传失败 ----------

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Mock:
    """本机假服务器：/admin/images/check、PUT /admin/images/<name>、/admin/events。行为可按需改。"""

    def __init__(self):
        self.images, self.checks, self.puts, self.events, self.bodies = set(), [], [], [], []
        self.snapshot_status = 200          # 快照的响应码（400 永久拒收、500 暂时失败）
        self.put_status = 200
        self.ack_bytes_delta = 0
        self.current = 0
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_PUT(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                name = self.path.rsplit('/', 1)[-1]
                outer.puts.append(name)
                if outer.put_status != 200:
                    return self.reply(outer.put_status, {'ok': False, 'detail': '暂时失败'})
                outer.images.add(name)
                self.reply(200, {'schema_version': 1, 'name': name, 'bytes': len(body) + outer.ack_bytes_delta})

            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                if self.path == '/admin/images/check':
                    names = json.loads(body)['names']
                    outer.checks.append(len(names))
                    return self.reply(200, {'schema_version': 1, 'missing': [n for n in names if n not in outer.images]})
                env = json.loads(body)
                outer.bodies.append(len(body))
                ack = {'ok': True, 'event_id': env['event_id'], 'received_at': '2026-11-03T08:00:00.000Z'}
                if env['kind'] == 'snapshot':
                    if outer.snapshot_status != 200:
                        return self.reply(outer.snapshot_status, {'ok': False, 'detail': '快照失败'})
                    no = env['payload']['publish_no']
                    ack['applied'] = no > outer.current
                    outer.current = max(outer.current, no)
                    ack['current_publish_no'] = outer.current
                outer.events.append((env['kind'], env['payload']))
                self.reply(200, ack)

        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), H)
        self.url = 'http://127.0.0.1:%d' % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def kinds(self):
        return [k for k, _ in self.events]


@pytest.fixture
def mock(root):
    m = Mock()
    (root / '.env').write_text(f'CURIO_SERVER_URL={m.url}\nCURIO_ADMIN_TOKEN={TOKEN}\n')
    yield m
    m.httpd.shutdown()
    m.httpd.server_close()


def statuses(db):
    with db.connect() as c:
        return dict(c.execute('SELECT publish_no,status FROM publishes'))


def published_codes(db):
    with db.connect() as c:
        return {r[0] for r in c.execute('SELECT code FROM published')}


def test_snapshot_promoted_only_on_server_ack(db, cfg, root, fetch, mock):
    _, codes = pool(db, cfg, RANK)
    set_rank(db, codes)
    no = do_publish(db, root, fetch)['publish_no']
    assert statuses(db) == {no: 'pending'}
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM publish_history').fetchone()[0] == 0
    assert sync.flush(db, root)['sync_status'] == 'ok'
    assert statuses(db) == {no: 'delivered'}
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM publish_history').fetchone()[0] == RANK


def test_drop_snapshot_and_republish_keeps_hiding_live_items(db, cfg, root, fetch, mock):
    from scripts import outbox as outbox_cli
    iids, codes = pool(db, cfg, RANK + 1)
    set_rank(db, codes[:RANK])
    first = do_publish(db, root, fetch)['publish_no']
    assert sync.flush(db, root)['sync_status'] == 'ok'
    with db.connect() as c:
        triage.remove(c, iids[0])                              # 已上线的货：推隐藏
    new_iid, new_code = add(db, cfg, 40)                       # 只在第二份快照里的货
    set_rank(db, codes[1:RANK + 1])
    second = do_publish(db, root, fetch)['publish_no']
    mock.snapshot_status = 400
    assert sync.flush(db, root)['sync_status'] == 'blocked'
    assert ('hide', ) == tuple(k for k in mock.kinds()[1:])    # 隐藏越过卡住的快照照发
    seq = [r['seq'] for r in outbox(db) if r['kind'] == 'snapshot' and not r['sent_at']][0]
    assert outbox_cli.main(['--db', str(db.path), 'drop', str(seq), '--yes'], out=lambda *_: None) == 0
    assert statuses(db) == {first: 'delivered', second: 'dropped'}
    assert new_code not in published_codes(db) and codes[0] in published_codes(db)
    with db.connect() as c:
        triage.undo(c, iids[0])                                # 第一份快照仍在客人页上：取消隐藏要推出去
    db.mark_gone(new_iid)                                      # 从未上线：不推
    acts = [(h['payload']['code'], h['payload']['action']) for h in outbox(db, 'hide')]
    assert acts == [(codes[0], 'hide'), (codes[0], 'unhide')]
    mock.snapshot_status = 200
    third = do_publish(db, root, fetch)['publish_no']
    assert sync.flush(db, root)['sync_status'] == 'ok'
    assert statuses(db) == {first: 'superseded', second: 'dropped', third: 'delivered'}


def test_hides_bypass_temporarily_failing_snapshot_in_order(db, cfg, root, fetch, mock):
    iids, codes = pool(db, cfg, RANK)
    set_rank(db, codes)
    do_publish(db, root, fetch)
    sync.flush(db, root)
    do_publish(db, root, fetch)
    with db.connect() as c:
        sync.enqueue(c, 'ping', {'schema_version': 1, 'message': '快照之后的其它类别'})
    db.mark_gone(iids[0])
    with db.connect() as c:
        triage.remove(c, iids[1])
        triage.undo(c)
    mock.snapshot_status = 500
    r = sync.flush(db, root)
    assert r['sync_status'] == 'error'
    hides = [p for k, p in mock.events if k == 'hide']
    assert [(h['code'], h['action']) for h in hides] == [(codes[0], 'hide'), (codes[1], 'hide'), (codes[1], 'unhide')]
    assert [h['hide_seq'] for h in hides] == sorted(h['hide_seq'] for h in hides)
    assert 'ping' not in mock.kinds()
    snap = [r for r in outbox(db) if r['kind'] == 'snapshot'][-1]
    assert snap['sent_at'] is None and snap['failed_at'] is None       # 暂时失败：仍待发，不阻塞
    mock.snapshot_status = 200
    assert sync.flush(db, root)['sync_status'] == 'ok' and mock.kinds()[-2:] == ['snapshot', 'ping']


@pytest.mark.parametrize('mode', ['put500', 'connection', 'bad_bytes'])
def test_image_upload_failure_keeps_snapshot_pending(db, cfg, root, fetch, mock, mode):
    _, codes = pool(db, cfg, RANK)
    set_rank(db, codes)
    no = do_publish(db, root, fetch)['publish_no']
    if mode == 'put500':
        mock.put_status = 503
    elif mode == 'bad_bytes':
        mock.ack_bytes_delta = 1
    if mode == 'connection':
        with patch('requests.sessions.Session.put', side_effect=requests.ConnectionError('断开')):
            r = sync.flush(db, root)
    else:
        r = sync.flush(db, root)
    assert r['sync_status'] == 'error'
    if mode == 'bad_bytes':
        assert '图片确认无效' in r['sync_error']
    [snap] = [x for x in outbox(db) if x['kind'] == 'snapshot']
    assert snap['sent_at'] is None and snap['failed_at'] is None and statuses(db) == {no: 'pending'}
    assert 'snapshot' not in mock.kinds()


def test_image_check_is_batched(tmp_path, mock):
    names = ['%016x.jpg' % n for n in range(5203)]
    items = [{'images': [{'name': names[i], 'thumb': names[i + 1]}]} for i in range(0, len(names) - 1, 2)]
    items.append({'images': [{'name': names[-1], 'thumb': names[-1]}]})
    mock.images = set(names) - {names[0], names[600], names[5202]}
    for n in (names[0], names[600], names[5202]):
        (tmp_path / n).write_bytes(b'\xff\xd8\xff' + n.encode())
    err = sync._upload_images(requests.Session(), mock.url, TOKEN, {'items': items}, tmp_path, time.monotonic() + 60,
                              time.monotonic)
    assert err == (None, False)
    assert max(mock.checks) <= sync.IMAGE_BATCH and sum(c for c in mock.checks) >= 5203
    assert sorted(mock.puts) == sorted([names[0], names[600], names[5202]])


def test_size_limit_measures_sent_bytes_with_cjk(db, mock, root):
    payload = {'schema_version': 1, 'message': '瓷' * 200}
    env = sync._envelope('0f8fad5b-d9cb-469f-a165-70867728950e', 'ping', payload, sync.utc_now())
    size = len(sync.wire(env))
    assert size < len(json.dumps(env).encode())             # 中文不转义：比 ASCII 转义少
    with patch.object(sync, 'MAX_PAYLOAD', size):
        with db.connect() as c:
            sync.enqueue(c, 'ping', payload)
    with patch.object(sync, 'MAX_PAYLOAD', size - 1):
        with db.connect() as c:
            with pytest.raises(sync.ContractError, match='上限'):
                sync.enqueue(c, 'ping', payload)
    assert sync.flush(db, root)['sync_status'] == 'ok' and mock.bodies == [size]


# ---------- 评审修正：撤销组、reconcile、锁 ----------

def test_group_undo_reconciles_every_item(db, cfg, root, fetch):
    iids, codes = pool(db, cfg, RANK)
    set_rank(db, codes)
    do_publish(db, root, fetch)
    with db.connect() as c:
        a = triage.remove(c, iids[0])['event']
        b = triage.remove(c, iids[1])['event']
        c.execute('UPDATE triage_events SET grp=? WHERE id IN (?,?)', (a, a, b))
        ev = c.execute('SELECT * FROM triage_events WHERE id=?', (b,)).fetchone()
        triage.undo_event(c, ev)
    acts = [(h['payload']['code'], h['payload']['action']) for h in outbox(db, 'hide')]
    assert sorted(acts[2:]) == sorted([(codes[0], 'unhide'), (codes[1], 'unhide')])


def test_reconcile_table_check_and_real_errors(db, cfg, root, fetch):
    import sqlite3 as sq
    bare = sq.connect(':memory:')
    bare.row_factory = sq.Row
    assert publish.reconcile(bare, 'x') is None                   # 没建表：不是错误
    bare.execute('CREATE TABLE published(code TEXT)')
    with pytest.raises(sq.OperationalError):                      # 其它数据库错误照常抛出
        publish.reconcile(bare, 'x')


def test_hide_when_price_no_longer_eur_or_invalid(db, cfg, root, fetch):
    iids, codes = pool(db, cfg, RANK)
    set_rank(db, codes)
    do_publish(db, root, fetch)
    with db.connect() as c:
        c.execute("UPDATE items SET currency='USD' WHERE item_id=?", (iids[0],))
        publish.reconcile(c, iids[0])
        c.execute("UPDATE items SET listed_price=-1 WHERE item_id=?", (iids[1],))
        publish.reconcile(c, iids[1])
        assert [r['code'] for r in c.execute(publish.ELIGIBLE_SQL)].count(codes[1]) == 0
    assert [(h['payload']['code'], h['payload']['action']) for h in outbox(db, 'hide')] == \
        [(codes[0], 'hide'), (codes[1], 'hide')]


def test_malformed_lock_value_is_tolerated(db, cfg, root, fetch):
    _, codes = pool(db, cfg, RANK)
    set_rank(db, codes)
    for bad in ('[1,2]', '{"owner":"x","until":"soon"}', 'not json', '{"until":null}'):
        db.set('publish_lock', bad)
        assert do_publish(db, root, fetch)['rank'] == RANK


def test_lock_refreshed_and_ownership_checked_before_enqueue(db, cfg, root):
    _, codes = pool(db, cfg, RANK)
    set_rank(db, codes)
    seen = []

    def stealing_fetch(url):
        with db.connect() as c:
            seen.append(publish._held(c.execute("SELECT v FROM kv WHERE k='publish_lock'").fetchone()[0]))
        db.set('publish_lock', json.dumps({'owner': 'someone-else', 'until': time.time() + 999}))
        return png(100, 100)
    with pytest.raises(publish.PublishBusy, match='接手'):
        publish.publish(db, root, fetch=stealing_fetch)
    assert seen and seen[0][1] > time.time() + publish.LOCK_SECONDS - 60       # 处理货时已续期
    assert outbox(db, 'snapshot') == []


# ---------- 评审修正：图片隐私与下载白名单 ----------

def _exif_jpeg(tmp_path, w, h, orientation):
    src = tmp_path / 'src.png'
    src.write_bytes(png(w, h))
    jpg = tmp_path / 'plain.jpg'
    subprocess.run(['sips', '-s', 'format', 'jpeg', str(src), '--out', str(jpg)], capture_output=True, check=True)
    tiff = (b'II*\x00' + struct.pack('<I', 8) + struct.pack('<H', 2)
            + struct.pack('<HHIHH', 0x0112, 3, 1, orientation, 0)
            + struct.pack('<HHII', 0x8825, 4, 1, 0)                       # GPS IFD 指针（隐私字段）
            + b'\x00\x00\x00\x00')
    seg = b'Exif\x00\x00' + tiff
    data = jpg.read_bytes()
    out = tmp_path / 'exif.jpg'
    out.write_bytes(data[:2] + b'\xff\xe1' + struct.pack('>H', len(seg) + 2) + seg
                    + b'\xff\xfe\x00\x0dserial-1234' + data[2:])
    return out


@pytest.mark.parametrize('orientation,dims', [(1, (400, 200)), (6, (200, 400)), (8, (200, 400)), (3, (400, 200))])
def test_transcode_applies_orientation_and_strips_metadata(tmp_path, orientation, dims):
    src = _exif_jpeg(tmp_path, 400, 200, orientation)
    assert publish.jpeg_orientation(src.read_bytes()) == orientation
    name, w, h = publish.transcode(src, 1600, tmp_path / 'out')
    data = (tmp_path / 'out' / name).read_bytes()
    assert (w, h) == dims
    markers = [m for m, _, _ in publish._segments_of(data)]
    assert 0xE1 not in markers and 0xFE not in markers and b'Exif' not in data and b'serial-1234' not in data


class Resp:
    def __init__(self, status=200, ctype='image/png', body=None, location=None):
        self.status_code, self.headers = status, {'content-type': ctype}
        if location:
            self.headers['location'] = location
        self.body = png(10, 10) if body is None else body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_content(self, n):
        yield self.body


def _addr(ip):
    return lambda host, port, **kw: [(2, 1, 6, '', (ip, port))]


@pytest.mark.parametrize('url,ip,resp,error', [
    ('http://i.ebayimg.com/a.png', '93.184.216.34', Resp(), '不在允许'),
    ('https://www.inforius-bilder.de/a.png', '93.184.216.34', Resp(), '不在允许'),
    ('https://user@i.ebayimg.com/a.png', '93.184.216.34', Resp(), '不在允许'),
    ('https://i.ebayimg.com/a.png', '127.0.0.1', Resp(), '内网'),
    ('https://i.ebayimg.com/a.png', '10.0.0.8', Resp(), '内网'),
    ('https://i.ebayimg.com/a.png', '93.184.216.34', Resp(ctype='text/html'), '不是图片'),
    ('https://i.ebayimg.com/a.png', '93.184.216.34', Resp(body=b'<html>'), '不是图片'),
    ('https://i.ebayimg.com/a.png', '93.184.216.34', Resp(302, location='https://evil.example/x.png'), '不在允许'),
    ('https://i.ebayimg.com/a.png', '93.184.216.34', Resp(404), 'HTTP 404'),
])
def test_download_refuses(url, ip, resp, error):
    with patch('curio.publish.socket.getaddrinfo', _addr(ip)), patch('curio.publish.requests.get', return_value=resp):
        with pytest.raises(publish.PublishError, match=error):
            publish._download(url)


def test_download_follows_allowed_redirect():
    hops = iter([Resp(301, location='https://i.etsystatic.com/b.webp'),
                 Resp(ctype='image/webp', body=b'RIFF\x00\x00\x00\x00WEBPVP8 ')])
    with patch('curio.publish.socket.getaddrinfo', _addr('151.101.1.1')), \
            patch('curio.publish.requests.get', side_effect=lambda *a, **k: next(hops)) as get:
        assert publish._download('https://i.ebayimg.com/a.png').startswith(b'RIFF')
    assert all(k['allow_redirects'] is False for _, k in get.call_args_list)


# ---------- 评审修正：载荷有效性 ----------

def test_items_without_images_are_named(db, cfg, root):
    _, codes = pool(db, cfg, RANK)
    add(db, cfg, 11, image='https://i.ebayimg.com/images/g/broken11.png')
    set_rank(db, codes)

    def fetch(url):
        if 'broken' in url:
            raise publish.PublishError('下载失败')
        return png(50, 50)
    r = publish.publish(db, root, fetch=fetch)
    with db.connect() as c:
        code11 = triage.code(c, 'it011')
    assert r['no_images'] == [code11] and r['image_failures'] == 1


def test_long_fields_truncated_and_default_alt_uses_std_name(db, cfg, root, fetch):
    iids, codes = pool(db, cfg, RANK)
    with db.connect() as c:
        c.execute("UPDATE std_names SET std_name=?, brand=?, segments=? WHERE item_id=?",
                  ('名' * 400, 'B' * 99, json.dumps({'function': '杯' * 300, 'pattern': None}), iids[0]))
    set_rank(db, codes)
    do_publish(db, root, fetch)
    it = next(i for i in outbox(db, 'snapshot')[0]['payload']['items'] if i['code'] == codes[0])
    assert len(it['std_name']) == 200 and len(it['brand']) == 60 and len(it['form']) == 100
    assert it['images'][0]['alt'] == '名' * 200 + ' · 第 1 张 / 共 1 张'


def test_clean_seller_text_compound_platform_words():
    out = publish.clean_seller_text('Als erfahrener eBayer, auch im Kleinanzeigenmarkt und Etsyshop; siehe ebay.de/x.')
    for bad in ('ebayer', 'kleinanzeigenmarkt', 'etsyshop', 'ebay.de'):
        assert bad not in out.lower()
    assert out.startswith('Als erfahrener')


def test_tombstone_window_uses_clock(db, cfg):
    now = 1_800_000_000.0
    with db.connect() as c:
        for code, days in (('CO-0901', 91), ('CO-0902', 89)):
            c.execute('INSERT INTO publish_history(code,item_id,first_no,last_no,left_at,left_status) VALUES(?,?,?,?,?,?)',
                      (code, 'x' + code, 1, 1, publish._iso(now - days * 86400), 'sold'))
        assert [t['code'] for t in publish._tombstones(c, set(), now)] == ['CO-0902']
