"""故事 15（CAP-15；AD-9、AD-12、AD-25）：行为数据回流——Mac 一侧（行为账本、文件锁、交接文件），以及本机端到端演示。

覆盖 I/O 矩阵的 Mac 部分：拉取入库（seq 1–120 → 写入 120、重算、确认 120、服务器删除）、写入失败不确认、
确认前崩溃（下轮重拉、INSERT OR IGNORE 不重复）、坏事件（存 behavior_unknown 仍确认）、并发（只有一个进程进入拉取段）、
关联（交接文件含该账号全部历史）、更正（两份都重算，无账号的删除）、写文件失败（下次重算）。
只连本机 127.0.0.1（标准库假服务器，或 server/.venv 里的真服务器）；GoodCustomer 一律用临时资料库，交接文件写在临时目录。
"""
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from curio import behavior, guests, sync
from curio.store import Store
from test_guests import (GCDB, REAL_GC, SERVER_PY, TOKEN, _free_port, account, gc,  # noqa: F401（fixtures）
                         never_real_gc, only_localhost, write_customer)

ROOT = Path(__file__).resolve().parents[1]
A = 'a' * 32
B = 'b' * 32
C = 'c' * 32


def event(aid, type_='item_open', code='CO-0012', n=0, **params):
    if not params:
        params = {'from': 'rank'} if type_ == 'item_open' else {'seconds': 45} if type_ == 'item_dwell' else {}
    return {'event_id': str(uuid.uuid4()), 'account_id': aid, 'type': type_,
            'co_code': code if type_ in ('item_open', 'item_dwell') else None,
            'client_ts': '2026-11-03T08:00:00.000Z', 'server_ts': '2026-11-03T08:%02d:%02d.000Z' % (n // 60 % 60, n % 60),
            'params': params, 'event_schema_version': 1}


class FakeInbox:
    """模仿 /admin/inbox 与 /admin/inbox/ack（以及 run 需要的 /admin/events、/admin/accounts）。"""

    def __init__(self):
        self.inbox = []            # [(seq, event)]
        self.seq = 0
        self.accounts = []
        self.ack_status = 200
        self.get_status = 200
        self.acks = []
        self.gets = 0
        self.stats = None          # 设了就随批次返回 stats
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, status, body):
                data = json.dumps(body, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.headers.get('Authorization') != 'Bearer ' + TOKEN:
                    return self.reply(401, {'ok': False})
                u = urlsplit(self.path)
                if u.path == '/admin/accounts':
                    return self.reply(200, {'schema_version': 1, 'accounts': outer.accounts})
                if u.path != '/admin/inbox':
                    return self.reply(404, {})
                outer.gets += 1
                if outer.get_status != 200:
                    return self.reply(outer.get_status, {'ok': False, 'detail': '坏了'})
                q = parse_qs(u.query)
                after, limit = int(q.get('after', ['0'])[0]), int(q.get('limit', ['500'])[0])
                rest = [(s, e) for s, e in outer.inbox if s > after]
                batch = {'schema_version': 1, 'events': [{'inbox_seq': s, 'event': e} for s, e in rest[:limit]],
                         'more': len(rest) > limit}
                if outer.stats is not None:
                    batch['stats'] = outer.stats
                self.reply(200, batch)

            def do_POST(self):
                if self.headers.get('Authorization') != 'Bearer ' + TOKEN:
                    return self.reply(401, {'ok': False})
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if self.path == '/admin/inbox/ack':
                    outer.acks.append(body['max_seq'])
                    if outer.ack_status != 200:
                        return self.reply(outer.ack_status, {'ok': False, 'detail': '确认失败'})
                    before = len(outer.inbox)
                    outer.inbox = [(s, e) for s, e in outer.inbox if s > body['max_seq']]
                    return self.reply(200, {'ok': True, 'max_seq': body['max_seq'], 'deleted': before - len(outer.inbox)})
                if self.path == '/admin/events':
                    return self.reply(200, {'ok': True, 'event_id': body['event_id'], 'received_at': '2026-11-03T08:00:00.000Z'})
                self.reply(404, {})

        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), H)
        self.url = f'http://127.0.0.1:{self.httpd.server_port}'
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def put(self, *events):
        for e in events:
            self.seq += 1
            self.inbox.append((self.seq, e))


@pytest.fixture
def fake():
    f = FakeInbox()
    yield f
    f.httpd.shutdown()
    f.httpd.server_close()


@pytest.fixture
def root(tmp_path, fake):
    (tmp_path / '.env').write_text(f'CURIO_SERVER_URL={fake.url}\nCURIO_ADMIN_TOKEN={TOKEN}\n')
    return tmp_path


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / 'data' / 'curio.db')


def with_accounts(store, *ids):
    with store.connect() as c:
        for aid in ids:
            c.execute('INSERT OR REPLACE INTO guest_accounts(account_id,username,nickname,status,created_at,account_rev,'
                      'pulled_at) VALUES(?,?,?,?,?,?,?)', (aid, 'u' + aid[:3], '客人' + aid[:1], 'active',
                                                           '2026-11-03T08:00:00.000Z', 1, '2026-11-03T08:00:00.000Z'))


def ledger(store):
    with store.connect() as c:
        return [r[0] for r in c.execute('SELECT event_id FROM behavior_events ORDER BY server_ts,event_id')]


def handoff(store, cid):
    p = behavior.handoff_path(store, cid)
    return json.loads(p.read_text(encoding='utf-8')) if p.exists() else None


# ---------- 拉取 → 入账 → 重算 → 确认 ----------

def test_handoff_location_is_next_to_curio_db(store):
    assert behavior.handoff_dir(store) == store.path.parent / 'gc_handoff'
    assert behavior.handoff_path(store, 'GC000123').name == 'GC000123.json'
    with pytest.raises(ValueError):
        behavior.handoff_path(store, '../x')


def test_pull_120_writes_rebuilds_and_acks(store, root, fake, gc):
    with_accounts(store, A)
    guests.link(store, gc, 'link', A, 'l1', 'GC000123')
    fake.put(*[event(A, n=k) for k in range(120)])
    r = behavior.pull(store, root)
    assert r['inbox_status'] == 'ok' and r['inbox_new'] == 120 and r['inbox_acked_seq'] == 120
    assert fake.acks == [120] and fake.inbox == []                     # 服务器删除 1–120
    assert len(ledger(store)) == 120
    h = handoff(store, 'GC000123')
    assert h['summary']['total'] == 120 and h['summary']['by_type']['item_open'] == 120
    assert [e['event_id'] for e in h['events']] == ledger(store)


def test_paging_acks_each_page(store, root, fake, monkeypatch):
    monkeypatch.setattr(behavior, 'PULL_LIMIT', 50)
    fake.put(*[event(A, n=k) for k in range(120)])
    r = behavior.pull(store, root)
    assert fake.acks == [50, 100, 120] and r['inbox_pulled'] == 120 and fake.inbox == []


def test_crash_before_ack_repull_no_duplicates(store, root, fake, gc):
    with_accounts(store, A)
    guests.link(store, gc, 'link', A, 'l1', 'GC000123')
    fake.put(*[event(A, n=k) for k in range(5)])
    fake.ack_status = 500
    r = behavior.pull(store, root)
    assert r['inbox_status'] == 'error' and len(ledger(store)) == 5 and len(fake.inbox) == 5
    assert handoff(store, 'GC000123')['summary']['total'] == 5           # 已入账已重算，只是未确认
    fake.ack_status = 200
    r = behavior.pull(store, root)
    assert r['inbox_status'] == 'ok' and r['inbox_new'] == 0 and r['inbox_duplicate'] == 5
    assert len(ledger(store)) == 5 and fake.inbox == []
    assert handoff(store, 'GC000123')['summary']['total'] == 5


def test_write_failure_does_not_ack(store, root, fake, monkeypatch):
    fake.put(event(A))

    def boom(*a, **k):
        raise sqlite3.OperationalError('disk I/O error')
    monkeypatch.setattr(behavior, 'ingest', boom)
    r = behavior.pull(store, root)
    assert r['inbox_status'] == 'error' and '未能写入' in r['inbox_error']
    assert fake.acks == [] and len(fake.inbox) == 1
    assert store.get(behavior.KV_ERROR)


def test_bad_event_goes_to_unknown_and_is_still_acked(store, root, fake):
    good = event(A)
    weird = dict(event(A, 'share'), note='来自未来版本')
    fake.put(good, weird, {'not': 'an event'})
    r = behavior.pull(store, root)
    assert r['inbox_status'] == 'ok' and r['inbox_new'] == 1 and r['inbox_unknown'] == 2
    assert fake.acks == [3] and fake.inbox == []
    with store.connect() as c:
        raws = [json.loads(x[0]) for x in c.execute('SELECT raw FROM behavior_unknown ORDER BY id')]
    assert raws == [weird, {'not': 'an event'}]                         # 原样
    fake.put(weird)                                                     # 再来一次同一条坏事件：不重复存
    behavior.pull(store, root)
    with store.connect() as c:
        assert c.execute('SELECT count(*) FROM behavior_unknown').fetchone()[0] == 2


def test_bad_batch_is_not_acked(store, root, fake, monkeypatch):
    fake.put(event(A))
    fake.inbox = [(5, fake.inbox[0][1]), (3, event(A))]                 # 序号不是递增
    r = behavior.pull(store, root)
    assert r['inbox_status'] == 'error' and fake.acks == [] and ledger(store) == []
    fake.get_status = 503
    r = behavior.pull(store, root)
    assert r['inbox_status'] == 'error' and '503' in r['inbox_error']


def test_server_unconfigured_is_noop(store, tmp_path):
    assert behavior.pull(store, tmp_path) == {}


def test_concurrent_pull_only_one_enters(store, root, fake):
    fake.put(event(A))
    with behavior.inbox_lock(store) as got:
        assert got is True
        assert behavior.pull(store, root) == {'inbox_status': 'busy'}   # 另一方跳过本轮
        assert fake.gets == 0
    assert behavior.pull(store, root)['inbox_status'] == 'ok'


def test_lock_held_by_another_process(store, root, fake):
    fake.put(event(A))
    code = ('import fcntl,os,sys,time\nfd=os.open(sys.argv[1],os.O_RDWR|os.O_CREAT,0o600)\n'
            'fcntl.flock(fd,fcntl.LOCK_EX)\nprint("locked",flush=True)\nsys.stdin.readline()\n')
    p = subprocess.Popen([sys.executable, '-c', code, str(behavior.lock_path(store))], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == 'locked'
        assert behavior.pull(store, root) == {'inbox_status': 'busy'} and fake.gets == 0
    finally:
        p.stdin.write('\n')
        p.stdin.flush()
        p.wait(timeout=10)
    assert behavior.pull(store, root)['inbox_status'] == 'ok'


def test_run_pulls_inbox_each_round(store, root, fake):
    fake.put(event(A, 'home_open'))
    r = sync.run(store, root)
    assert r['inbox_status'] == 'ok' and r['inbox_new'] == 1 and fake.inbox == []
    fake.get_status = 500
    r = sync.run(store, root)
    assert r['inbox_status'] == 'error' and r['sync_status'] == 'ok'      # 只写结果字段，不算同步失败


def test_full_page_of_500_with_default_limit(store, root, fake):
    fake.put(*[event(A, n=k) for k in range(500)])
    r = behavior.pull(store, root)
    assert r['inbox_status'] == 'ok' and r['inbox_new'] == 500 and fake.acks == [500] and fake.inbox == []


def test_deadline_with_pages_left_reports_more(store, root, fake, monkeypatch):
    monkeypatch.setattr(behavior, 'PULL_LIMIT', 2)
    fake.put(*[event(A, n=k) for k in range(5)])
    clock = lambda: 1000.0 if store.get(behavior.KV_ACKED) else 0.0     # 第一页确认后限时即到
    r = behavior.pull(store, root, monotonic=clock, seconds=10)
    assert r['inbox_status'] == 'ok' and r['inbox_more'] is True and fake.acks == [2]
    r = behavior.pull(store, root)
    assert 'inbox_more' not in r and fake.inbox == []


def test_unexpected_errors_inside_pull_are_recorded(store, root, fake, monkeypatch):
    fake.put(event(A))

    def boom(*a, **k):
        raise OSError('disk full')
    monkeypatch.setattr(behavior, 'rebuild_dirty', boom)
    r = behavior.pull(store, root)
    assert r['inbox_status'] == 'error' and 'OSError' in r['inbox_error']
    assert store.get(behavior.KV_ERROR) == r['inbox_error'] and fake.acks == []
    monkeypatch.undo()
    monkeypatch.setattr(sync, 'load_schema', lambda *a, **k: (_ for _ in ()).throw(sync.ContractError('坏契约')))
    r = behavior.pull(store, root)
    assert r['inbox_status'] == 'error' and store.get(behavior.KV_ERROR)


def test_server_stats_copied_and_growth_logged(store, root, fake, caplog):
    fake.stats = {'expired': 0, 'rate_dropped': 0, 'rejected': 0}
    fake.put(event(A))
    r = behavior.pull(store, root)
    assert r['inbox_server_expired'] == 0 and 'inbox_server_grown' not in r
    fake.stats = {'expired': 7, 'rate_dropped': 0, 'rejected': 2}
    fake.put(event(A))
    with caplog.at_level('WARNING', logger='curio.behavior'):
        r = behavior.pull(store, root)
    assert r['inbox_server_grown'] == {'expired': 7, 'rejected': 2} and 'expired +7' in caplog.text
    fake.put(event(A))
    assert 'inbox_server_grown' not in behavior.pull(store, root)          # 没再增长：不再报


# ---------- 交接文件（关联 / 更正 / 解除） ----------

def ingest(store, *events):
    return behavior.ingest(store, [{'inbox_seq': k + 1, 'event': e} for k, e in enumerate(events)])


def test_unlinked_events_stay_in_ledger_until_linked(store, gc):
    with_accounts(store, A)
    history = [event(A, n=1), event(A, 'item_dwell', n=2), event(A, 'currency', n=3, currency='CNY')]
    ingest(store, *history)
    behavior.rebuild_dirty(store)
    assert not behavior.handoff_dir(store).exists() or not list(behavior.handoff_dir(store).iterdir())
    guests.link(store, gc, 'link', A, 'l1', 'GC000123')
    h = handoff(store, 'GC000123')
    assert [e['event_id'] for e in h['events']] == [e['event_id'] for e in history]
    assert h['customer_id'] == 'GC000123' and h['accounts'][0]['account_id'] == A
    assert h['summary']['by_type'] == {'home_open': 0, 'item_open': 1, 'item_dwell': 1, 'filter': 0, 'currency': 1, 'like': 0, 'want': 0}


def test_relink_rebuilds_both_and_deletes_empty(store, gc):
    with_accounts(store, A, B)
    ingest(store, event(A, n=1), event(B, n=2))
    guests.link(store, gc, 'link', A, 'l1', 'GC000123')
    guests.link(store, gc, 'link', B, 'l2', 'GC000123')
    assert handoff(store, 'GC000123')['summary']['total'] == 2
    guests.link(store, gc, 'relink', A, 'r1', 'GC000456')
    assert [e['account_id'] for e in handoff(store, 'GC000123')['events']] == [B]
    assert [e['account_id'] for e in handoff(store, 'GC000456')['events']] == [A]
    guests.link(store, gc, 'relink', B, 'r2', 'GC000456')
    assert handoff(store, 'GC000123') is None                          # 无账号时文件删除
    assert handoff(store, 'GC000456')['summary']['total'] == 2
    guests.link(store, gc, 'unlink', A, 'u1')
    guests.link(store, gc, 'unlink', B, 'u2')
    assert handoff(store, 'GC000456') is None
    assert len(ledger(store)) == 2                                     # 账本不动


def test_two_accounts_merged_sorted_and_rebuild_is_idempotent(store, gc):
    with_accounts(store, A, B)
    guests.link(store, gc, 'link', A, 'l1', 'GC000123')
    guests.link(store, gc, 'link', B, 'l2', 'GC000123')
    ingest(store, event(B, n=30), event(A, n=10), event(B, 'filter', n=20, brand='Meissen'))
    behavior.rebuild_dirty(store)
    path = behavior.handoff_path(store, 'GC000123')
    first = path.read_bytes()
    assert [e['server_ts'] for e in handoff(store, 'GC000123')['events']] == sorted(
        e['server_ts'] for e in handoff(store, 'GC000123')['events'])
    behavior.rebuild(store, 'GC000123')
    assert path.read_bytes() == first                                  # 整份覆盖写、幂等
    assert not [p for p in path.parent.iterdir() if p.name.endswith('.tmp')]


def test_handoff_write_failure_rebuilt_next_time(store, gc):
    with_accounts(store, A)
    ingest(store, event(A))
    d = behavior.handoff_dir(store)
    d.parent.mkdir(parents=True, exist_ok=True)
    d.write_text('挡住目录')                                           # 写文件失败
    r = guests.link(store, gc, 'link', A, 'l1', 'GC000123')
    assert r['ok'] is True                                             # 关联照常生效
    with store.connect() as c:
        assert c.execute('SELECT customer_id FROM handoff_dirty').fetchall()[0][0] == 'GC000123'
    d.unlink()
    assert behavior.rebuild_dirty(store) == (1, [])
    assert handoff(store, 'GC000123')['summary']['total'] == 1
    with store.connect() as c:
        assert c.execute('SELECT count(*) FROM handoff_dirty').fetchone()[0] == 0


def test_mark_during_rebuild_survives_and_next_rebuild_uses_new_mapping(store, monkeypatch):
    with_accounts(store, A)
    with store.connect() as c:
        c.execute("INSERT INTO guest_links(account_id,customer_id,linked_at) VALUES(?,?,?)", (A, 'GC000123', 'x'))
    ingest(store, event(A))
    real = behavior.rebuild
    calls = []

    def racing(store_, cid):
        out = real(store_, cid)
        if not calls:              # 重算刚写完文件，后台把账号改关联到别处（同一事务标记两个客户）
            with store.connect() as c:
                c.execute("UPDATE guest_links SET customer_id='GC000456' WHERE account_id=?", (A,))
                behavior.mark_dirty(c, 'GC000123', 'GC000456')
        calls.append(cid)
        return out
    monkeypatch.setattr(behavior, 'rebuild', racing)
    behavior.rebuild_dirty(store)
    assert handoff(store, 'GC000123')['summary']['total'] == 1             # 本次算的是旧映射
    with store.connect() as c:
        assert {r[0] for r in c.execute('SELECT customer_id FROM handoff_dirty')} >= {'GC000123'}
    behavior.rebuild_dirty(store)
    assert handoff(store, 'GC000123') is None and handoff(store, 'GC000456')['summary']['total'] == 1
    with store.connect() as c:
        assert c.execute('SELECT count(*) FROM handoff_dirty').fetchone()[0] == 0


def test_create_and_link_rebuilds(store, gc, monkeypatch):
    with_accounts(store, C)
    ingest(store, event(C, 'home_open'))
    monkeypatch.setattr(guests, 'gcdb_new_customer', lambda gc_, name, run=None: 'GC000901')
    guests.create_and_link(store, gc, C, 'c1')
    assert handoff(store, 'GC000901')['summary']['total'] == 1


def test_handoff_does_not_need_gc_library(store, tmp_path):
    """重算只用 curio.db 里的映射与账本，不读 GoodCustomer 库。"""
    with_accounts(store, A)
    with store.connect() as c:
        c.execute("INSERT INTO guest_links(account_id,customer_id,linked_at) VALUES(?,?,?)", (A, 'GC000123', 'x'))
    ingest(store, event(A))
    assert behavior.rebuild_dirty(store) == (1, [])
    assert handoff(store, 'GC000123')['summary']['total'] == 1


# ---------- 契约：Mac 与服务器同一份判断 ----------

def test_check_event_matrix():
    ok = [event(A), event(A, 'item_dwell'), event(A, 'home_open', brand='KPM Berlin', band='lt200'),
          event(A, 'filter'), event(A, 'currency', currency='EUR')]
    bad = [event(A, 'share'), dict(event(A), co_code=None), dict(event(A, 'home_open'), co_code='CO-0001'),
           event(A, 'item_dwell', seconds=1801), event(A, 'currency', currency='USD'),
           dict(event(A), event_id=str(uuid.uuid4()) + '\n'), dict(event(A), extra=1),
           dict(event(A), account_id='x'), dict(event(A), event_schema_version=2)]
    assert [behavior.check_event(e) for e in ok] == [None] * len(ok)
    assert all(behavior.check_event(e) for e in bad)


@pytest.mark.skipif(not SERVER_PY.exists(), reason='先在 server/ 下 uv sync')
def test_event_contract_agrees_with_server_jsonschema():
    events = [event(A), event(A, 'item_dwell'), event(A, 'filter', form='FN-001'), event(A, 'share'),
              dict(event(A), co_code=None), event(A, 'item_dwell', seconds=0), dict(event(A), extra=1),
              event(A, 'currency', currency='CNY'), dict(event(A, 'home_open'), co_code='CO-0001')]
    probe = ('import json,sys\nfrom app import Contract\nimport inbox\n'
             'ck=inbox.Checker(Contract(sys.argv[1]))\nout=[]\n'
             'for e in json.load(sys.stdin):\n'
             '  try:\n    ck.check(e);out.append(True)\n  except inbox.BadEvent:\n    out.append(False)\n'
             'print(json.dumps(out))')
    r = subprocess.run([str(SERVER_PY), '-c', probe, str(ROOT / 'contract' / 'schemas')], cwd=ROOT / 'server',
                       input=json.dumps(events), capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    mac = [behavior.check_event(e) is None for e in events]
    assert json.loads(r.stdout) == mac == [True, True, True, False, False, False, False, True, False]


# ---------- 本机演示：测试服务器 + 临时 GC 库 ----------

@pytest.mark.skipif(not SERVER_PY.exists(), reason='先在 server/ 下 uv sync')
def test_end_to_end_guest_behavior_reaches_handoff(tmp_path, store, gc):
    port = _free_port()
    env = dict(os.environ, CURIO_ADMIN_TOKEN=TOKEN, CURIO_SERVER_DB=str(tmp_path / 'server.db'),
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
        guests.change_invite(store, 'MOMO2026', 'i1')
        assert sync.flush(store, tmp_path)['sync_status'] == 'ok'
        wang = requests.Session()
        wang.headers['Origin'] = url
        r = wang.post(url + '/register', allow_redirects=False,
                      data={'invite': 'MOMO2026', 'username': 'wangjie', 'password': 'meissen1710', 'nickname': '王姐',
                            'consent': 'yes', 'next': '/'})
        assert r.status_code == 303
        assert sync.pull_accounts(store, tmp_path)['accounts_status'] == 'ok'
        [row] = guests.view(store, gc)['pending']
        guests.link(store, gc, 'link', row['account_id'], 'l1', 'GC000123')

        # 客人浏览、点开、停留、筛选、切币种（guest.js 发出的同样载荷；停留走 sendBeacon 的 text/plain）
        def ev(type_, code=None, **params):
            return {'event_id': str(uuid.uuid4()), 'type': type_, 'co_code': code,
                    'client_ts': '2026-11-03T08:00:00.000Z', 'params': params, 'event_schema_version': 1}
        sent = [ev('home_open'), ev('item_open', 'CO-0012', **{'from': 'rank'}), ev('filter', brand='Meissen')]
        r = wang.post(url + '/events', json={'schema_version': 1, 'events': sent})
        assert r.status_code == 200 and r.json()['accepted'] == 3, r.text
        # 币种切换：服务器在 /currency 里记（无脚本表单提交）
        assert wang.post(url + '/currency', data={'currency': 'CNY', 'next': '/'}, allow_redirects=False).status_code == 303
        dwell = ev('item_dwell', 'CO-0012', seconds=45)
        r = wang.post(url + '/events', data=json.dumps({'schema_version': 1, 'events': [dwell]}),
                      headers={'Content-Type': 'text/plain;charset=UTF-8'})
        assert r.json()['accepted'] == 1
        assert wang.post(url + '/events', json={'schema_version': 1, 'events': [dwell]}).json()['duplicates'] == 1
        assert wang.post(url + '/events', json={'schema_version': 1, 'events': [ev('share')]}).status_code == 400
        assert requests.post(url + '/events', json={'schema_version': 1, 'events': [ev('home_open')]}).status_code == 401

        r = sync.run(store, tmp_path)
        assert r['inbox_status'] == 'ok' and r['inbox_new'] == 5, r
        h = handoff(store, 'GC000123')
        assert set(e['event_id'] for e in sent + [dwell]) < set(e['event_id'] for e in h['events'])
        assert [e['params'] for e in h['events'] if e['type'] == 'currency'] == [{'currency': 'CNY'}]
        assert h['summary']['by_type'] == {'home_open': 1, 'item_open': 1, 'item_dwell': 1, 'filter': 1, 'currency': 1, 'like': 0, 'want': 0}
        assert [e for e in h['events'] if e['type'] == 'item_dwell'][0]['params'] == {'seconds': 45}
        inbox = requests.get(url + '/admin/inbox', headers={'Authorization': 'Bearer ' + TOKEN}).json()
        assert inbox['events'] == []                                    # 服务器收件箱为空
        before = behavior.handoff_path(store, 'GC000123').read_bytes()

        r = sync.run(store, tmp_path)                                   # 再运行一轮：无重复
        assert r['inbox_status'] == 'ok' and r['inbox_pulled'] == 0
        assert behavior.handoff_path(store, 'GC000123').read_bytes() == before
        assert len(ledger(store)) == 5

        with sqlite3.connect(tmp_path / 'server.db') as c:
            dump = '\n'.join(c.iterdump())
        assert 'GC000123' not in dump                                   # 映射永不上线
        assert str(REAL_GC) not in str(behavior.handoff_dir(store))     # 不写真实 GoodCustomer 库
        assert not (Path(gc['db']) / 'customers' / 'GC000123' / 'curiobject').exists()
    finally:
        proc.terminate()
        proc.wait(timeout=10)
