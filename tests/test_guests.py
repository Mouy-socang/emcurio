"""故事 14（CAP-14）：账号名单拉取、关联 / 更正 / 解除（只在 curio.db）、新建档案（gcdb）、停用与重置指令
（发件箱、待同步、确认后抹掉明文）、邀请码、后台 06 接口。

只连 127.0.0.1 上临时启动的服务器（标准库假服务器，或 server/.venv 里的真服务器）；GoodCustomer 一律用临时资料库，
从不读写真实 ~/Desktop/goodcustomerDB。
"""
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
import requests

from curio import guests, sync
from curio.config import goodcustomer, load
from curio.store import Store

ROOT = Path(__file__).resolve().parents[1]
TOKEN = 'test-admin-token-SENTINEL'
GCDB = Path('~/Desktop/goodcustomer-plugin/skills/gc-schema/scripts/gcdb.py').expanduser()
SERVER_PY = ROOT / 'server' / '.venv' / 'bin' / 'python'
REAL_GC = Path('~/Desktop/goodcustomerDB').expanduser().resolve()
_real_request = requests.sessions.Session.request
WANG = 'a' * 32
LILY = 'b' * 32


@pytest.fixture(autouse=True)
def only_localhost():
    def guard(self, method, url, *a, **kw):
        assert urlsplit(url).hostname == '127.0.0.1', '测试不得连接外部主机'
        return _real_request(self, method, url, *a, **kw)
    with patch('requests.sessions.Session.request', guard), patch('curio.sync._notify'):
        yield


@pytest.fixture(autouse=True)
def never_real_gc(monkeypatch):
    """任何 gcdb 调用都不得指向真实资料库。"""
    real = subprocess.run

    def run(cmd, *a, **kw):
        if '--db' in cmd:
            assert Path(cmd[cmd.index('--db') + 1]).resolve() != REAL_GC, '测试不得写真实 GoodCustomer 库'
        return real(cmd, *a, **kw)
    monkeypatch.setattr(subprocess, 'run', run)


# ---------- 临时 GoodCustomer 资料库 ----------

def write_customer(db, root, cid, name, aliases=(), status='active', accounts=()):
    d = Path(db) / root / cid
    d.mkdir(parents=True, exist_ok=True)
    (d / 'customer.json').write_text(json.dumps({
        'customer_id': cid, 'display_name': name, 'aliases': list(aliases), 'status': status,
        'platform_accounts': list(accounts), 'schema_version': 'gc_schema_v3_2_local'}, ensure_ascii=False))


@pytest.fixture
def gc(tmp_path):
    db = tmp_path / 'goodcustomerDB'
    (db / '_system').mkdir(parents=True)
    (db / '_system' / 'db.json').write_text('{"schema_version": "gc_schema_v3_2_local"}')
    (db / '_system' / 'counters.json').write_text('{"GC": 900}')
    for name in ('identity_index.json', 'fingerprints.json', 'file_hashes.json'):
        (db / '_system' / name).write_text('{}')
    (db / 'config.json').write_text('{"owner": "墨墨"}')
    write_customer(db, 'customers', 'GC000123', '王姐', aliases=['Wang Jie'])
    write_customer(db, 'customers', 'GC000456', '王姐姐')
    write_customer(db, 'customers', 'GC000777', '王姐（旧）', status='merged')
    write_customer(db, 'customers', 'GC000888', '王姐归档', status='archived')
    write_customer(db, '潜在客户', 'GC000150', '新加坡客人',
                   accounts=[{'platform': 'wechat', 'handle': 'lily_wx', 'display_name': 'Lily'}])
    return {'db': str(db), 'gcdb': str(GCDB)}


def gc_digest(gc):
    """资料库里已有文件的内容摘要（新建档案只能新增文件，不能改已有档案）。"""
    out = {}
    for p in sorted(Path(gc['db']).rglob('customer.json')):
        out[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


# ---------- 假服务器（/admin/accounts + /admin/events） ----------

class Fake:
    def __init__(self):
        self.accounts = []
        self.status = 200
        self.body = None              # 覆盖名单响应
        self.not_applied = set()      # 这些 command_id 回 applied: false
        self.post_status = None       # 设了就让 POST /admin/events 返回这个状态（不存下）
        self.events = {}
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
                if self.path == '/admin/accounts':
                    if outer.status != 200:
                        return self.reply(outer.status, {'ok': False, 'detail': '坏了'})
                    return self.reply(200, outer.body or {'schema_version': 1, 'accounts': outer.accounts})
                if self.path == '/healthz':
                    return self.reply(200, {'status': 'ok'})
                self.reply(404, {})

            def do_POST(self):
                if self.headers.get('Authorization') != 'Bearer ' + TOKEN:
                    return self.reply(401, {'ok': False})
                env = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if outer.post_status:
                    return self.reply(outer.post_status, {'ok': False, 'detail': '假服务器设定的失败'})
                try:
                    sync.check_envelope(env)
                except sync.ContractError as e:
                    return self.reply(400, {'ok': False, 'detail': str(e)})
                outer.events[env['event_id']] = env
                ack = {'ok': True, 'event_id': env['event_id'], 'received_at': '2026-11-03T08:00:00.000Z'}
                if env['kind'] in ('set_invite_code', 'disable_account', 'reset_password'):
                    ack['applied'] = env['payload']['command_id'] not in outer.not_applied
                self.reply(200, ack)

        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), H)
        self.url = f'http://127.0.0.1:{self.httpd.server_port}'
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


def account(aid, username, nickname, status='active', rev=1, created='2026-11-03T08:00:00.000Z'):
    return {'account_id': aid, 'username': username, 'nickname': nickname, 'status': status,
            'created_at': created, 'account_rev': rev}


@pytest.fixture
def fake():
    f = Fake()
    yield f
    f.httpd.shutdown()
    f.httpd.server_close()


@pytest.fixture
def root(tmp_path, fake):
    (tmp_path / '.env').write_text(f'CURIO_SERVER_URL={fake.url}\nCURIO_ADMIN_TOKEN={TOKEN}\n')
    return tmp_path


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / 'curio.db')


def pulled(store, root, fake, *accts):
    fake.accounts = list(accts)
    r = sync.pull_accounts(store, root)
    assert r['accounts_status'] == 'ok', r
    return r


def outbox(store):
    with store.connect() as c:
        return [dict(r) for r in c.execute('SELECT * FROM outbox ORDER BY seq')]


def log_rows(store):
    with store.connect() as c:
        return [dict(r) for r in c.execute('SELECT account_id,action,before_id,after_id,request_id FROM guest_link_log ORDER BY id')]


def names(view, key):
    return [r['username'] for r in view[key]]


def by(view, key, username):
    [row] = [r for r in view[key] if r['username'] == username]
    return row


# ---------- 设置 ----------

def test_goodcustomer_settings_default_and_validation(tmp_path):
    cfg = load()['goodcustomer']
    assert cfg['db'] == str(Path('~/Desktop/goodcustomerDB').expanduser())
    assert cfg['gcdb'].endswith('gc-schema/scripts/gcdb.py') and '~' not in cfg['gcdb']
    assert goodcustomer(None)['db'].endswith('goodcustomerDB')
    assert goodcustomer({'db': '~/x'})['db'] == str(Path('~/x').expanduser())
    for bad in ('x', {'db': ''}, {'db': 3}, {'other': 'x'}):
        with pytest.raises(ValueError):
            goodcustomer(bad)


# ---------- 名单拉取 ----------

def test_new_registration_appears_in_pending(store, root, fake, gc):
    assert guests.view(store, gc)['count'] == 0
    pulled(store, root, fake, account(WANG, 'wangjie', '王姐'))
    v = guests.view(store, gc)
    assert v['count'] == 1 and names(v, 'pending') == ['wangjie'] and v['pending'][0]['nickname'] == '王姐'
    assert v['pulled_at'] and v['pull_error'] is None and v['gc'] == {'available': True}


def test_pull_failure_keeps_last_list(store, root, fake, gc):
    pulled(store, root, fake, account(WANG, 'wangjie', '王姐'))
    fake.status = 500
    r = sync.pull_accounts(store, root)
    assert r['accounts_status'] == 'error' and '500' in r['accounts_error']
    v = guests.view(store, gc)
    assert names(v, 'pending') == ['wangjie'] and '500' in v['pull_error']


def test_pull_rejects_extra_fields_and_writes_nothing(store, root, fake):
    fake.body = {'schema_version': 1, 'accounts': [dict(account(WANG, 'wangjie', '王姐'), email='w@example.com')]}
    r = sync.pull_accounts(store, root)
    assert r['accounts_status'] == 'error' and '不合契约' in r['accounts_error']
    with store.connect() as c:
        assert c.execute('SELECT count(*) FROM guest_accounts').fetchone()[0] == 0


def test_pull_overwrites_by_account_and_never_deletes(store, root, fake, gc):
    pulled(store, root, fake, account(WANG, 'wangjie', '王姐'), account(LILY, 'lily_sg', 'Lily'))
    pulled(store, root, fake, account(WANG, 'wangjie', '王姐（改）', rev=3))
    v = guests.view(store, gc)
    assert sorted(names(v, 'pending')) == ['lily_sg', 'wangjie']           # 服务器名单里没有的不删
    assert [r['nickname'] for r in v['pending'] if r['username'] == 'wangjie'] == ['王姐（改）']
    pulled(store, root, fake, account(WANG, 'wangjie', '旧名', rev=2))       # 晚到的旧名单不覆盖
    assert [r['nickname'] for r in guests.view(store, gc)['pending'] if r['username'] == 'wangjie'] == ['王姐（改）']


def test_unconfigured_server_pull_is_noop(store, tmp_path):
    assert sync.pull_accounts(store, tmp_path) == {}


def test_run_pulls_accounts_each_round(store, root, fake):
    fake.accounts = [account(WANG, 'wangjie', '王姐')]
    with patch('curio.sync.send_heartbeat', return_value=({'heartbeat_status': 'ok'}, False)):
        r = sync.run(store, root)
        assert r['sync_status'] == 'ok' and r['accounts_status'] == 'ok' and r['accounts_total'] == 1
        fake.status = 500
        r = sync.run(store, root)
    assert r['sync_status'] == 'ok' and r['accounts_status'] == 'error' and 'sync_fail_streak' not in r


# ---------- 搜索与关联 ----------

def test_search_by_name_alias_wechat_and_id_skips_merged_and_archived(gc):
    assert [c['customer_id'] for c in guests.search(gc, '王姐')] == ['GC000123', 'GC000456']
    assert [c['customer_id'] for c in guests.search(gc, 'wang jie')] == ['GC000123']
    assert [c['customer_id'] for c in guests.search(gc, 'Lily')] == ['GC000150']
    assert [c['customer_id'] for c in guests.search(gc, 'gc000456')] == ['GC000456']
    assert guests.search(gc, '  ') == [] and guests.search(gc, '没有这个人') == []


def test_link_existing_moves_row_and_logs(store, root, fake, gc):
    pulled(store, root, fake, account(WANG, 'wangjie', '王姐'))
    before = gc_digest(gc)
    r = guests.link(store, gc, 'link', WANG, 'r1', 'GC000123')
    assert r['display_name'] == '王姐' and r['customer_id'] == 'GC000123'
    v = guests.view(store, gc)
    assert v['count'] == 0 and names(v, 'linked') == ['wangjie']
    assert v['linked'][0]['customer_id'] == 'GC000123' and v['linked'][0]['customer_name'] == '王姐'
    assert log_rows(store) == [{'account_id': WANG, 'action': 'link', 'before_id': None, 'after_id': 'GC000123',
                                'request_id': 'r1'}]
    assert gc_digest(gc) == before                                        # GoodCustomer 只读
    assert not outbox(store)                                              # 关联不经服务器


def test_link_replay_and_conflicts(store, root, fake, gc):
    pulled(store, root, fake, account(WANG, 'wangjie', '王姐'))
    first = guests.link(store, gc, 'link', WANG, 'r1', 'GC000123')
    again = guests.link(store, gc, 'link', WANG, 'r1', 'GC000123')
    assert again == dict(first, replayed=True) and len(log_rows(store)) == 1
    with pytest.raises(guests.GuestError, match='重复请求内容不同'):
        guests.link(store, gc, 'link', WANG, 'r1', 'GC000456')
    with pytest.raises(guests.GuestError, match='已关联到 GC000123'):
        guests.link(store, gc, 'link', WANG, 'r2', 'GC000456')
    for cid in ('GC000777', 'GC000888', 'GC999999', 'X'):
        with pytest.raises(guests.GuestError):
            guests.link(store, gc, 'relink', WANG, 'r-' + cid, cid)
    with pytest.raises(guests.GuestError, match='找不到这个账号'):
        guests.link(store, gc, 'link', LILY, 'r3', 'GC000456')


def test_relink_and_unlink_log_before_and_after(store, root, fake, gc):
    pulled(store, root, fake, account(WANG, 'wangjie', '王姐'))
    guests.link(store, gc, 'link', WANG, 'r1', 'GC000123')
    r = guests.link(store, gc, 'relink', WANG, 'r2', 'GC000456')
    assert r['before'] == 'GC000123' and r['customer_id'] == 'GC000456'
    assert guests.view(store, gc)['linked'][0]['customer_id'] == 'GC000456'
    with pytest.raises(guests.GuestError, match='已关联到 GC000456'):
        guests.link(store, gc, 'relink', WANG, 'r3', 'GC000456')
    guests.link(store, gc, 'unlink', WANG, 'r4')
    v = guests.view(store, gc)
    assert names(v, 'pending') == ['wangjie'] and v['count'] == 1 and not v['linked']
    assert [(r['action'], r['before_id'], r['after_id']) for r in log_rows(store)] == [
        ('link', None, 'GC000123'), ('relink', 'GC000123', 'GC000456'), ('unlink', 'GC000456', None)]
    with pytest.raises(guests.GuestError, match='还没有关联'):
        guests.link(store, gc, 'unlink', WANG, 'r5')


@pytest.mark.skipif(not GCDB.is_file(), reason='本机没有 GoodCustomer 插件的 gcdb.py')
def test_create_profile_with_gcdb_assigns_number_and_links(store, root, fake, gc):
    pulled(store, root, fake, account(LILY, 'lily_sg', 'Lily'))
    before = gc_digest(gc)
    r = guests.create_and_link(store, gc, LILY, 'c1')
    assert r['customer_id'] == 'GC000901' and r['created'] is True
    created = Path(gc['db']) / '潜在客户' / 'GC000901' / 'customer.json'
    assert json.loads(created.read_text())['display_name'] == 'Lily'
    after = gc_digest(gc)
    assert {k: v for k, v in after.items() if k in before} == before   # 已有档案一个字节都没改
    v = guests.view(store, gc)
    assert names(v, 'linked') == ['lily_sg'] and v['linked'][0]['customer_id'] == 'GC000901'
    assert log_rows(store)[-1]['action'] == 'link' and log_rows(store)[-1]['after_id'] == 'GC000901'
    assert guests.create_and_link(store, gc, LILY, 'c1')['replayed'] is True       # 重放不再新建
    assert not (Path(gc['db']) / '潜在客户' / 'GC000902').exists()


def test_gcdb_failure_does_not_link(store, root, fake, gc):
    pulled(store, root, fake, account(LILY, 'lily_sg', 'Lily'))
    seen = []

    def failing(cmd, **kw):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, '', '错误：GC 编号已用完\n')
    with pytest.raises(guests.GuestError, match='未能新建档案：错误：GC 编号已用完'):
        guests.create_and_link(store, gc, LILY, 'c1', run=failing)
    assert seen[0][2:] == ['--db', gc['db'], 'new-customer', '--name=Lily']
    v = guests.view(store, gc)
    assert names(v, 'pending') == ['lily_sg'] and not log_rows(store)
    ok = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, 'not-an-id\n', '')
    with pytest.raises(guests.GuestError, match='档案可能已新建，请先在 GoodCustomer 搜索确认'):
        guests.create_and_link(store, gc, LILY, 'c2', run=ok)


def test_create_link_failure_records_new_id_and_retry_reuses_it(store, root, fake, gc, monkeypatch):
    pulled(store, root, fake, account(LILY, 'lily_sg', 'Lily'))
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, 'GC000901\n', '')
    real_log = guests._log
    monkeypatch.setattr(guests, '_log', lambda *a: (_ for _ in ()).throw(sqlite3.OperationalError('database is locked')))
    with pytest.raises(guests.GuestError, match='GC000901'):
        guests.create_and_link(store, gc, LILY, 'c1', run=run)
    with store.connect() as c:
        assert c.execute('SELECT count(*) FROM guest_links').fetchone()[0] == 0
    assert store.get('gc_created:c1') == 'GC000901' and len(calls) == 1
    monkeypatch.setattr(guests, '_log', real_log)
    r = guests.create_and_link(store, gc, LILY, 'c1', run=run)
    assert r['customer_id'] == 'GC000901' and len(calls) == 1                # 重试不再调用 gcdb
    assert guests.view(store, gc)['linked'][0]['customer_id'] == 'GC000901'


def test_create_retry_after_account_linked_elsewhere_names_both(store, root, fake, gc, monkeypatch):
    pulled(store, root, fake, account(LILY, 'lily_sg', 'Lily'))
    store.set('gc_created:c1', 'GC000901')                                   # 上一次已新建、未关联
    guests.link(store, gc, 'link', LILY, 'l1', 'GC000123')
    with pytest.raises(guests.GuestError, match='已关联到 GC000123；已新建的档案 GC000901 未关联'):
        guests.create_and_link(store, gc, LILY, 'c1', run=lambda *a, **k: pytest.fail('不应再调用 gcdb'))


def test_non_list_aliases_and_accounts_are_ignored(gc):
    write_customer(gc['db'], 'customers', 'GC000999', '赵姐')
    p = Path(gc['db']) / 'customers' / 'GC000999' / 'customer.json'
    data = json.loads(p.read_text())
    data['aliases'] = 'Zhao'
    data['platform_accounts'] = 5
    p.write_text(json.dumps(data, ensure_ascii=False))
    guests._gc_invalidate(gc)
    assert [c['customer_id'] for c in guests.search(gc, '赵姐')] == ['GC000999']
    assert guests.search(gc, 'h') == []                                      # 字符串别名不被拆成单字


def test_gc_customers_cached_until_ttl_or_create(gc, monkeypatch):
    first = guests.gc_customers(gc)
    write_customer(gc['db'], 'customers', 'GC000998', '新档案')
    assert guests.gc_customers(gc) == first                                  # 30 秒内用缓存
    guests._gc_invalidate(gc)
    assert 'GC000998' in [c['customer_id'] for c in guests.gc_customers(gc)]


def test_missing_gc_library_still_lists_accounts(store, root, fake, tmp_path):
    gone = {'db': str(tmp_path / 'nowhere'), 'gcdb': str(tmp_path / 'gcdb.py')}
    pulled(store, root, fake, account(WANG, 'wangjie', '王姐'))
    v = guests.view(store, gone)
    assert v['gc'] == {'available': False} and names(v, 'pending') == ['wangjie']
    for call in (lambda: guests.search(gone, '王姐'),
                 lambda: guests.link(store, gone, 'link', WANG, 'r1', 'GC000123'),
                 lambda: guests.create_and_link(store, gone, WANG, 'r2')):
        with pytest.raises(guests.GuestError, match='找不到 GoodCustomer 资料库'):
            call()


# ---------- 停用 / 重置密码 ----------

def test_disable_pending_then_confirmed(store, root, fake, gc):
    pulled(store, root, fake, account(WANG, 'wangjie', '王姐'))
    guests.link(store, gc, 'link', WANG, 'r0', 'GC000123')
    r = guests.command(store, 'disable_account', WANG, 'd1')
    assert r['state'] == 'pending' and 'temp_password' not in r
    v = guests.view(store, gc)
    assert names(v, 'linked') == ['wangjie'] and v['linked'][0]['disable']['state'] == 'pending'
    [row] = outbox(store)
    p = json.loads(row['payload'])
    assert row['kind'] == 'disable_account' and p['account_id'] == WANG and p['target_seq'] == 1
    assert p['command_id'] == r['command_id'] and store.get('cmd_seq:account:' + WANG) == '1'
    with pytest.raises(guests.GuestError, match='停用还在待同步'):
        guests.command(store, 'disable_account', WANG, 'd2')
    assert sync.flush(store, root)['sync_status'] == 'ok'
    v = guests.view(store, gc)
    assert names(v, 'disabled') == ['wangjie'] and not v['linked'] and v['disabled'][0]['customer_id'] == 'GC000123'
    with pytest.raises(guests.GuestError, match='此账号已停用'):
        guests.command(store, 'disable_account', WANG, 'd3')
    with pytest.raises(guests.GuestError, match='不提供重置'):
        guests.command(store, 'reset_password', WANG, 'd4')
    assert guests.command(store, 'disable_account', WANG, 'd1')['replayed'] is True


def test_disable_from_pending_list_counts_until_confirmed(store, root, fake, gc):
    pulled(store, root, fake, account(WANG, 'wangjie', '王姐'))
    guests.command(store, 'disable_account', WANG, 'd1')
    assert guests.view(store, gc)['count'] == 1                          # 待同步期间仍在待关联
    sync.flush(store, root)
    assert guests.view(store, gc)['count'] == 0


def test_server_disabled_status_from_pull_lists_as_disabled(store, root, fake, gc):
    pulled(store, root, fake, account(WANG, 'wangjie', '王姐', status='disabled', rev=2))
    v = guests.view(store, gc)
    assert names(v, 'disabled') == ['wangjie'] and v['count'] == 0


def test_reset_password_temp_shown_once_and_plaintext_erased_after_ack(store, root, fake, gc, capsys):
    pulled(store, root, fake, account(WANG, 'wangjie', '王姐'))
    r = guests.command(store, 'reset_password', WANG, 'p1')
    temp = r['temp_password']
    assert re.fullmatch(r'[A-HJ-NP-Za-km-np-z2-9]{12}', temp)
    with store.connect() as c:
        assert temp not in json.dumps([dict(x) for x in c.execute('SELECT * FROM guest_requests')], ensure_ascii=False)
    assert json.loads(outbox(store)[0]['payload'])['temp_password'] == temp   # 只在发件箱里待到确认
    from scripts import outbox as outbox_script
    outbox_script.main(['--db', str(store.path), 'list'])
    printed = capsys.readouterr().out
    assert temp not in printed and '重置密码 aaaaaaaa…（序号 1）' in printed
    replay = guests.command(store, 'reset_password', WANG, 'p1')
    assert replay['replayed'] is True and replay['password_lost'] is True and 'temp_password' not in replay
    with pytest.raises(guests.GuestError, match='上一次重置还在待同步'):
        guests.command(store, 'reset_password', WANG, 'p2')
    assert guests.view(store, gc)['pending'][0]['reset']['state'] == 'pending'
    assert sync.flush(store, root)['sync_status'] == 'ok'
    [sent] = fake.events.values()
    assert sent['payload']['temp_password'] == temp
    assert json.loads(outbox(store)[0]['payload'])['temp_password'] == sync.ERASED
    with sqlite3.connect(store.path) as c:
        assert temp not in '\n'.join(c.iterdump())
    v = guests.view(store, gc)
    assert v['pending'][0]['reset']['state'] == 'done' and v['pending'][0]['reset']['command_id'] == r['command_id']
    # 停用与重置共用按账号的序号
    guests.command(store, 'disable_account', WANG, 'p3')
    assert json.loads(outbox(store)[1]['payload'])['target_seq'] == 2


def test_not_applied_command_is_failed_and_secret_erased(store, root, fake, gc):
    pulled(store, root, fake, account(WANG, 'wangjie', '王姐'))
    r = guests.command(store, 'reset_password', WANG, 'p1')
    fake.not_applied.add(r['command_id'])
    result = sync.flush(store, root)
    assert result['sync_status'] == 'blocked' and result['sync_error'] == sync.ACCOUNT_NOT_APPLIED
    row = outbox(store)[0]
    assert row['failed_at'] and json.loads(row['payload'])['temp_password'] == sync.ERASED
    v = guests.view(store, gc)
    assert v['pending'][0]['reset']['state'] == 'failed' and v['pending'][0]['reset']['error'] == sync.ACCOUNT_NOT_APPLIED
    assert sync.flush(store, root)['sync_status'] == 'blocked'           # 不自动重发第二次
    for kind in ('disable_account', 'reset_password'):
        with pytest.raises(guests.GuestError, match='上一条指令未同步，请先用 scripts/outbox.py 处理'):
            guests.command(store, kind, WANG, 'again-' + kind)
    from scripts import outbox as outbox_script
    lines = []
    assert outbox_script.main(['--db', str(store.path), 'requeue', str(outbox(store)[0]['seq'])], out=lines.append) == 1
    assert '不能重发' in lines[0] and outbox(store)[0]['failed_at']


def test_transient_failure_keeps_temp_password_and_resends_it(store, root, fake, gc):
    pulled(store, root, fake, account(WANG, 'wangjie', '王姐'))
    temp = guests.command(store, 'reset_password', WANG, 'p1')['temp_password']
    fake.post_status = 503
    assert sync.flush(store, root)['sync_status'] == 'error'
    assert json.loads(outbox(store)[0]['payload'])['temp_password'] == temp
    fake.post_status = None
    assert sync.flush(store, root)['sync_status'] == 'ok'
    [sent] = fake.events.values()
    assert sent['payload']['temp_password'] == temp
    assert json.loads(outbox(store)[0]['payload'])['temp_password'] == sync.ERASED


def test_permanent_rejection_erases_temp_password(store, root, fake, gc):
    pulled(store, root, fake, account(WANG, 'wangjie', '王姐'))
    temp = guests.command(store, 'reset_password', WANG, 'p1')['temp_password']
    fake.post_status = 400
    assert sync.flush(store, root)['sync_status'] == 'blocked'
    row = outbox(store)[0]
    assert row['failed_at'] and json.loads(row['payload'])['temp_password'] == sync.ERASED
    with sqlite3.connect(store.path) as c:
        assert temp not in '\n'.join(c.iterdump())


def test_bad_account_command_rejected(store):
    with pytest.raises(guests.GuestError):
        guests.command(store, 'disable_account', 'nope', 'x1')
    with pytest.raises(guests.GuestError, match='未知操作'):
        guests.command(store, 'delete_account', WANG, 'x2')
    with store.connect() as c, pytest.raises(sync.ContractError):
        sync.enqueue_account_command(c, 'disable_account', 'A' * 32)


@pytest.mark.skipif(not SERVER_PY.exists(), reason='先在 server/ 下 uv sync')
def test_account_command_contracts_agree_with_server_jsonschema():
    """Mac 最小校验器与服务器 jsonschema（经 server/.venv）对两种指令与账号名单的判断一致。"""
    def env(kind, **payload):
        base = {'schema_version': 1, 'command_id': str(uuid.uuid4()), 'target_seq': 1, 'account_id': WANG}
        return {'schema_version': 1, 'event_id': str(uuid.uuid4()), 'kind': kind,
                'created_at': '2026-11-03T08:00:00.000Z', 'payload': dict(base, **payload)}
    temp = sync.new_temp_password()
    envs = [env('disable_account'), env('reset_password', temp_password=temp),
            env('disable_account', account_id='X'), env('disable_account', target_seq=0),
            env('reset_password', temp_password='Hk7mPq2Rx9T0'), env('reset_password'),
            env('disable_account', temp_password=temp)]
    lists = [{'schema_version': 1, 'accounts': [account(WANG, 'wangjie', '王姐')]},
             {'schema_version': 1, 'accounts': [dict(account(WANG, 'wangjie', '王姐'), email='x@y.z')]},
             {'schema_version': 1, 'accounts': [account(WANG, 'wangjie', '王姐', status='gone')]}]
    probe = ('import json,sys\n'
             'import app\n'
             'c=app.Contract(app.DEFAULT_CONTRACT)\n'
             'data=json.load(sys.stdin)\n'
             'out=[]\n'
             'for e in data["envs"]:\n'
             '    try:c.check(e);out.append(True)\n'
             '    except app.BadPayload:out.append(False)\n'
             'v=c.validator("accounts_list",1)\n'
             'out+=[v.is_valid(x) for x in data["lists"]]\n'
             'print(json.dumps(out))\n')
    r = subprocess.run([str(SERVER_PY), '-c', probe], cwd=ROOT / 'server', input=json.dumps({'envs': envs, 'lists': lists}),
                       capture_output=True, text=True, timeout=60, check=True)
    server_ok = json.loads(r.stdout)
    mac_ok = []
    for e in envs:
        try:
            sync.check_envelope(e)
            mac_ok.append(True)
        except sync.ContractError:
            mac_ok.append(False)
    schema = sync.load_schema('accounts_list', 1)
    mac_ok += [not sync.validate(x, schema) for x in lists]
    assert mac_ok == server_ok == [True, True, False, False, False, False, False, True, False, False]
    assert not set(sync.TEMP_ALPHABET) & set('0O1lIo') and len(temp) == 12


# ---------- 邀请码 ----------

def test_invite_pending_then_current(store, root, fake):
    with store.connect() as c:
        assert guests.invite_state(c) == {'current': None, 'pending': None}
    guests.change_invite(store, 'MOMO2026', 'i1')
    sync.flush(store, root)
    r = guests.change_invite(store, 'ABCD2026', 'i2')
    assert r['invite'] == {'current': {'code': 'MOMO2026', 'at': r['invite']['current']['at']},
                           'pending': {'code': 'ABCD2026', 'state': 'pending'}}   # 待同步期间旧码仍显示为当前
    sync.flush(store, root)
    with store.connect() as c:
        assert guests.invite_state(c)['current']['code'] == 'ABCD2026' and guests.invite_state(c)['pending'] is None
    with pytest.raises(guests.GuestError, match='4–32 位'):
        guests.change_invite(store, 'AB', 'i3')
    for bad in (None, 123, ['MOMO']):
        with pytest.raises(guests.GuestError, match='邀请码无效'):
            guests.change_invite(store, bad, 'i4')


# ---------- 后台接口（/api/guests…） ----------

@pytest.fixture
def http(store, root, gc):
    from curio.review import server
    kicked = []
    pulls = []

    def pull():
        pulls.append(1)
        return sync.pull_accounts(store, root)
    def gcdb_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, 'GC000901\n', '')
    httpd = server(store, port=0, root=root, flush=lambda: kicked.append(1), gc=gc, pull=pull, gcdb_run=gcdb_run)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = 'http://127.0.0.1:' + str(httpd.server_port)
    html = urllib.request.urlopen(base + '/').read().decode()
    token = re.search("const token='([^']+)'", html)[1]

    def get(path):
        try:
            with urllib.request.urlopen(base + path) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def post(path, body):
        req = urllib.request.Request(base + path, json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json', 'X-Curio-Token': token})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)
    yield dict(html=html, get=get, post=post, kicked=kicked, pulls=pulls, base=base)
    httpd.shutdown()
    httpd.server_close()


def test_backoffice_page_has_guest_section_and_dialog(http):
    html = http['html']
    assert '<a class="tc" id="tab06" href="/#guests">' in html and 'id="cGuests"' in html
    assert 'id="guestsView"' in html and '暂无待关联的新注册' in html and '按微信名或用户名搜' in html
    assert 'role="alertdialog" aria-modal="true" aria-labelledby="cdT" aria-describedby="cdD"' in html
    js = urllib.request.urlopen(http['base'] + '/triage.js').read().decode()
    assert 'window.curioGuests' in js and '没有找到 ' in js and '未保存' in js and '待同步' in js
    assert 'confirm(' not in js.replace('confirmDlg(', '').replace('confirmUnlink(', '').replace(
        'confirmDisable(', '').replace('confirmReset(', '')                  # 不调用系统 confirm()


def test_backoffice_guest_api_flow(http, fake, store, gc):
    anna = 'c' * 32
    fake.accounts = [account(WANG, 'wangjie', '王姐'), account(LILY, 'lily_sg', 'Lily'), account(anna, 'anna_b', 'Anna')]
    status, v = http['get']('/api/guests')
    assert status == 200 and v['count'] == 0 and not http['pulls']
    status, v = http['get']('/api/guests?refresh=1')
    assert status == 200 and v['count'] == 3 and v['pull']['accounts_status'] == 'ok' and http['pulls'] == [1]
    status, r = http['post']('/api/guests/link', {'action': 'create', 'account_id': anna, 'request_id': 'c1'})
    assert status == 200 and r['customer_id'] == 'GC000901' and r['created'] is True
    with store.connect() as c:
        assert c.execute('SELECT customer_id FROM guest_links WHERE account_id=?', (anna,)).fetchone()[0] == 'GC000901'
    status, s = http['get']('/api/guests/search?q=' + urllib.request.quote('王姐'))
    assert [c['customer_id'] for c in s['results']] == ['GC000123', 'GC000456']
    status, r = http['post']('/api/guests/link', {'action': 'link', 'account_id': WANG, 'customer_id': 'GC000123',
                                                  'request_id': 'r1'})
    assert status == 200 and r['display_name'] == '王姐'
    status, r = http['post']('/api/guests/link', {'action': 'link', 'account_id': WANG, 'customer_id': 'GC000456',
                                                  'request_id': 'r2'})
    assert status == 400 and '已关联到 GC000123' in r['error']
    status, r = http['post']('/api/guests/command', {'action': 'reset_password', 'account_id': WANG, 'request_id': 'p1'})
    assert status == 200 and len(r['temp_password']) == 12
    for _ in range(50):
        if http['kicked']:
            break
        time.sleep(0.02)
    assert http['kicked'] == [1]
    status, r = http['post']('/api/guests/command', {'action': 'disable_account', 'account_id': LILY, 'request_id': 'd1'})
    assert status == 200 and r['state'] == 'pending'
    for _ in range(50):
        if len(http['kicked']) >= 2:
            break
        time.sleep(0.02)
    assert http['kicked'] == [1, 1]
    status, r = http['post']('/api/guests/invite', {'code': 'AB', 'request_id': 'i1'})
    assert status == 400 and '4–32 位' in r['error']
    status, r = http['post']('/api/guests/invite', {'code': 'ABCD2026', 'request_id': 'i2'})
    assert status == 200 and r['invite']['pending'] == {'code': 'ABCD2026', 'state': 'pending'}
    for _ in range(50):                                                   # kick() 在后台线程里调用 flush
        if len(http['kicked']) >= 3:
            break
        time.sleep(0.02)
    assert len(http['kicked']) == 3
    status, v = http['get']('/api/guests')
    assert by(v, 'linked', 'wangjie')['reset']['state'] == 'pending' and by(v, 'pending', 'lily_sg')['disable']['state'] == 'pending'
    assert 'temp_password' not in json.dumps(v)


def test_backoffice_guest_api_refresh_failure_keeps_list(http, fake):
    fake.accounts = [account(WANG, 'wangjie', '王姐')]
    http['get']('/api/guests?refresh=1')
    fake.status = 503
    status, v = http['get']('/api/guests?refresh=1')
    assert status == 200 and v['pull']['accounts_status'] == 'error' and names(v, 'pending') == ['wangjie']


# ---------- 本机真服务器演示（I/O 矩阵端到端） ----------

def _free_port():
    import socket
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


@pytest.mark.skipif(not SERVER_PY.exists(), reason='先在 server/ 下 uv sync')
def test_end_to_end_with_local_server(tmp_path, store, gc):
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

        def session():
            s = requests.Session()
            s.headers['Origin'] = url
            return s

        def register(code, username, nickname):
            s = session()
            r = s.post(url + '/register', allow_redirects=False,
                       data={'invite': code, 'username': username, 'password': 'meissen1710', 'nickname': nickname,
                             'consent': 'yes', 'next': '/'})
            return r, s

        def login(username, password):
            s = session()
            return s.post(url + '/login', allow_redirects=False,
                          data={'username': username, 'password': password, 'next': '/'}), s

        guests.change_invite(store, 'MOMO2026', 'i1')
        assert sync.flush(store, tmp_path)['sync_status'] == 'ok'
        r, wang = register('MOMO2026', 'wangjie', '王姐')
        assert r.status_code == 303
        r, lily = register('MOMO2026', 'lily_sg', 'Lily')
        assert r.status_code == 303

        # 新注册：拉取后出现在待关联
        assert sync.pull_accounts(store, tmp_path)['accounts_status'] == 'ok'
        v = guests.view(store, gc)
        assert v['count'] == 2 and sorted(r['nickname'] + ' · ' + r['username'] for r in v['pending']) == [
            'Lily · lily_sg', '王姐 · wangjie']
        ids = {r['username']: r['account_id'] for r in v['pending']}

        # 关联已有、新建档案（真 gcdb 时）
        guests.link(store, gc, 'link', ids['wangjie'], 'l1', 'GC000123')
        if GCDB.is_file():
            assert guests.create_and_link(store, gc, ids['lily_sg'], 'c1')['customer_id'] == 'GC000901'

        # 重置：待同步 → 确认后旧会话失效、临时密码可登录、服务器只存哈希
        temp = guests.command(store, 'reset_password', ids['wangjie'], 'p1')['temp_password']
        assert by(guests.view(store, gc), 'linked', 'wangjie')['reset']['state'] == 'pending'
        assert '今日榜' in wang.get(url + '/').text
        assert sync.flush(store, tmp_path)['sync_status'] == 'ok'
        assert by(guests.view(store, gc), 'linked', 'wangjie')['reset']['state'] == 'done'
        assert '登录已过期，请重新登录。' in wang.get(url + '/').text
        assert login('wangjie', 'meissen1710')[0].status_code == 401
        r, wang2 = login('wangjie', temp)
        assert r.status_code == 303 and '今日榜' in wang2.get(url + '/').text

        # 停用：待同步 → 确认后进已停用；现有登录失效、再登录只见停用页
        guests.command(store, 'disable_account', ids['lily_sg'], 'd1')
        assert sync.flush(store, tmp_path)['sync_status'] == 'ok'
        assert names(guests.view(store, gc), 'disabled') == ['lily_sg']
        assert '登录已过期，请重新登录。' in lily.get(url + '/').text
        r, lily2 = login('lily_sg', 'meissen1710')
        page = lily2.get(url + '/')
        assert page.status_code == 403 and '此账号已停用' in page.text and '今日榜' not in page.text
        assert sync.pull_accounts(store, tmp_path)['accounts_status'] == 'ok'
        assert names(guests.view(store, gc), 'disabled') == ['lily_sg']

        # 换邀请码：待同步期间旧码仍可注册；确认后旧码失效
        guests.change_invite(store, 'ABCD2026', 'i2')
        assert register('MOMO2026', 'early', '早到')[0].status_code == 303
        assert sync.flush(store, tmp_path)['sync_status'] == 'ok'
        assert register('MOMO2026', 'late', '晚到')[0].status_code == 400
        assert register('ABCD2026', 'late', '晚到')[0].status_code == 303

        # 乱序：序号落后的指令不生效，Mac 按永久失败显示
        with store.connect() as c:
            c.execute("UPDATE kv SET v='0' WHERE k=?", ('cmd_seq:account:' + ids['wangjie'],))
        guests.command(store, 'reset_password', ids['wangjie'], 'p2')
        result = sync.flush(store, tmp_path)
        assert result['sync_status'] == 'blocked' and result['sync_error'] == sync.ACCOUNT_NOT_APPLIED
        assert by(guests.view(store, gc), 'linked', 'wangjie')['reset']['state'] == 'failed'

        with sqlite3.connect(tmp_path / 'server.db') as c:
            dump = '\n'.join(c.iterdump())
        assert temp not in dump and 'GC000123' not in dump                # 映射永不上线
        with sqlite3.connect(store.path) as c:
            assert temp not in '\n'.join(c.iterdump())
    finally:
        proc.terminate()
        proc.wait(timeout=10)
