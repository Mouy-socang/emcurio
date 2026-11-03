"""故事 13：已发布货下架检查、心跳、标记售出与复查后即时发送（CAP-2、AD-5、AD-10、AD-26、AD-27）。

外部平台全部模拟；同步只连本机 127.0.0.1 上临时启动的服务器（标准库假服务器，或 server/.venv 里的真服务器）。
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
import zlib
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

import pytest
import requests

from curio import curio_monitor, publish, ranking, sync, triage
from curio.config import load
from curio.errors import CollectionError
from curio.schedule import (KV_LAST_FETCH, KV_LAST_CHECK, KV_LAST_PUBLISHED, KV_PUBLISHED_OK, Budget, Scheduler,
                            iso_z, published_check_interval)
from curio.store import Store

ROOT = Path(__file__).resolve().parents[1]
SERVER_PY = ROOT / 'server' / '.venv' / 'bin' / 'python'
TOKEN = 'test-admin-token-SENTINEL'
_real_request = requests.sessions.Session.request


@pytest.fixture(autouse=True)
def only_localhost():
    def guard(self, method, url, *a, **kw):
        assert urlsplit(url).hostname == '127.0.0.1', '测试不得连接外部主机'
        return _real_request(self, method, url, *a, **kw)
    with patch('requests.sessions.Session.request', guard), patch('curio.sync._notify') as note:
        yield note


class Clock:
    def __init__(self, ts=None):
        self.ts = time.time() if ts is None else ts

    def __call__(self):
        return self.ts


@pytest.fixture
def root(tmp_path):
    shutil.copytree(ROOT / 'config', tmp_path / 'config')
    (tmp_path / '.env').write_text('EBAY_CLIENT_ID=dummy\nEBAY_CLIENT_SECRET=dummy\n')
    return tmp_path


@pytest.fixture
def cfg(root):
    return load(root)


@pytest.fixture
def db(root):
    return Store(root / 'data' / 'curio.db')


def add(db, cfg, n, platform='ebay', published=True, publish_no=1):
    """一件留下的货；published=True 时直接记为"可能在线"（published 表），不经转码与发件箱。"""
    iid = f'ets:{n}' if platform == 'etsy' else f'it{n:03d}'
    db.ingest(dict(item_id=iid, platform=platform, title=f'Meissen Tasse Nr {n}', price=100 + n, currency='EUR',
                   buying='FIXED_PRICE', url=f'https://www.ebay.de/itm/{n}', image=''), cfg)
    with db.connect() as c:
        triage.keep(c, iid)
        code = triage.code(c, iid)
        if published:
            c.execute('INSERT INTO published(code,item_id,publish_no,changed_at) VALUES(?,?,?,?)',
                      (code, iid, publish_no, sync.utc_now()))
            c.execute('INSERT INTO publish_items(publish_no,code,item_id) VALUES(?,?,?)', (publish_no, code, iid))
    return iid, code


def outbox(db, kind=None):
    with db.connect() as c:
        rows = [dict(r) for r in c.execute('SELECT * FROM outbox ORDER BY seq')]
    return [dict(r, payload=json.loads(r['payload'])) for r in rows if kind is None or r['kind'] == kind]


class FakeEbay:
    """在售判定用的假 eBay：detail() 计入在售检查预算；gone 里的返回 None（404），fail 里的抛错。"""

    def __init__(self, db, budget=None, gone=(), fail=()):
        self.db, self.budget, self.gone, self.fail = db, budget, set(gone), set(fail)
        self.purpose = 'check'
        self.calls = []

    def detail(self, iid):
        if self.budget is not None:
            self.budget.spend('ebay', self.purpose)
        self.calls.append(iid)
        if iid in self.fail:
            raise CollectionError('eBay 网络请求失败')
        if iid in self.gone:
            return None
        with self.db.connect() as c:
            row = c.execute('SELECT * FROM items WHERE item_id=?', (iid,)).fetchone()
        return {'itemId': iid, 'title': row['title'], 'price': {'value': str(row['listed_price']), 'currency': 'EUR'}}


def ok_at(db, platform):
    return db.get(KV_PUBLISHED_OK + platform)


# ---------- 调度：独立节拍 ----------

def run_with(root, clock, ebay_api, check=None):
    with patch('curio.curio_monitor.EbayAPI', return_value=ebay_api), \
         patch('curio.curio_monitor.collect_ebay', Mock(return_value=0)) as e, \
         patch('curio.curio_monitor.collect_ka', side_effect=lambda store, config, budget=None: store.set('ka_status', 'ok') or 0), \
         patch('curio.curio_monitor.check_existing', check or Mock(return_value={'attempted': 0, 'errors': 0})) as c:
        return curio_monitor.run(root, clock), e, c


def not_due_except_published(db, clock, published_minutes_ago):
    """新货抓取与普通在售检查都刚做过；已发布货检查在 N 分钟前。"""
    db.set(KV_LAST_FETCH, clock.ts - 60)
    db.set('schedule_fetch_interval_min', 60)
    db.set(KV_LAST_CHECK, clock.ts - 60)
    db.set(KV_LAST_PUBLISHED, clock.ts - published_minutes_ago * 60)


def test_due_published_check_runs_alone_and_counts(db, cfg, root):
    clock = Clock()
    for n in (1, 2):
        add(db, cfg, n)
    not_due_except_published(db, clock, 16)
    api = FakeEbay(db)
    result, fetch, check = run_with(root, clock, api)
    assert result['published_check_ran'] and not result['fetch_ran'] and not result['status_check_ran']
    assert not fetch.called and not check.called
    pc = result['published_check']
    assert pc['attempted'] == 2 and pc['active'] == 2 and pc['errors'] == 0
    assert pc['platforms']['ebay'] == {'items': 2, 'ok': True, 'status': 'ok'}
    assert ok_at(db, 'ebay') == iso_z(clock.ts) and ok_at(db, 'etsy') == iso_z(clock.ts)   # Etsy 没有已发布货：无需检查
    assert result['published_check_interval_min'] >= 15
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM collection_runs').fetchone()[0] == 1
        assert c.execute('SELECT count(*) FROM status_check_events').fetchone()[0] == 2


def test_not_due_after_five_minutes(db, cfg, root):
    clock = Clock()
    add(db, cfg, 1)
    not_due_except_published(db, clock, 5)
    api = FakeEbay(db)
    result, *_ = run_with(root, clock, api)
    assert result['status'] == 'idle' and api.calls == [] and ok_at(db, 'ebay') is None


def test_nothing_published_counts_as_success_without_work(db, cfg, root):
    clock = Clock()
    add(db, cfg, 1, published=False)
    not_due_except_published(db, clock, 16)
    api = FakeEbay(db)
    result, *_ = run_with(root, clock, api)
    assert result['status'] == 'idle' and api.calls == []
    assert ok_at(db, 'ebay') == ok_at(db, 'etsy') == iso_z(clock.ts)
    assert float(db.get(KV_LAST_PUBLISHED)) == clock.ts


def test_published_check_runs_before_fetch(db, cfg, root):
    clock = Clock()
    add(db, cfg, 1)
    order = []
    api = FakeEbay(db)
    real = api.detail
    api.detail = lambda iid: order.append('published') or real(iid)
    with patch('curio.curio_monitor.EbayAPI', return_value=api), \
         patch('curio.curio_monitor.collect_ebay', side_effect=lambda *a: order.append('fetch') or 0), \
         patch('curio.curio_monitor.collect_ka', side_effect=lambda store, config, budget=None: store.set('ka_status', 'ok') or 0), \
         patch('curio.curio_monitor.check_existing', side_effect=lambda *a: order.append('check') or {'errors': 0}):
        curio_monitor.run(root, clock)
    assert order == ['published', 'fetch', 'check']


# ---------- 判定：下架、部分失败、预算 ----------

def test_gone_published_item_is_hidden_in_same_round(db, cfg):
    iid, code = add(db, cfg, 1)
    add(db, cfg, 2)
    api = FakeEbay(db, gone={iid})
    summary = curio_monitor.check_published(db, api, cfg, sched=Scheduler(db, Clock()))
    assert summary['gone'] == 1 and summary['active'] == 1 and summary['platforms']['ebay']['ok']
    with db.connect() as c:
        assert c.execute('SELECT status FROM items WHERE item_id=?', (iid,)).fetchone()[0] == 'gone'
    hides = outbox(db, 'hide')
    assert [h['payload']['code'] for h in hides] == [code]
    assert hides[0]['payload']['action'] == 'hide' and hides[0]['payload']['reason'] == 'sold'


def test_partial_failure_keeps_success_time(db, cfg):
    first, _ = add(db, cfg, 1)
    second, _ = add(db, cfg, 2)
    third, _ = add(db, cfg, 3)
    db.set(KV_PUBLISHED_OK + 'ebay', '2026-11-03T08:00:00.000Z')
    api = FakeEbay(db, gone={third}, fail={second})
    summary = curio_monitor.check_published(db, api, cfg, sched=Scheduler(db, Clock()))
    assert api.calls == [first, second, third]                # 失败后其余照常判定
    assert summary['errors'] == 1 and summary['active'] == 1 and summary['gone'] == 1
    assert summary['platforms']['ebay'] == {'items': 3, 'ok': False, 'status': 'error'}
    assert ok_at(db, 'ebay') == '2026-11-03T08:00:00.000Z'    # 该平台不更新成功时间
    assert len(outbox(db, 'hide')) == 1


def test_budget_shared_with_status_checks_and_takes_priority(db, cfg):
    budget = Budget(db, {'ebay': 100, 'etsy': 100, 'kleinanzeigen': 100})
    # eBay 当日在售检查只剩 3 次。
    for _ in range(97):
        budget.spend('ebay', 'check')
    ids = [add(db, cfg, n)[0] for n in range(1, 11)]
    api = FakeEbay(db, budget=budget)
    summary = curio_monitor.check_published(db, api, cfg, budget, sched=Scheduler(db, Clock()))
    assert api.calls == ids[:3] and summary['attempted'] == 3 and summary['budget_skipped'] == 7
    assert summary['platforms']['ebay']['ok'] is False and summary['platforms']['ebay']['status'] == 'budget_exhausted'
    assert ok_at(db, 'ebay') is None
    assert budget.check_remaining('ebay') == 0


def test_kleinanzeigen_and_hidden_items_not_checked(db, cfg):
    iid, code = add(db, cfg, 1)
    with db.connect() as c:
        c.execute('UPDATE published SET hidden=1 WHERE code=?', (code,))
    rows = curio_monitor.published_rows(db)
    assert rows == {'ebay': [], 'etsy': []}


def test_unavailable_ebay_client_is_not_success(db, cfg):
    add(db, cfg, 1)
    summary = curio_monitor.check_published(db, None, cfg, sched=Scheduler(db, Clock()))
    assert summary['platforms']['ebay'] == {'items': 1, 'ok': False, 'status': 'unavailable'}
    assert ok_at(db, 'ebay') is None and ok_at(db, 'etsy') is not None


def test_etsy_batch_judgement(db, cfg):
    a, code_a = add(db, cfg, 1, platform='etsy')
    b, _ = add(db, cfg, 2, platform='etsy')
    etsy = Mock()
    verdicts = {a: ('gone', 'Etsy state=sold_out; sale unknown', None), b: ('active', 'Etsy listing active', None)}
    with patch('curio.etsy.judge_listings', return_value=(verdicts, None)) as judge, \
         patch('curio.curio_monitor._apply_etsy', side_effect=lambda store, row, o, e, l, cfg: (
             store.mark_gone(row['item_id']) if o == 'gone' else None) or (o, e)):
        summary = curio_monitor.check_published(db, None, cfg, etsy=etsy, sched=Scheduler(db, Clock()))
    assert judge.call_args[0][1] == [a, b] and etsy.purpose == 'check'
    assert summary['platforms']['etsy']['ok'] and summary['gone'] == 1
    assert outbox(db, 'hide')[0]['payload']['code'] == code_a
    with patch('curio.etsy.judge_listings', return_value=({b: ('skipped', 'batch not fetched', None)}, 'down')):
        db.set(KV_PUBLISHED_OK + 'etsy', 'old')
        summary = curio_monitor.check_published(db, None, cfg, etsy=etsy, sched=Scheduler(db, Clock()))
    assert summary['platforms']['etsy']['ok'] is False and ok_at(db, 'etsy') == 'old'


def test_interval_follows_launchd_wake():
    assert published_check_interval(300) == 15
    assert published_check_interval(1800) == 30
    assert published_check_interval(None) == 15


# ---------- 心跳 ----------

class Beats:
    """本机假服务器：记下每个信封；fail=True 时全部 500。"""

    def __init__(self):
        self.envelopes, self.fail = [], False
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                env = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
                sync.check_envelope(env)
                if outer.fail:
                    return self.reply(500, {'ok': False, 'detail': '内部错误'})
                outer.envelopes.append(env)
                self.reply(200, {'ok': True, 'event_id': env['event_id'], 'received_at': sync.utc_now()})

            def reply(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), H)
        self.url = 'http://127.0.0.1:%d' % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def kinds(self):
        return [e['kind'] for e in self.envelopes]


@pytest.fixture
def beats(root):
    s = Beats()
    (root / '.env').write_text(f'CURIO_SERVER_URL={s.url}\nCURIO_ADMIN_TOKEN={TOKEN}\n')
    yield s
    s.httpd.shutdown()
    s.httpd.server_close()


def test_heartbeat_payload(db, cfg):
    clock = Clock()
    payload = sync.heartbeat_payload(db, clock)
    now = iso_z(clock.ts)
    assert payload == {'schema_version': 1, 'sent_at': now,
                       'platforms': [{'platform': 'ebay', 'last_success_at': now},
                                     {'platform': 'etsy', 'last_success_at': now}]}
    add(db, cfg, 1)                             # eBay 有已发布货、还从未查成功过
    with db.connect() as c:
        c.execute('DELETE FROM kv WHERE k=?', (KV_PUBLISHED_OK + 'ebay',))
    payload = sync.heartbeat_payload(db, clock)
    assert payload['platforms'][0] == {'platform': 'ebay'}
    db.set(KV_PUBLISHED_OK + 'ebay', '2026-11-03T08:00:00.000Z')
    assert sync.heartbeat_payload(db, clock)['platforms'][0]['last_success_at'] == '2026-11-03T08:00:00.000Z'


def test_idle_round_still_sends_heartbeat(db, cfg, root, beats):
    clock = Clock()
    not_due_except_published(db, clock, 5)
    result, *_ = run_with(root, clock, FakeEbay(db))
    assert result['status'] == 'idle' and result['sync_status'] == 'ok' and result['heartbeat_status'] == 'ok'
    assert beats.kinds() == ['heartbeat']
    assert outbox(db) == []                     # 心跳不进发件箱


def test_heartbeat_failure_counts_as_sync_failure(db, root, beats, only_localhost):
    beats.fail = True
    for n in (1, 2, 3):
        r = sync.run(db, root)
        assert r['sync_status'] == 'ok' and r['heartbeat_status'] == 'error' and r['sync_fail_streak'] == n
    assert only_localhost.called                 # 第 3 次通知
    beats.fail = False
    r = sync.run(db, root)
    assert r['heartbeat_status'] == 'ok' and 'sync_fail_streak' not in r and db.get(sync.KV_FAIL_STREAK) is None


def test_heartbeat_skipped_when_server_not_configured(db, root):
    r = sync.run(db, root)
    assert r['sync_status'] == 'disabled' and 'heartbeat_status' not in r


def test_gone_in_round_is_sent_with_heartbeat_same_round(db, cfg, root, beats):
    clock = Clock()
    iid, code = add(db, cfg, 1)
    not_due_except_published(db, clock, 20)
    result, *_ = run_with(root, clock, FakeEbay(db, gone={iid}))
    assert result['published_check']['gone'] == 1 and result['sync_status'] == 'ok'
    assert beats.kinds() == ['hide', 'heartbeat']
    assert beats.envelopes[0]['payload']['code'] == code
    # 该货已隐藏：不再"可能在线"，eBay 心跳报当前时间。
    assert beats.envelopes[1]['payload']['platforms'][0]['last_success_at']


# ---------- 标记售出 ----------

def test_mark_sold_hides_as_sold_and_undo_unhides(db, cfg):
    iid, code = add(db, cfg, 1)
    with db.connect() as c:
        publish.set_rank(c, [code], keep_stale=True)
    r = ranking.pool_action(db, 'mark_sold', iid, 's1')
    assert r['state'] == 'out_of_pool' and r['sold'] and r['left_rank'] and r['hidden_queued']
    with db.connect() as c:
        ev = c.execute("SELECT * FROM triage_events WHERE item_id=? ORDER BY id DESC LIMIT 1", (iid,)).fetchone()
        assert ev['action'] == 'mark_sold' and ev['reason'] == '售出' and ev['new_state'] == 'out_of_pool'
        assert publish.rank(c) == []
        # 墓碑：已售出。
        assert publish._tombstones(c, set(), time.time())[0]['status'] == 'sold'
    hide = outbox(db, 'hide')[-1]['payload']
    assert hide['action'] == 'hide' and hide['reason'] == 'sold' and hide['code'] == code
    pool = ranking.pool(db)
    assert pool['out'][0]['sold'] is True and pool['counts']['out'] == 1
    r = ranking.pool_action(db, 'undo_sold', iid, 'u1')
    assert r['state'] == 'kept' and not r['sold'] and r['hidden_queued']
    assert outbox(db, 'hide')[-1]['payload']['action'] == 'unhide'
    with pytest.raises(ValueError, match='没有标记售出'):
        ranking.pool_action(db, 'undo_sold', iid, 'u2')


def test_mark_sold_only_from_kept_and_plain_out_of_pool_is_removed(db, cfg):
    iid, code = add(db, cfg, 1)
    ranking.pool_action(db, 'out_of_pool', iid, 'o1')
    assert outbox(db, 'hide')[-1]['payload']['reason'] == 'removed'
    with pytest.raises(triage.TriageError):
        ranking.pool_action(db, 'mark_sold', iid, 's1')
    with db.connect() as c:
        assert not triage.is_sold(c, iid)


def test_sold_flag_follows_undo_chain(db, cfg):
    iid, _ = add(db, cfg, 1)
    with db.connect() as c:
        triage.mark_sold(c, iid)
        triage.back_to_pool(c, iid)
        assert not triage.is_sold(c, iid)
        triage.undo(c, iid)                     # 撤销放回 → 回到售出
        assert triage.state(c, iid) == 'out_of_pool' and triage.is_sold(c, iid)
        triage.undo(c, iid)                     # 撤销售出 → 留下
        assert triage.state(c, iid) == 'kept' and not triage.is_sold(c, iid)


def test_platform_gone_hides_as_sold(db, cfg):
    iid, code = add(db, cfg, 1)
    db.mark_gone(iid)
    assert outbox(db, 'hide')[-1]['payload']['reason'] == 'sold'


# ---------- 后台：复查判下架、标记售出后立即发送 ----------

def test_recheck_gone_and_mark_sold_trigger_flush(db, cfg, root):
    import urllib.request
    from curio.review import server
    calls = threading.Semaphore(0)
    httpd = server(db, port=0, root=root, flush=calls.release)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = 'http://127.0.0.1:%d' % httpd.server_port
    try:
        token = re.search("const token='([^']+)'", urllib.request.urlopen(base + '/').read().decode())[1]

        def post(path, body):
            req = urllib.request.Request(base + path, json.dumps(body).encode(),
                                         headers={'Content-Type': 'application/json', 'X-Curio-Token': token})
            with urllib.request.urlopen(req) as r:
                return json.load(r)
        iid, code = add(db, cfg, 1)
        with patch('curio.curio_monitor.recheck_item', return_value={'outcome': 'active', 'evidence': ''}):
            assert post('/api/triage/recheck', {'item_id': iid})['outcome'] == 'active'
        assert not calls.acquire(timeout=0.5)
        with patch('curio.curio_monitor.EbayAPI', return_value=FakeEbay(db, gone={iid})):
            assert post('/api/triage/recheck', {'item_id': iid})['outcome'] == 'gone'
        assert outbox(db, 'hide')[-1]['payload']['code'] == code
        assert calls.acquire(timeout=5)
        other, _ = add(db, cfg, 2)
        d = post('/api/pool/action', {'action': 'mark_sold', 'item_id': other, 'request_id': 'ms1'})
        assert d['sold'] and d['hidden_queued']
        assert calls.acquire(timeout=5)
    finally:
        httpd.shutdown()
        httpd.server_close()


# ---------- 验收：本机演示，真服务器 ----------

def png(w, h, rgb=(40, 80, 160)):
    raw = b''.join(b'\x00' + bytes(rgb) * w for _ in range(h))

    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw, 9)) + chunk(b'IEND', b''))


@pytest.fixture
def live(root):
    if not SERVER_PY.exists():
        pytest.skip('先在 server/ 下 uv sync')
    if shutil.which('sips') is None:
        pytest.skip('需要 macOS 自带 sips')
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    env = dict(os.environ, CURIO_ADMIN_TOKEN=TOKEN, CURIO_SERVER_DB=str(root / 'srv' / 'server.db'),
               CURIO_COOKIE_SECURE='0', CURIO_MAIL_DIR=str(root / 'mail'))
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
        (root / '.env').write_text(f'CURIO_SERVER_URL={url}\nCURIO_ADMIN_TOKEN={TOKEN}\n')
        yield url
    finally:
        proc.terminate()
        proc.wait(timeout=10)


STALE = '部分货品状态可能未更新，下单前请先问墨墨。'


def test_demo_gone_disappears_and_stale_heartbeat_shows_notice(db, cfg, root, live):
    ids = []
    for n in range(1, 13):
        db.ingest(dict(item_id=f'it{n:03d}', platform='ebay', title=f'Meissen Tasse Nr {n}', price=100 + n,
                       currency='EUR', buying='FIXED_PRICE', url=f'https://www.ebay.de/itm/{n}',
                       image=f'https://i.ebayimg.com/images/g/{n}.png'), cfg)
        with db.connect() as c:
            triage.keep(c, f'it{n:03d}')
            ids.append((f'it{n:03d}', triage.code(c, f'it{n:03d}')))
    with db.connect() as c:
        publish.set_rank(c, [code for _, code in ids[:10]])
    publish.publish(db, root, fetch=lambda url: png(800, 600))
    with db.connect() as c:
        sync.enqueue_invite(c, 'MOMO2026')
    assert sync.flush(db, root)['sync_status'] == 'ok'
    s = requests.Session()
    s.headers['Origin'] = live
    assert s.post(live + '/register', allow_redirects=False,
                  data={'invite': 'MOMO2026', 'username': 'wangjie', 'password': 'meissen1710', 'nickname': '王姐',
                        'consent': 'yes', 'next': '/'}).status_code == 303
    gone_iid, gone_code = ids[11]
    assert gone_code in s.get(live + '/').text
    # 发布过、还没收到心跳：提示。
    assert STALE in s.get(live + '/').text
    # 一轮：已发布货检查判这一件下架 → 隐藏与心跳同轮发出。
    clock = Clock()
    not_due_except_published(db, clock, 16)
    result, *_ = run_with(root, clock, FakeEbay(db, gone={gone_iid}))
    assert result['published_check']['gone'] == 1 and result['heartbeat_status'] == 'ok'
    home = s.get(live + '/').text
    assert gone_code not in home and STALE not in home
    item = s.get(live + '/item/' + gone_code).text
    assert '此件已售出。' in item and STALE not in item
    # 心跳时间改到 2 小时 5 分钟前：首页与单件页都提示。
    db.set(KV_PUBLISHED_OK + 'ebay', iso_z(time.time() - 2 * 3600 - 300))
    assert sync.run(db, root)['heartbeat_status'] == 'ok'
    assert STALE in s.get(live + '/').text and STALE in s.get(live + '/item/' + ids[0][1]).text
    # 恢复后提示消失。
    db.set(KV_PUBLISHED_OK + 'ebay', iso_z(time.time() - 20 * 60))
    sync.run(db, root)
    assert STALE not in s.get(live + '/').text


# ---------- 评审修正 ----------

class Tick:
    """假 monotonic：每调用一次前进 step 秒。"""

    def __init__(self, step):
        self.t, self.step = 0.0, step

    def __call__(self):
        self.t += self.step
        return self.t


def test_ebay_deadline_mid_list(db, cfg):
    ids = [add(db, cfg, n)[0] for n in range(1, 6)]
    db.set(KV_PUBLISHED_OK + 'ebay', 'old')
    api = FakeEbay(db)
    # started=100；之后每次检查 +100：第 1、2 件在 240 秒内，第 3 件前超时。
    summary = curio_monitor.check_published(db, api, cfg, sched=Scheduler(db, Clock()), monotonic=Tick(100))
    assert api.calls == ids[:2] and summary['platforms']['ebay']['status'] == 'timeout'
    assert ok_at(db, 'ebay') == 'old'


def test_etsy_deadline_between_batches(db, cfg):
    from curio.etsy import BATCH
    rows = {'ebay': [], 'etsy': [dict(item_id=f'ets:{n}', platform='etsy') for n in range(BATCH + 5)]}
    db.set(KV_PUBLISHED_OK + 'etsy', 'old')
    seen = []

    def judge(api, ids):
        seen.append(len(ids))
        return {i: ('active', 'Etsy listing active', None) for i in ids}, None
    with patch('curio.etsy.judge_listings', side_effect=judge), \
         patch('curio.curio_monitor._apply_etsy', side_effect=lambda store, row, o, e, l, cfg: (o, e)):
        summary = curio_monitor.check_published(db, None, cfg, etsy=Mock(), rows=rows, sched=Scheduler(db, Clock()),
                                                monotonic=Tick(150))
    assert seen == [BATCH] and summary['platforms']['etsy']['status'] == 'timeout'
    assert summary['attempted'] == BATCH and ok_at(db, 'etsy') == 'old'


@pytest.mark.parametrize('verdict,error', [(('unknown', 'Etsy detail not returned', None), None),
                                           (('active', 'Etsy listing active', None), 'boom')])
def test_etsy_unknown_or_error_is_not_success(db, cfg, verdict, error):
    iid, _ = add(db, cfg, 1, platform='etsy')
    db.set(KV_PUBLISHED_OK + 'etsy', 'old')
    with patch('curio.etsy.judge_listings', return_value=({iid: verdict}, error)), \
         patch('curio.curio_monitor._apply_etsy', side_effect=lambda store, row, o, e, l, cfg: (o, e)):
        summary = curio_monitor.check_published(db, None, cfg, etsy=Mock(), sched=Scheduler(db, Clock()))
    assert summary['platforms']['etsy']['ok'] is False and ok_at(db, 'etsy') == 'old'


def test_etsy_budget_exhausted_is_not_success(db, cfg):
    from curio.schedule import BudgetExhausted
    iid, _ = add(db, cfg, 1, platform='etsy')
    with patch('curio.etsy.judge_listings', return_value=({iid: ('skipped', '', None)}, BudgetExhausted('etsy', 'check'))):
        summary = curio_monitor.check_published(db, None, cfg, etsy=Mock(), sched=Scheduler(db, Clock()))
    assert summary['platforms']['etsy'] == {'items': 1, 'ok': False, 'status': 'budget_exhausted'}
    assert summary['budget_skipped'] == 1 and ok_at(db, 'etsy') is None


def run_status(db):
    with db.connect() as c:
        return c.execute('SELECT status FROM collection_runs ORDER BY id DESC LIMIT 1').fetchone()[0]


def test_published_failure_makes_run_partial(db, cfg, root):
    clock = Clock()
    iid, _ = add(db, cfg, 1)
    not_due_except_published(db, clock, 16)
    run_with(root, clock, FakeEbay(db, fail={iid}))
    assert run_status(db) == 'partial'


def test_published_exception_makes_run_partial(db, cfg, root):
    clock = Clock()
    add(db, cfg, 1)
    not_due_except_published(db, clock, 16)
    with patch('curio.curio_monitor.check_published', side_effect=RuntimeError('x')):
        result, *_ = run_with(root, clock, FakeEbay(db))
    assert 'published_check_error' in result and run_status(db) == 'partial'


def test_capacity_warning(db, cfg, root):
    from curio.schedule import published_capacity
    budget = Budget(db, {'ebay': 100, 'etsy': 4500, 'kleinanzeigen': 100})
    assert published_capacity(budget, {'ebay': 0, 'etsy': 10}, 15) == []
    warn = published_capacity(budget, {'ebay': 1, 'etsy': 0}, 15)          # 96 次/天 > 份额 40
    assert len(warn) == 1 and 'ebay' in warn[0] and '96' in warn[0] and '40' in warn[0]
    (root / 'config' / 'settings.yaml').write_text(
        (root / 'config' / 'settings.yaml').read_text() + '\napi_budget:\n  ebay: 100\n')
    clock = Clock()
    add(db, cfg, 1)
    not_due_except_published(db, clock, 16)
    with patch('curio.curio_monitor.log') as log:
        result, *_ = run_with(root, clock, FakeEbay(db))
        assert result['published_check_warning'] and log.warning.call_count == 1
        not_due_except_published(db, clock, 16)
        run_with(root, clock, FakeEbay(db))
        assert log.warning.call_count == 1                                 # 每天只记一次


@pytest.mark.parametrize('content', [b'not a plist', b'<?xml version="1.0"?><plist version="1.0"><array/></plist>',
                                     b'<?xml version="1.0"?><plist version="1.0"><dict><key>StartInterval</key>'
                                     b'<string>x</string></dict></plist>'])
def test_launchd_interval_bad_files(tmp_path, content):
    from curio.schedule import launchd_interval
    path = tmp_path / 'x.plist'
    path.write_bytes(content)
    assert launchd_interval(str(path)) is None


def test_launchd_interval_reads_patched_path(tmp_path, monkeypatch, db):
    import plistlib
    path = tmp_path / 'c.plist'
    path.write_bytes(plistlib.dumps({'StartInterval': 1800}))
    monkeypatch.setattr('curio.schedule.LAUNCHD_PLIST', str(path))
    out = Scheduler(db, Clock()).summary()
    assert out['launchd_interval_min'] == 30 and out['published_check_interval_min'] == 30


def test_heartbeat_capped_by_undelivered_hide(db, cfg):
    iid, code = add(db, cfg, 1)
    add(db, cfg, 2)
    db.mark_gone(iid)                                   # 入队隐藏，尚未送达
    hide_at = outbox(db, 'hide')[0]['created_at']
    db.set(KV_PUBLISHED_OK + 'ebay', '2099-01-01T00:00:00.000Z')
    payload = sync.heartbeat_payload(db, Clock(time.time() + 3600))
    assert all(p['last_success_at'] == hide_at for p in payload['platforms'])
    with db.connect() as c:
        c.execute("UPDATE outbox SET sent_at=? WHERE kind='hide'", (sync.utc_now(),))
    assert sync.heartbeat_payload(db)['platforms'][0]['last_success_at'] == '2099-01-01T00:00:00.000Z'


def test_nothing_online_refreshes_stored_time(db, cfg):
    db.set(KV_PUBLISHED_OK + 'ebay', '2026-01-01T00:00:00.000Z')
    clock = Clock()
    sync.heartbeat_payload(db, clock)
    assert ok_at(db, 'ebay') == iso_z(clock.ts)
    add(db, cfg, 1)                                     # 再次有已发布货：报此刻（不是一月的旧时间）
    assert sync.heartbeat_payload(db, Clock(clock.ts + 60))['platforms'][0]['last_success_at'] == iso_z(clock.ts)


def test_heartbeat_unexpected_error_keeps_flush_result(db, root, beats):
    with patch('curio.sync.heartbeat_payload', side_effect=RuntimeError('x')):
        r = sync.run(db, root)
    assert r['sync_status'] == 'ok' and r['heartbeat_status'] == 'error' and r['sync_fail_streak'] == 1


def test_tombstone_keeps_removed_when_later_gone(db, cfg):
    iid, code = add(db, cfg, 1)
    ranking.pool_action(db, 'out_of_pool', iid, 'o1')
    db.mark_gone(iid)
    assert [h['payload']['reason'] for h in outbox(db, 'hide')] == ['removed']
    with db.connect() as c:
        assert publish._tombstones(c, set(), time.time())[0]['status'] == 'removed'


def test_tombstone_sold_comes_from_hide_reason(db, cfg):
    iid, code = add(db, cfg, 1)
    ranking.pool_action(db, 'mark_sold', iid, 's1')
    with db.connect() as c:
        c.execute('UPDATE triage SET sold=0 WHERE item_id=?', (iid,))     # 只剩隐藏原因
        assert publish._tombstones(c, set(), time.time())[0]['status'] == 'sold'


def test_undo_sale_after_later_human_action_refused(db, cfg):
    iid, _ = add(db, cfg, 1)
    ranking.pool_action(db, 'mark_sold', iid, 's1')
    ranking.pool_action(db, 'back_to_pool', iid, 'b1')
    with pytest.raises(ValueError, match='没有标记售出'):
        ranking.pool_action(db, 'undo_sold', iid, 'u1')
    with db.connect() as c:
        with pytest.raises(triage.TriageError, match='状态已变化'):
            triage.undo(c, iid, actions=('mark_sold',))


def test_restored_sold_when_one_undo_covers_several_events(db, cfg):
    iid, _ = add(db, cfg, 1)
    with db.connect() as c:
        triage.mark_sold(c, iid)
        first = triage.back_to_pool(c, iid)['event']
        c.execute('UPDATE triage_events SET grp=? WHERE id=?', (first, first))
        triage.transition(c, iid, 'mark_sold', grp=first)
        assert triage.is_sold(c, iid)
        triage.undo(c, iid)                     # 一次撤销整组：再次售出 + 放回 → 回到第一次售出
        assert triage.state(c, iid) == 'out_of_pool' and triage.is_sold(c, iid)


def test_kick_reruns_once_and_replay_does_not_kick(db, cfg, root):
    import urllib.request
    from curio.review import server
    gate, calls = threading.Event(), []

    def flush():
        calls.append(1)
        if len(calls) == 1:
            gate.wait(5)
    httpd = server(db, port=0, root=root, flush=flush)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = 'http://127.0.0.1:%d' % httpd.server_port
    try:
        token = re.search("const token='([^']+)'", urllib.request.urlopen(base + '/').read().decode())[1]

        def post(body):
            req = urllib.request.Request(base + '/api/pool/action', json.dumps(body).encode(),
                                         headers={'Content-Type': 'application/json', 'X-Curio-Token': token})
            with urllib.request.urlopen(req) as r:
                return json.load(r)
        a, _ = add(db, cfg, 1)
        b, _ = add(db, cfg, 2)
        post({'action': 'mark_sold', 'item_id': a, 'request_id': 'k1'})
        post({'action': 'mark_sold', 'item_id': b, 'request_id': 'k2'})
        post({'action': 'mark_sold', 'item_id': b, 'request_id': 'k2'})      # 重放：不再触发
        gate.set()
        time.sleep(0.5)
        assert len(calls) == 2
        assert post({'action': 'mark_sold', 'item_id': b, 'request_id': 'k2'})['replayed'] is True
        time.sleep(0.3)
        assert len(calls) == 2
    finally:
        httpd.shutdown()
        httpd.server_close()
