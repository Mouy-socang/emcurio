"""故事 31：喜欢 / 想要与阅读数——Mac 一侧（行为账本、06 列表、02 / 03 数字；端到端用本机测试服务器）。

覆盖：like / want 契约（Mac 与服务器同一份判断）、账本重放到最后状态（取消后不再列出 / 不再计数）、06 列表显示
谁（已关联的显示 GC 名）、哪件、何时、02 / 03 每行带喜欢 / 想要 / 阅读数；端到端：客人在单件页点了喜欢 / 想要，
Mac 下一轮同步后 06 看到。不连任何外部主机、不读写真实 data/curio.db。
"""
import json
import os
import subprocess
import time
import uuid
from pathlib import Path

import pytest
import requests

from curio import behavior, guests, ranking, sync
from curio.store import Store
from test_guests import (SERVER_PY, TOKEN, _free_port, gc,  # noqa: F401（fixtures）
                         never_real_gc, only_localhost)

ROOT = Path(__file__).resolve().parents[1]
A = 'a' * 32
B = 'b' * 32
C = 'c' * 32


def mark_event(aid, kind='like', on=True, code='CO-0012', n=0):
    return {'event_id': str(uuid.uuid4()), 'account_id': aid, 'type': kind, 'co_code': code,
            'client_ts': '2026-11-04T08:00:00.000Z', 'server_ts': '2026-11-04T08:%02d:%02d.000Z' % (n // 60 % 60, n % 60),
            'params': {'on': on}, 'event_schema_version': 1}


def open_event(aid, code='CO-0012', n=0):
    return {'event_id': str(uuid.uuid4()), 'account_id': aid, 'type': 'item_open', 'co_code': code,
            'client_ts': '2026-11-04T08:00:00.000Z', 'server_ts': '2026-11-04T07:00:%02d.000Z' % n,
            'params': {'from': 'pool'}, 'event_schema_version': 1}


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / 'data' / 'curio.db')


def with_accounts(store, *ids):
    with store.connect() as c:
        for aid in ids:
            c.execute('INSERT OR REPLACE INTO guest_accounts(account_id,username,nickname,status,created_at,account_rev,'
                      'pulled_at) VALUES(?,?,?,?,?,?,?)', (aid, 'u' + aid[:3], '客人' + aid[:1], 'active',
                                                           '2026-11-03T08:00:00.000Z', 1, '2026-11-03T08:00:00.000Z'))


def ingest(store, *events, start=1):
    return behavior.ingest(store, [{'inbox_seq': start + k, 'event': e} for k, e in enumerate(events)])


# ---------- 契约 ----------

def test_check_event_like_want():
    ok = [mark_event(A), mark_event(A, 'want', False)]
    bad = [dict(mark_event(A), co_code=None), dict(mark_event(A), params={'on': 1}), dict(mark_event(A), params={}),
           dict(mark_event(A), params={'on': True, 'x': 1})]
    assert [behavior.check_event(e) for e in ok] == [None, None]
    assert all(behavior.check_event(e) for e in bad)
    assert {'like', 'want'} <= set(behavior.EVENT_TYPES)
    schema = sync.load_schema('behavior_event', 1)
    assert set(schema['properties']['type']['enum']) == set(behavior.EVENT_TYPES) == set(schema['$defs'])


@pytest.mark.skipif(not SERVER_PY.exists(), reason='先在 server/ 下 uv sync')
def test_like_contract_agrees_with_server_jsonschema():
    events = [mark_event(A), mark_event(A, 'want', False), dict(mark_event(A), co_code=None),
              dict(mark_event(A), params={'on': 'yes'}), dict(mark_event(A, 'want'), params={})]
    probe = ('import json,sys\nfrom app import Contract\nimport inbox\n'
             'ck=inbox.Checker(Contract(sys.argv[1]))\nout=[]\n'
             'for e in json.load(sys.stdin):\n'
             '  try:\n    ck.check(e);out.append(True)\n  except inbox.BadEvent:\n    out.append(False)\n'
             'print(json.dumps(out))')
    r = subprocess.run([str(SERVER_PY), '-c', probe, str(ROOT / 'contract' / 'schemas')], cwd=ROOT / 'server',
                       input=json.dumps(events), capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    mac = [behavior.check_event(e) is None for e in events]
    assert json.loads(r.stdout) == mac == [True, True, False, False, False]


# ---------- 重放、计数、06 列表 ----------

def test_counts_replay_last_state(store):
    with_accounts(store, A, B, C)
    ingest(store, mark_event(A, n=1), mark_event(B, n=2), mark_event(C, n=3), mark_event(C, on=False, n=4),
           mark_event(A, 'want', n=5), mark_event(B, code='CO-0013', n=6),
           open_event(A, n=1), open_event(A, n=2), open_event(B, n=3))
    with store.connect() as c:
        got = behavior.item_counts(c)
        assert got['CO-0012'] == {'likes': 2, 'wants': 1, 'reads': 2}
        assert got['CO-0013'] == {'likes': 1, 'wants': 0, 'reads': 0}
        assert behavior.item_counts(c, ['CO-0099']) == {'CO-0099': {'likes': 0, 'wants': 0, 'reads': 0}}


def test_same_timestamp_ordered_by_inbox_seq(store):
    with_accounts(store, A)
    ingest(store, mark_event(A, n=7), mark_event(A, on=False, n=7))          # 同一毫秒：按收件箱顺序
    with store.connect() as c:
        assert behavior.item_counts(c, ['CO-0012'])['CO-0012']['likes'] == 0
        assert behavior.marks_list(c) == []


def test_marks_list_who_what_when(store, gc):
    with_accounts(store, A, B)
    guests.link(store, gc, 'link', A, 'l1', 'GC000123')
    ingest(store, mark_event(A, 'want', n=1), mark_event(B, n=2), mark_event(B, 'want', n=3),
           mark_event(B, 'want', on=False, n=4))
    view = guests.view(store, gc)
    assert [(m['kind'], m['code'], m['at'], m['account_id']) for m in view['marks']] == [
        ('like', 'CO-0012', '2026-11-04T08:00:02.000Z', B), ('want', 'CO-0012', '2026-11-04T08:00:01.000Z', A)]
    want = view['marks'][1]
    assert want['customer_id'] == 'GC000123' and want['customer_name'] == '王姐' and want['nickname'] == '客人a'
    like = view['marks'][0]
    assert like['customer_id'] is None and like['nickname'] == '客人b' and like['username'] == 'ubbb'


def test_view_marks_empty_without_events(store, gc):
    assert guests.view(store, gc)['marks'] == []


def test_triage_page_has_marks_list():
    html = (ROOT / 'curio' / 'triage.html').read_text(encoding='utf-8')
    js = (ROOT / 'curio' / 'triage.js').read_text(encoding='utf-8')
    rk = (ROOT / 'curio' / 'ranking.js').read_text(encoding='utf-8')
    assert 'id="gMarks"' in html and '想要 / 喜欢' in html
    assert 'markRow' in js and 'data.marks' in js
    assert 'marksEl' in rk and "'想要 '" in rk


def test_pool_and_rank_rows_carry_counts(store, tmp_path):
    from test_ranking import add
    from curio.config import load
    from curio import publish
    cfg = dict(load(), keywords=['Meissen'])
    _, code = add(store, cfg, 1, score=5)
    _, code2 = add(store, cfg, 2, score=4)
    with store.connect() as c:
        publish.set_rank(c, [code])
    with_accounts(store, A, B)
    ingest(store, mark_event(A, code=code, n=1), mark_event(B, code=code, n=2), mark_event(A, 'want', code=code, n=3),
           open_event(A, code=code))
    pool = ranking.pool(store, tmp_path)
    rows = {r['code']: r for r in pool['items']}
    assert rows[code]['marks'] == {'likes': 2, 'wants': 1, 'reads': 1}
    assert rows[code2]['marks'] == {'likes': 0, 'wants': 0, 'reads': 0}
    [row] = ranking.rank_state(store, tmp_path)['rows']
    assert row['code'] == code and row['marks'] == {'likes': 2, 'wants': 1, 'reads': 1}


def test_disabled_accounts_not_counted_or_listed(store, gc):
    with_accounts(store, A, B)
    ingest(store, mark_event(A, n=1), mark_event(B, n=2), mark_event(B, 'want', n=3))
    with store.connect() as c:
        c.execute("UPDATE guest_accounts SET status='disabled' WHERE account_id=?", (B,))
        assert behavior.item_counts(c)['CO-0012'] == {'likes': 1, 'wants': 0, 'reads': 0}
    assert [m['account_id'] for m in guests.view(store, gc)['marks']] == [A]


def test_marks_list_titles_named_and_unnamed(store):
    from test_ranking import add
    from test_auction_pool import add as add_raw
    from curio.config import load
    cfg = dict(load(), keywords=['Meissen'])
    named_id, named = add(store, cfg, 1, score=5)                     # 有标准名称
    add_raw(store, cfg, 'raw1', buying='FIXED_PRICE')                  # 没有标准名称：用卖家标题
    unnamed = store.triage_act('keep', 'raw1', 'k-raw1')['code']
    with_accounts(store, A)
    ingest(store, mark_event(A, code=named, n=1), mark_event(A, 'want', code=unnamed, n=2))
    with store.connect() as c:
        rows = {r['code']: r for r in behavior.marks_list(c)}
    assert rows[named]['item_id'] == named_id and rows[named]['title'] == 'Meissen 花瓶 1'      # 标准名称，不是卖家标题
    assert rows[unnamed]['item_id'] == 'raw1' and rows[unnamed]['title'] == 'Meissen Tasse raw1'


def test_auction_rank_rows_carry_counts(store, tmp_path):
    from test_auction_pool import add as add_raw, rz
    from curio.config import load
    from curio import publish
    cfg = dict(load(), keywords=['Meissen'])
    add_raw(store, cfg, 'auc1', ends=rz(20))
    code = store.triage_act('keep', 'auc1', 'k-auc1')['code']
    with store.connect() as c:
        publish.set_rank_auction(c, [code])
    with_accounts(store, A, B)
    ingest(store, mark_event(A, code=code, n=1), mark_event(B, 'want', code=code, n=2), open_event(A, code=code),
           open_event(B, code=code, n=1))
    [row] = ranking.rank_state(store, tmp_path, 'auction')['rows']
    assert row['code'] == code and row['marks'] == {'likes': 1, 'wants': 1, 'reads': 2}


# ---------- 端到端：客人点了 → Mac 同步 → 06 看到 ----------

def _item(n):
    code = 'CO-%04d' % n
    return {'code': code, 'std_name': f'Meissen梅森 杯碟 {n}', 'title': f'Meissen梅森 杯碟 {n}', 'brand': 'Meissen',
            'form_id': 'FN-001', 'form': '杯碟', 'price_eur': 185, 'price_band': 'lt200', 'images': [],
            'seller_text': 'Sehr guter Zustand.', 'seller_lang': 'de'}


@pytest.mark.skipif(not SERVER_PY.exists(), reason='先在 server/ 下 uv sync')
def test_end_to_end_like_and_want_reach_06(tmp_path, store, gc):
    port = _free_port()
    env = dict(os.environ, CURIO_ADMIN_TOKEN=TOKEN, CURIO_SERVER_DB=str(tmp_path / 'server.db'),
               CURIO_COOKIE_SECURE='0', CURIO_MAIL_DIR=str(tmp_path / 'mail'))
    proc = subprocess.Popen([str(SERVER_PY), 'app.py', '--port', str(port)], cwd=ROOT / 'server', env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f'http://127.0.0.1:{port}'
    auth = {'Authorization': 'Bearer ' + TOKEN}
    try:
        for _ in range(100):
            try:
                if requests.get(url + '/healthz', timeout=1).status_code == 200:
                    break
            except requests.RequestException:
                time.sleep(0.1)
        else:
            pytest.fail('本机服务器未启动')
        items = [_item(n) for n in (12, 13)]
        snap = {'schema_version': 1, 'publish_no': 1, 'hide_watermark': 1, 'rank': ['CO-0012'],
                'pool': ['CO-0012', 'CO-0013'], 'items': items, 'tombstones': []}
        r = requests.post(url + '/admin/events', headers=auth, json={
            'schema_version': 1, 'event_id': str(uuid.uuid4()), 'kind': 'snapshot',
            'created_at': '2026-11-04T08:00:00.000Z', 'payload': snap})
        assert r.status_code == 200, r.text
        (tmp_path / '.env').write_text(f'CURIO_SERVER_URL={url}\nCURIO_ADMIN_TOKEN={TOKEN}\n')
        guests.change_invite(store, 'MOMO2026', 'i1')
        assert sync.flush(store, tmp_path)['sync_status'] == 'ok'
        sessions = {}
        for name, nick in (('wangjie', '王姐'), ('lisi', '李四'), ('zhaowu', '赵五')):
            s = requests.Session()
            s.headers['Origin'] = url
            r = s.post(url + '/register', allow_redirects=False,
                       data={'invite': 'MOMO2026', 'username': name, 'password': 'meissen1710', 'nickname': nick,
                             'consent': 'yes', 'next': '/'})
            assert r.status_code == 303
            sessions[name] = s
        assert sync.pull_accounts(store, tmp_path)['accounts_status'] == 'ok'
        wang = next(r for r in guests.view(store, gc)['pending'] if r['username'] == 'wangjie')
        guests.link(store, gc, 'link', wang['account_id'], 'l1', 'GC000123')

        w = sessions['wangjie']
        assert w.get(url + '/item/CO-0012').status_code == 200
        js = {'Accept': 'application/json'}
        r = w.post(url + '/mark', headers=js, data={'code': 'CO-0012', 'kind': 'want', 'on': '1', 'next': '/item/CO-0012'})
        assert r.status_code == 200 and r.json()['on'] is True, r.text
        r = w.post(url + '/mark', allow_redirects=False,         # 无脚本表单
                   data={'code': 'CO-0012', 'kind': 'like', 'on': '1', 'next': '/item/CO-0012'})
        assert r.status_code == 303
        for name in ('lisi', 'zhaowu'):
            assert sessions[name].post(url + '/mark', headers=js, data={'code': 'CO-0012', 'kind': 'like', 'on': '1',
                                                                        'next': '/'}).json()['likes'] in (2, 3)
        # 第四位客人打开首页：那件卡片显示「♡ 3」，不显示是谁
        s = requests.Session()
        s.headers['Origin'] = url
        s.post(url + '/register', allow_redirects=False,
               data={'invite': 'MOMO2026', 'username': 'qianliu', 'password': 'meissen1710', 'nickname': '钱六',
                     'consent': 'yes', 'next': '/'})
        home = s.get(url + '/').text
        start = home.index('data-marks data-code="CO-0012"')
        block = home[start:home.index('</div>', start)]
        assert '♡ 3' in block and '王姐' not in block and 'wangjie' not in block

        r = sync.run(store, tmp_path)
        assert r['inbox_status'] == 'ok' and r['inbox_unknown'] == 0, r
        marks = guests.view(store, gc)['marks']
        mine = [m for m in marks if m['account_id'] == wang['account_id']]
        assert sorted(m['kind'] for m in mine) == ['like', 'want']
        assert all(m['code'] == 'CO-0012' and m['customer_name'] == '王姐' and m['customer_id'] == 'GC000123'
                   and m['at'].endswith('Z') for m in mine)
        assert len([m for m in marks if m['kind'] == 'like']) == 3
        with store.connect() as c:
            assert behavior.item_counts(c, ['CO-0012'])['CO-0012'] == {'likes': 3, 'wants': 1, 'reads': 0}
        # 取消后下一轮同步不再列出
        w.post(url + '/mark', headers=js, data={'code': 'CO-0012', 'kind': 'want', 'on': '0', 'next': '/'})
        sync.run(store, tmp_path)
        assert [m['kind'] for m in guests.view(store, gc)['marks'] if m['account_id'] == wang['account_id']] == ['like']
    finally:
        proc.terminate()
        proc.wait(10)
