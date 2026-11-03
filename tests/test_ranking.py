"""故事 11（CAP-7）：后台 02 商品池、03 榜、预览与发布——服务端可验证的部分。

覆盖 I/O 矩阵：加入榜到 10 件、榜满 / Kleinanzeigen 不能上榜、上移 / 拖动后保存（名次存储）、不足 10 件不能预览、
预览（与客人页同一模板、页内只引用本机地址、不入发件箱、不分配编号、不记录行为）、只从预览发布与发布状态、
发布后改排序 / 点评即"有未发布的修改"、移出商品池（隐藏入队并移出榜）、榜内货下架。
图片用系统 sips 真转码（源图现做的 PNG），远程图片只经注入的假下载；任何非 127.0.0.1 的请求直接失败。
界面手势（长按把手拖动、播报、390px 布局）见故事 Verification 的人工检查。
"""
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.request
import zlib
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
import requests

from curio import publish, sync, triage
from curio.config import load
from curio.store import Store

ROOT = Path(__file__).resolve().parents[1]
SERVER_PY = ROOT / 'server' / '.venv' / 'bin' / 'python'
TOKEN = 'test-admin-token-RANKING'
_real_request = requests.sessions.Session.request
pytestmark = [pytest.mark.skipif(shutil.which('sips') is None, reason='需要 macOS 自带 sips'),
              pytest.mark.skipif(not SERVER_PY.exists(), reason='预览需要 server/.venv（cd server && uv sync）')]


@pytest.fixture(autouse=True)
def only_localhost():
    def guard(self, method, url, *a, **kw):
        assert urlsplit(url).hostname == '127.0.0.1', '测试不得连接外部主机'
        return _real_request(self, method, url, *a, **kw)
    with patch('requests.sessions.Session.request', guard), patch('curio.sync._notify') as note:
        yield note


def png(w, h, rgb=(40, 80, 160)):
    raw = b''.join(b'\x00' + bytes(rgb) * w for _ in range(h))

    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw, 9)) + chunk(b'IEND', b''))


class Fetch:
    def __init__(self):
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        n = int(re.search(r'(\d+)', url).group(1)) if re.search(r'(\d+)', url) else 1
        return png(800, 600, (n % 256, 90, 150))


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


def add(db, cfg, n, platform='ebay', keep=True, score=None):
    iid = ('ka:%d' % n) if platform == 'kleinanzeigen' else 'it%03d' % n
    db.ingest(dict(item_id=iid, platform=platform, title=f'Meissen Tasse Nr {n}', price=100 + n * 37,
                   currency='EUR', buying='FIXED_PRICE', url=f'https://www.ebay.de/itm/{100000000000 + n}',
                   image=f'https://i.ebayimg.com/images/g/img{n}.png',
                   description='Sehr guter Zustand, keine Chips. https://www.ebay.de/str/porzellanfritz'), cfg)
    form_id, form = FORMS[n % 3]
    brand = 'Meissen' if n % 3 else 'KPM Berlin'
    with db.connect() as c:
        c.execute('''INSERT OR REPLACE INTO std_names(item_id,std_name,std_key,status,brand,pattern_id,form_id,function_id,
            era,seller_grade,segments,kb_status,kb_fingerprint,generated_at,overpaint)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)''',
                  (iid, f'{brand} {form} {n}', f'{brand}|-|-|{form_id}', 'named', brand, None, None, form_id, None, None,
                   json.dumps({'brand': brand, 'function': form}, ensure_ascii=False), 'ok', 'fp',
                   '2026-11-03T00:00:00+00:00'))
        if score is not None:
            c.execute("INSERT OR REPLACE INTO scores(item_id,score,source,reason,rule_version,scored_at) "
                      "VALUES(?,?,'rule','测试',1,'2026-11-03T00:00:00+00:00')", (iid, score))
        if keep:
            triage.keep(c, iid)
        return iid, triage.code(c, iid)


def kept(db, cfg, n=12):
    out = [add(db, cfg, i, score=i) for i in range(1, n + 1)]
    return [i for i, _ in out], [c for _, c in out]


def outbox(db, kind=None):
    with db.connect() as c:
        rows = [dict(r) for r in c.execute('SELECT * FROM outbox ORDER BY seq')]
    return [dict(r, payload=json.loads(r['payload'])) for r in rows if kind is None or r['kind'] == kind]


class Flush:
    def __init__(self):
        self.calls = 0
        self.done = threading.Event()

    def __call__(self):
        self.calls += 1
        self.done.set()


@pytest.fixture
def http(db, root, fetch):
    from curio.review import server
    flush = Flush()
    httpd = server(db, port=0, root=root, fetch=fetch, flush=flush)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = 'http://127.0.0.1:' + str(httpd.server_port)
    page = urllib.request.urlopen(base + '/').read().decode()
    token = re.search("const token='([^']+)'", page)[1]

    def raw(path):
        try:
            r = urllib.request.urlopen(base + path)
            return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def get(path):
        return json.load(urllib.request.urlopen(base + path))

    def post(path, body, tok=None, headers=None):
        h = {'Content-Type': 'application/json', 'X-Curio-Token': token if tok is None else tok, **(headers or {})}
        req = urllib.request.Request(base + path, json.dumps(body).encode(), headers=h)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    rank_ids = iter(range(10_000))

    def save(codes, request_id=None):
        return post('/api/rank', {'codes': codes, 'request_id': request_id or 'rank-%d' % next(rank_ids)})

    yield dict(base=base, page=page, token=token, get=get, post=post, raw=raw, save=save, flush=flush)
    httpd.shutdown()
    httpd.server_close()


def ranked(db):
    with db.connect() as c:
        return publish.rank(c)


# ---------- 页面与导航 ----------

def test_tabs_02_and_03_enabled_and_scripts_served(http):
    page = http['page']
    assert 'href="/#pool"' in page and 'href="/#rank"' in page and page.count('暂不可用') == 0
    assert 'id="poolView"' in page and 'id="rankView"' in page and '/ranking.js' in page
    status, headers, body = http['raw']('/ranking.js')
    assert status == 200 and 'javascript' in headers['Content-Type'] and b'/api/rank/preview' in body
    js = body.decode()
    # 只在把手上长按拖动；播报文案照 EXPERIENCE.md。
    assert "'已拿起，第 '" in js and "'已放到第 '" in js and '榜需满 10 件才能发布' in js and '排序尚未保存' in js
    css = page
    assert '-webkit-touch-callout:none' in css and 'touch-action:none' in css
    assert '>发布<' not in re.search(r'<section id="rankView".*?</section>', page, re.S)[0]   # 03 上不放"发布"


# ---------- 02 商品池 ----------

def test_pool_lists_kept_items_by_score_with_flags(db, cfg, http):
    iids, codes = kept(db, cfg, 12)
    ka, ka_code = add(db, cfg, 50, platform='kleinanzeigen', score=99)
    gone, gone_code = add(db, cfg, 51, score=0)
    db.mark_gone(gone)
    add(db, cfg, 52, keep=False)                    # 新货：不在商品池
    d = http['get']('/api/pool')
    assert d['counts']['kept'] == 14 and d['rank'] == []
    order = [it['code'] for it in d['items']]
    assert order[0] == ka_code and order[1:13] == list(reversed(codes))       # 分数高到低
    by = {it['code']: it for it in d['items']}
    assert by[ka_code]['rank_reason'] == 'Kleinanzeigen 的货不能上榜'
    assert '仅后台 · Kleinanzeigen' in by[ka_code]['tags']
    assert by[gone_code]['on_sale'] is False and by[gone_code]['rank_reason']
    assert by[codes[0]]['rank_reason'] is None and by[codes[0]]['price'].startswith('€')
    assert by[codes[0]]['platform'] == 'eBay' and by[codes[0]]['rank_pos'] is None


def test_add_to_rank_until_full_then_preview_enabled(db, cfg, http):
    _, codes = kept(db, cfg, 12)
    status, d = http['save'](codes[:9])
    assert status == 200 and d['count'] == 9
    assert http['post']('/api/rank/preview', {'rank': codes[:9]}) == (400, {'error': '榜需满 10 件才能发布（现在 9 件）'})
    status, d = http['save'](codes[:10])
    assert status == 200 and d['count'] == 10 and [r['code'] for r in d['rows']] == codes[:10]
    assert http['get']('/api/pool')['items'][-1]['rank_pos'] == 1        # 分数最低的 codes[0] 在榜第 1 名
    status, d = http['save'](codes[:11])
    assert status == 400 and '最多 10 件' in d['error'] and ranked(db) == codes[:10]


def test_kleinanzeigen_cannot_be_ranked(db, cfg, http):
    _, codes = kept(db, cfg, 3)
    _, ka = add(db, cfg, 50, platform='kleinanzeigen')
    status, d = http['save'](codes + [ka])
    assert status == 400 and 'Kleinanzeigen' in d['error'] and ranked(db) == []


def test_move_up_and_drag_save_order(db, cfg, http):
    _, codes = kept(db, cfg, 10)
    http['save'](codes)
    up = codes[:]
    up[1], up[2] = up[2], up[1]                       # 第 3 名上移到第 2 名
    status, d = http['save'](up)
    assert status == 200 and [r['code'] for r in d['rows']] == up and ranked(db) == up
    dragged = up[:]
    dragged.insert(4, dragged.pop(0))                 # 第 1 名拖到第 5 名
    http['save'](dragged)
    assert ranked(db) == dragged and [r['pos'] for r in http['get']('/api/rank')['rows']] == list(range(1, 11))


def test_rank_save_is_idempotent_by_request_id(db, cfg, http):
    _, codes = kept(db, cfg, 4)
    assert http['save'](codes[:2], 'same')[0] == 200
    assert http['save'](codes[:3], 'other')[0] == 200
    status, d = http['save'](codes[:2], 'same')        # 重放：返回首次结果，不改当前榜
    assert status == 200 and ranked(db) == codes[:3]
    assert http['save'](codes[:1], 'same') == (400, {'error': '重复请求内容不同'})


def test_writes_need_token_and_same_origin(db, cfg, http):
    _, codes = kept(db, cfg, 2)
    for path, body in (('/api/rank', {'codes': codes, 'request_id': 'x'}), ('/api/rank/preview', {}),
                       ('/api/rank/publish', {'rank': [], 'request_id': 'y'}),
                       ('/api/pool/action', {'action': 'out_of_pool', 'item_id': 'it001', 'request_id': 'z'})):
        assert http['post'](path, body, tok='wrong')[0] == 403
        assert http['post'](path, body, headers={'Origin': 'http://evil.example'})[0] == 403
    assert ranked(db) == []


# ---------- 移出 / 放回商品池 ----------

def test_out_of_pool_hides_published_item_and_leaves_rank(db, cfg, http, root, fetch):
    iids, codes = kept(db, cfg, 12)
    http['save'](codes[:10])
    publish.publish(db, root, fetch=fetch)
    before = len(outbox(db, 'hide'))
    status, d = http['post']('/api/pool/action', {'action': 'out_of_pool', 'item_id': iids[2], 'request_id': 'o1'})
    assert status == 200 and d['state'] == 'out_of_pool' and d['left_rank'] is True
    assert codes[2] not in ranked(db) and len(ranked(db)) == 9
    hides = outbox(db, 'hide')
    assert len(hides) == before + 1 and hides[-1]['payload']['code'] == codes[2]
    assert hides[-1]['payload']['action'] == 'hide' and hides[-1]['payload']['reason'] == 'removed'
    # 重放同一请求：不再入队。
    assert http['post']('/api/pool/action', {'action': 'out_of_pool', 'item_id': iids[2], 'request_id': 'o1'})[0] == 200
    assert len(outbox(db, 'hide')) == before + 1
    pool_ = http['get']('/api/pool')
    assert pool_['counts'] == {'kept': 11, 'out': 1, 'rank': 9} and pool_['out'][0]['code'] == codes[2]
    status, d = http['post']('/api/pool/action', {'action': 'back_to_pool', 'item_id': iids[2], 'request_id': 'b1'})
    assert status == 200 and d['state'] == 'kept' and outbox(db, 'hide')[-1]['payload']['action'] == 'unhide'
    assert codes[2] not in ranked(db)                  # 放回商品池不自动回到榜
    status, d = http['post']('/api/pool/action', {'action': 'back_to_pool', 'item_id': iids[2], 'request_id': 'b2'})
    assert status == 400 and '不能放回商品池' in d['error']


# ---------- 榜内下架 ----------

def test_gone_item_in_rank_marked_and_blocks_preview_until_replaced(db, cfg, http):
    iids, codes = kept(db, cfg, 12)
    http['save'](codes[:10])
    db.mark_gone(iids[3])
    d = http['get']('/api/rank')
    row = d['rows'][3]
    assert row['off'] is True and row['gone'] is True and '已下架' in row['reason']
    assert sum(r['off'] for r in d['rows']) == 1
    # 下架的行可原样留在榜里继续排序。
    moved = codes[:10]
    moved[0], moved[1] = moved[1], moved[0]
    assert http['save'](moved)[0] == 200 and ranked(db) == moved
    status, err = http['post']('/api/rank/preview', {'rank': moved})
    assert status == 400 and '第 4 名' in err['error']
    # 补位：移出下架的，加入另一件。
    fixed = [c for c in moved if c != codes[3]] + [codes[10]]
    assert http['save'](fixed)[0] == 200
    assert http['post']('/api/rank/preview', {'rank': fixed})[0] == 200
    # 新加入的货必须合格（下架的不能再加回来）。
    status, err = http['save'](fixed[:9] + [codes[3]])
    assert status == 400 and '已下架' in err['error']


# ---------- 预览 ----------

def test_preview_renders_candidate_with_guest_templates_locally(db, cfg, http, fetch):
    _, codes = kept(db, cfg, 25)
    rank = list(reversed(codes[5:15]))
    http['save'](rank)
    status, d = http['post']('/api/rank/preview', {'rank': rank})
    assert status == 200 and d['url'] == '/preview/' and d['pool'] == 25
    status, headers, body = http['raw']('/preview/')
    html = body.decode()
    assert status == 200 and headers['Content-Security-Policy'].startswith("default-src 'self'")
    # 横条在顶：返回修改 / 发布；客人页本体与服务器同一份模板（今日榜、全部藏品）。
    assert html.index('class="pvbar"') < html.index('<h1 class="display">今日榜')
    assert '返回修改' in html and '>发布</button>' in html and '全部藏品' in html
    assert re.findall(r'<li class="rk"><a class="rk-a" href="/preview/item/(CO-\d+)"', html) == rank
    # 页内只引用本机地址：没有外部主机、原平台链接或图片地址。
    assert not re.search(r'https?://|ebay|etsy|kleinanzeigen', html, re.I)
    assert not re.search(r'(?:href|src|action)="(?!/preview/|/#rank|#)', html)
    img = re.search(r'src="(/preview/img/[0-9a-f]{16}\.jpg)"', html)[1]
    status, headers, data = http['raw'](img)
    assert status == 200 and headers['Content-Type'] == 'image/jpeg' and data[:3] == b'\xff\xd8\xff'
    assert http['raw']('/preview/img/0123456789abcdef.jpg')[0] == 404
    # 单件页、筛选、续载、静态资源都可用。
    status, _, item = http['raw']('/preview/item/' + rank[0])
    assert status == 200 and '货号 ' + rank[0] in item.decode() and 'class="pvbar"' in item.decode()
    status, _, filtered = http['raw']('/preview/?form=FN-044')
    assert status == 200 and '已选花瓶' in filtered.decode()
    more = http['get']('/preview/api/pool?offset=20')
    assert more['total'] == 25 and more['count'] == 5 and 'href="/preview/item/' in more['html']
    status, _, js = http['raw']('/preview/static/guest.js')
    assert status == 200 and b"'/preview/api/pool?'" in js and b"'/api/pool?'" not in js
    assert http['raw']('/preview/static/guest.css')[0] == 200 and http['raw']('/preview/bar.js')[0] == 200
    # 不上线、不入发件箱、不分配编号、不记发布。
    assert outbox(db) == []
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM publishes').fetchone()[0] == 0
        assert c.execute('SELECT count(*) FROM published').fetchone()[0] == 0
        assert c.execute("SELECT v FROM kv WHERE k='publish_seq'").fetchone() is None
    assert http['flush'].calls == 0
    # 图片只经注入的下载（本测试里全部是假下载），转码文件留在本机。
    assert fetch.urls and all(urlsplit(u).hostname == 'i.ebayimg.com' for u in fetch.urls)


def test_preview_invalidated_by_rank_change(db, cfg, http):
    _, codes = kept(db, cfg, 11)
    http['save'](codes[:10])
    assert http['post']('/api/rank/preview', {'rank': codes[:10]})[0] == 200
    assert 'class="pvbar"' in http['raw']('/preview/')[2].decode()
    http['save'](codes[1:11])
    status, _, body = http['raw']('/preview/')
    assert status == 200 and '预览已失效' in body.decode()
    # 界面所见的榜与库里不同（别处改过）：不生成预览。
    assert http['post']('/api/rank/preview', {'rank': codes[:10]})[0] == 409


# ---------- 发布（只从预览横条） ----------

def test_publish_only_from_preview_and_status(db, cfg, http):
    iids, codes = kept(db, cfg, 12)
    rank = codes[:10]
    http['save'](rank)
    assert http['get']('/api/rank')['status'] == {'published_at': None, 'publish_no': None, 'dirty': True, 'orders': 0}
    status, d = http['post']('/api/rank/publish', {'rank': rank, 'request_id': 'p0'})
    assert status == 409 and '预览' in d['error'] and outbox(db, 'snapshot') == []
    assert http['post']('/api/rank/preview', {'rank': rank})[0] == 200
    status, d = http['post']('/api/rank/publish', {'rank': list(reversed(rank)), 'request_id': 'p1'})
    assert status == 409 and '重新预览' in d['error']
    status, d = http['post']('/api/rank/publish', {'rank': rank, 'request_id': 'p2'})
    assert status == 200 and d['publish_no'] >= 1 and re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z', d['published_at'])
    snaps = outbox(db, 'snapshot')
    assert len(snaps) == 1 and snaps[0]['payload']['rank'] == rank
    assert http['flush'].done.wait(5) and http['flush'].calls == 1          # 后台触发一次同步
    # 重放：同一结果，不再发布。
    assert http['post']('/api/rank/publish', {'rank': rank, 'request_id': 'p2'}) == (200, d)
    assert len(outbox(db, 'snapshot')) == 1
    st = http['get']('/api/rank')['status']
    assert st == {'published_at': d['published_at'], 'publish_no': d['publish_no'], 'dirty': False,
                  'orders': 0}
    # 发布后预览作废：再发布须重新预览。
    assert http['post']('/api/rank/publish', {'rank': rank, 'request_id': 'p3'})[0] == 409
    # 改排序 → 有未发布的修改；改回原顺序 → 没有。
    moved = rank[1:] + rank[:1]
    http['save'](moved)
    assert http['get']('/api/rank')['status']['dirty'] is True
    http['save'](rank)
    assert http['get']('/api/rank')['status']['dirty'] is False
    # 改点评 → 有未发布的修改（同一秒内也算）；删掉点评回到原样 → 没有。
    assert http['post']('/api/triage/comment', {'item_id': iids[0], 'text': '釉色温润。'})[0] == 200
    assert http['get']('/api/rank')['status']['dirty'] is True
    assert http['post']('/api/triage/comment', {'item_id': iids[0], 'text': ''})[0] == 200
    assert http['get']('/api/rank')['status']['dirty'] is False
    # 改替代文字 → 有未发布的修改。
    assert http['post']('/api/triage/alt', {'item_id': iids[1], 'index': 0, 'text': '杯身侧面'})[0] == 200
    assert http['get']('/api/rank')['status']['dirty'] is True
    assert http['post']('/api/triage/alt', {'item_id': iids[1], 'index': 0, 'text': ''})[0] == 200
    # 商品池变化（价格、下架）→ 有未发布的修改。
    assert http['get']('/api/rank')['status']['dirty'] is False
    db.mark_gone(iids[11])
    assert http['get']('/api/rank')['status']['dirty'] is True


def test_publish_failure_keeps_rank(db, cfg, http):
    _, codes = kept(db, cfg, 10)
    http['save'](codes)
    assert http['post']('/api/rank/preview', {'rank': codes})[0] == 200
    with db.connect() as c:          # 另一个发布正在进行（发布锁）
        c.execute("INSERT OR REPLACE INTO kv VALUES('publish_lock',?)",
                  (json.dumps({'owner': 'other', 'until': time.time() + 600}),))
    status, d = http['post']('/api/rank/publish', {'rank': codes, 'request_id': 'busy'})
    assert status == 409 and '另一个发布正在进行' in d['error']
    assert ranked(db) == codes and outbox(db, 'snapshot') == [] and http['flush'].calls == 0
    with db.connect() as c:
        c.execute("DELETE FROM kv WHERE k='publish_lock'")
    # 发布前预览已作废：须重新预览后再发布。
    assert http['post']('/api/rank/publish', {'rank': codes, 'request_id': 'busy-2'})[0] == 409
    assert http['post']('/api/rank/preview', {'rank': codes})[0] == 200
    assert http['post']('/api/rank/publish', {'rank': codes, 'request_id': 'busy-3'})[0] == 200


def test_preview_needs_rank_list(db, cfg, http):
    kept(db, cfg, 10)
    assert http['post']('/api/rank/preview', {})[0] == 400
    assert http['post']('/api/rank/preview', {'rank': 'CO-0001'}) == (400, {'error': '榜须为货号列表'})


def test_comment_after_preview_blocks_publish(db, cfg, http):
    iids, codes = kept(db, cfg, 10)
    http['save'](codes)
    assert http['post']('/api/rank/preview', {'rank': codes})[0] == 200
    assert http['post']('/api/triage/comment', {'item_id': iids[0], 'text': '新点评'})[0] == 200
    status, d = http['post']('/api/rank/publish', {'rank': codes, 'request_id': 'c1'})
    assert status == 409 and outbox(db, 'snapshot') == []
    assert '预览已失效' in http['raw']('/preview/')[2].decode()


def test_out_of_pool_of_unranked_item_invalidates_preview(db, cfg, http):
    iids, codes = kept(db, cfg, 11)
    http['save'](codes[:10])
    assert http['post']('/api/rank/preview', {'rank': codes[:10]})[0] == 200
    status, d = http['post']('/api/pool/action', {'action': 'out_of_pool', 'item_id': iids[10], 'request_id': 'o'})
    assert status == 200 and d['left_rank'] is False
    assert '预览已失效' in http['raw']('/preview/')[2].decode()


def test_publish_rechecks_content_fingerprint(db, cfg, root, fetch):
    iids, codes = kept(db, cfg, 10)
    with db.connect() as c:
        publish.set_rank(c, codes)
    payload, _ = publish.candidate(db, root, fetch)
    fp = publish.payload_fingerprint(payload)
    with db.connect() as c:
        c.execute("INSERT INTO comments VALUES(?,?,?)", (iids[0], '预览后才写的点评', '2026-11-03T00:00:00+00:00'))
    with pytest.raises(publish.PublishStale) as e:
        publish.publish(db, root, fetch=fetch, expect_rank=codes, expect_fingerprint=fp)
    assert '内容在预览后有变化' in str(e.value) and outbox(db) == []
    payload, _ = publish.candidate(db, root, fetch)
    r = publish.publish(db, root, fetch=fetch, expect_rank=codes, expect_fingerprint=publish.payload_fingerprint(payload))
    assert r['publish_no'] >= 1 and outbox(db, 'snapshot')[0]['payload']['rank'] == codes


def test_expect_rank_mismatch_leaves_no_trace(db, cfg, root, fetch):
    _, codes = kept(db, cfg, 10)
    with db.connect() as c:
        publish.set_rank(c, codes)
        seq = c.execute("SELECT v FROM kv WHERE k='publish_seq'").fetchone()
    with pytest.raises(publish.PublishError):
        publish.publish(db, root, fetch=fetch, expect_rank=list(reversed(codes)), request_id='r1')
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM publishes').fetchone()[0] == 0
        assert c.execute("SELECT v FROM kv WHERE k='publish_seq'").fetchone() == seq
        assert c.execute("SELECT count(*) FROM triage_requests WHERE request_id='r1'").fetchone()[0] == 0
    assert outbox(db) == []


def test_request_result_recorded_with_snapshot_and_replayed(db, cfg, root, fetch):
    _, codes = kept(db, cfg, 10)
    with db.connect() as c:
        publish.set_rank(c, codes)
    first = publish.publish(db, root, fetch=fetch, expect_rank=codes, request_id='once')
    with db.connect() as c:
        row = c.execute("SELECT result FROM triage_requests WHERE request_id='once'").fetchone()
        assert json.loads(row[0])['publish_no'] == first['publish_no']
    again = publish.publish(db, root, fetch=fetch, expect_rank=codes, request_id='once')
    assert again['replayed'] is True and again['publish_no'] == first['publish_no']
    assert len(outbox(db, 'snapshot')) == 1


def test_concurrent_retry_waits_for_first_publish(db, cfg, http, monkeypatch):
    _, codes = kept(db, cfg, 10)
    http['save'](codes)
    assert http['post']('/api/rank/preview', {'rank': codes})[0] == 200
    real = publish.prepare_images

    def slow(*a, **kw):
        time.sleep(1.0)
        return real(*a, **kw)
    monkeypatch.setattr(publish, 'prepare_images', slow)
    out = []

    def go():
        out.append(http['post']('/api/rank/publish', {'rank': codes, 'request_id': 'twice'}))
    threads = [threading.Thread(target=go) for _ in range(2)]
    threads[0].start(); time.sleep(0.3); threads[1].start()
    for t in threads:
        t.join(15)
    assert [o[0] for o in out] == [200, 200] and out[0][1] == out[1][1]
    assert len(outbox(db, 'snapshot')) == 1


def test_preview_invalidated_while_generating_is_not_kept(db, cfg, http, monkeypatch):
    iids, codes = kept(db, cfg, 10)
    http['save'](codes)
    real = publish.candidate

    def slow(*a, **kw):
        r = real(*a, **kw)
        time.sleep(1.0)
        return r
    monkeypatch.setattr(publish, 'candidate', slow)
    out = []
    t = threading.Thread(target=lambda: out.append(http['post']('/api/rank/preview', {'rank': codes})))
    t.start(); time.sleep(0.5)
    assert http['post']('/api/triage/comment', {'item_id': iids[0], 'text': '生成途中改的'})[0] == 200
    t.join(15)
    assert out[0][0] == 409 and '预览期间内容有变化' in out[0][1]['error']
    assert '预览已失效' in http['raw']('/preview/')[2].decode()


def test_detail_reports_rank_reason_from_server(db, cfg, http):
    iids, _ = kept(db, cfg, 2)
    ka, _ = add(db, cfg, 50, platform='kleinanzeigen')
    new, _ = add(db, cfg, 51, keep=False)
    db.mark_gone(iids[1])
    reason = lambda iid: http['get']('/api/triage/item?id=' + iid)['rank_reason']
    assert reason(iids[0]) is None and reason(ka) == 'Kleinanzeigen 的货不能上榜'
    assert reason(new) == '不在商品池，不能上榜' and reason(iids[1]) == '原平台已下架，不能上榜'


def test_render_preview_rejects_empty_output(tmp_path):
    from curio import ranking
    py = tmp_path / '.venv' / 'bin' / 'python'
    py.parent.mkdir(parents=True)
    py.write_text('#!/bin/sh\nexit 0\n')
    py.chmod(0o755)
    with pytest.raises(ranking.PreviewError):
        ranking.render_preview({}, '/', server_dir=tmp_path)


# ---------- 验收：预览与发布后客人首页的榜顺序一致（本机真服务器） ----------

def _free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


@pytest.fixture
def live(tmp_path):
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


def test_acceptance_preview_then_publish_matches_guest_home(db, cfg, root, fetch, live):
    from curio.review import server
    iids, codes = kept(db, cfg, 12)
    httpd = server(db, port=0, root=root, fetch=fetch)          # 默认 flush：sync.flush(store, root) → 本机服务器
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = 'http://127.0.0.1:' + str(httpd.server_port)
    try:
        token = re.search("const token='([^']+)'", requests.get(base + '/').text)[1]
        h = {'X-Curio-Token': token}
        # 02 商品池按分数列出 12 件；加入 10 件、排序（上移、拖动）。
        assert [it['code'] for it in requests.get(base + '/api/pool').json()['items']] == list(reversed(codes))
        rank = []
        for n, code in enumerate(reversed(codes[2:])):
            rank.append(code)
            assert requests.post(base + '/api/rank', json={'codes': rank, 'request_id': f'add{n}'}, headers=h).ok
        rank[2], rank[1] = rank[1], rank[2]
        rank.insert(4, rank.pop(0))
        assert requests.post(base + '/api/rank', json={'codes': rank, 'request_id': 'order'}, headers=h).ok
        assert requests.post(base + '/api/rank/preview', json={'rank': rank}, headers=h).ok
        preview_html = requests.get(base + '/preview/').text
        preview_rank = re.findall(r'<li class="rk"><a class="rk-a" href="/preview/item/(CO-\d+)"', preview_html)
        assert preview_rank == rank
        with db.connect() as c:          # 邀请码先入队：发布后的那一次后台 flush 一起送达
            sync.enqueue_invite(c, 'MOMO2026')
        r = requests.post(base + '/api/rank/publish', json={'rank': rank, 'request_id': 'go'}, headers=h)
        assert r.ok
        # 只靠发布后后台线程里的那一次 flush 送达（不手动 flush）。
        for _ in range(300):
            with db.connect() as c:
                done = c.execute('SELECT status FROM publishes WHERE publish_no=?',
                                 (r.json()['publish_no'],)).fetchone()[0] == 'delivered'
                left = c.execute('SELECT count(*) FROM outbox WHERE sent_at IS NULL').fetchone()[0]
            if done and not left:
                break
            time.sleep(0.1)
        else:
            pytest.fail('后台 flush 未在 30 秒内送达快照')
        s = requests.Session()
        s.headers['Origin'] = live
        assert s.post(live + '/register', allow_redirects=False,
                      data={'invite': 'MOMO2026', 'username': 'wangjie', 'password': 'meissen1710',
                            'nickname': '王姐', 'consent': 'yes', 'next': '/'}).status_code == 303
        home = s.get(live + '/').text
        assert re.findall(r'<li class="rk"><a class="rk-a" href="/item/(CO-\d+)"', home) == preview_rank
        # 预览页除前缀与横条外与客人页同构：卡片顺序一致。
        assert re.findall(r'<li class="card"><a class="card-a" href="/item/(CO-\d+)"', home) == \
            re.findall(r'<li class="card"><a class="card-a" href="/preview/item/(CO-\d+)"', preview_html)
        st = requests.get(base + '/api/rank').json()['status']
        assert st['published_at'] == r.json()['published_at'] and st['dirty'] is False
    finally:
        httpd.shutdown()
        httpd.server_close()
