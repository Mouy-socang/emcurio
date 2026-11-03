"""故事 18（CAP-12，FR-24，AD-11）：千人千面商品池——Mac 端打分与顺序（judge.personal_order）、谁有顺序、
发布时写进快照（可选 orders）、03 显示人数，以及本机测试服务器上的端到端演示。

GoodCustomer 一律用临时资料库（tmp_path），从不读写真实 ~/Desktop/goodcustomerDB；curio.db 都在 tmp_path；
任何非 127.0.0.1 的请求直接失败。
"""
import json
import re
import shutil
import sqlite3
import time
import uuid
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
import requests

from curio import judge, matching, personalize, publish, ranking, rules, sync, triage
from curio.config import load, match_rules_for, personalize_rules, personalize_rules_for
from curio.store import Store
from test_publish import Fetch, live, png  # noqa: F401  (live 是夹具)

ROOT = Path(__file__).resolve().parents[1]
REAL_GC = Path('~/Desktop/goodcustomerDB').expanduser().resolve()
REAL_DB = (ROOT / 'data' / 'curio.db').resolve()
NOW = time.time()
_real_request = requests.sessions.Session.request


@pytest.fixture(autouse=True)
def only_localhost():
    def guard(self, method, url, *a, **kw):
        assert urlsplit(url).hostname == '127.0.0.1', '测试不得连接外部主机'
        return _real_request(self, method, url, *a, **kw)
    with patch('requests.sessions.Session.request', guard), patch('curio.sync._notify'):
        yield


@pytest.fixture(autouse=True)
def fresh_cache():
    matching._cache.clear()
    yield
    matching._cache.clear()


# ---------- 临时 GoodCustomer 资料库 ----------

def customer(gc, cid, name, needs=(), prefs=()):
    d = Path(gc['db']) / 'customers' / cid
    (d / '06_结构化结果').mkdir(parents=True, exist_ok=True)
    (d / 'customer.json').write_text(json.dumps({'customer_id': cid, 'display_name': name, 'aliases': [],
                                                 'status': 'active', 'platform_accounts': []}, ensure_ascii=False))
    (d / '06_结构化结果' / 'needs.jsonl').write_text(''.join(json.dumps(n, ensure_ascii=False) + '\n' for n in needs))
    (d / '06_结构化结果' / 'preferences.jsonl').write_text(
        ''.join(json.dumps(p, ensure_ascii=False) + '\n' for p in prefs))


def pref(dim, value, polarity='like', source='explicit'):
    return {'dimension': dim, 'value': value, 'polarity': polarity, 'source_type': source, 'status': 'active'}


@pytest.fixture
def gc(tmp_path):
    db = tmp_path / 'goodcustomerDB'
    (db / '_system').mkdir(parents=True)
    (db / '_system' / 'db.json').write_text('{"schema_version": "gc_schema_v3_2_local"}')
    assert db.resolve() != REAL_GC
    return {'db': str(db), 'gcdb': str(tmp_path / 'no-gcdb.py')}


@pytest.fixture
def root(tmp_path):
    return tmp_path


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / 'data' / 'curio.db')
    assert Path(s.path).resolve() != REAL_DB
    return s


@pytest.fixture
def cfg():
    return dict(load(), keywords=['Meissen'])


# 商品池（墨墨顺序 = 留下时间新到旧，所以后加的在前）。
GOODS = [
    # key, 品牌, 器型, 图案, 图案 ID
    ('m_cup', 'Meissen', '杯', None, None),
    ('k_vase1', 'KPM Berlin', '花瓶', None, None),
    ('m_onion', 'Meissen', '盘', '蓝洋葱', 'MP-001'),
    ('k_cup', 'KPM Berlin', '杯', None, None),
    ('m_vase', 'Meissen', '花瓶', None, None),
    ('k_vase2', 'KPM Berlin', '花瓶', None, None),
    ('m_plate', 'Meissen', '盘', None, None),
    ('k_plate', 'KPM Berlin', '盘', None, None),
]
TITLES = {'Meissen': 'Meissen', 'KPM Berlin': 'KPM Berlin'}
FORM_WORD = {'杯': 'Tasse', '花瓶': 'Vase', '盘': 'Teller'}


def add(store, cfg, n, key, brand, form, pattern=None, pattern_id=None, image=''):
    iid = f'it-{key}'
    store.ingest(dict(item_id=iid, platform='ebay', title=f'{TITLES[brand]} {FORM_WORD[form]} {n}', price=100 + n * 10,
                      currency='EUR', buying='FIXED_PRICE', url=f'https://www.ebay.de/itm/{100000000000 + n}',
                      image=image, description='Sehr guter Zustand.'), cfg)
    seg = {'brand': brand, 'pattern': pattern, 'function': form}
    with store.connect() as c:
        c.execute('''INSERT OR REPLACE INTO std_names(item_id,std_name,std_key,status,brand,pattern_id,form_id,function_id,
            era,seller_grade,segments,kb_status,kb_fingerprint,generated_at,overpaint)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)''',
                  (iid, ' '.join(x for x in (brand, pattern, form) if x), f'{brand}|-|-|-', 'named', brand, pattern_id,
                   None, 'FN-%03d' % (1 + list(FORM_WORD).index(form)), None, None,
                   json.dumps(seg, ensure_ascii=False), 'ok', 'fp', '2026-11-03T00:00:00+00:00'))
        triage.keep(c, iid)
        c.execute("UPDATE triage SET updated_at=? WHERE item_id=?", ('2026-11-03T%02d:00:00+00:00' % (20 - n), iid))
        return triage.code(c, iid)


@pytest.fixture
def goods(store, cfg):
    return {key: add(store, cfg, n, key, *rest) for n, (key, *rest) in enumerate(GOODS, 1)}


def account(store, account_id=None, customer_id=None, status='active', username=None):
    account_id = account_id or uuid.uuid4().hex
    with store.connect() as c:
        c.execute('INSERT OR REPLACE INTO guest_accounts VALUES(?,?,?,?,?,?,?)',
                  (account_id, username or account_id[:8], '客人', status, '2026-11-03T08:00:00.000Z', 1,
                   '2026-11-03T08:00:00.000Z'))
        if customer_id:
            c.execute('INSERT OR REPLACE INTO guest_links VALUES(?,?,?)', (account_id, customer_id,
                                                                           '2026-11-03T08:00:00.000Z'))
    return account_id


def iso(ts):
    return time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime(ts))


def event(store, account_id, kind, code=None, seconds=None, days_ago=0.0):
    params = {'seconds': seconds} if kind == 'item_dwell' else {'from': 'pool'} if kind == 'item_open' else {}
    at = iso(NOW - days_ago * 86400)
    with store.connect() as c:
        c.execute('INSERT INTO behavior_events(event_id,account_id,type,co_code,client_ts,server_ts,params,'
                  'event_schema_version,inbox_seq,pulled_at) VALUES(?,?,?,?,?,?,?,1,NULL,?)',
                  (str(uuid.uuid4()), account_id, kind, code, at, at, json.dumps(params), at))


def raw_orders(store, gc, root, now=NOW):
    """下发的顺序（前缀）。"""
    inputs = personalize.prepare(store, gc, root, now=now)
    with store.connect() as c:
        return personalize.orders(c, publish.eligible(c), inputs)[0]


def orders(store, gc, root, now=NOW):
    """展开成整个商品池的顺序（前缀 + 其余按墨墨顺序，同服务器）。"""
    base = moomoo(store)
    got = raw_orders(store, gc, root, now)
    for prefix in got.values():
        assert prefix and prefix != base[:len(prefix)] and prefix[-1] != base[len(prefix) - 1]   # 只到最后一处不同
    return {a: p + [x for x in base if x not in p] for a, p in got.items()}


def moomoo(store):
    with store.connect() as c:
        return [it['code'] for it in publish.eligible(c)]


def brands_of(store, order):
    with store.connect() as c:
        return [c.execute('SELECT n.brand FROM item_codes k JOIN std_names n ON n.item_id=k.item_id WHERE k.code=?',
                          (code,)).fetchone()[0] for code in order]


# ---------- I/O 矩阵 ----------

def test_two_tastes_get_different_orders(store, gc, root, goods):
    customer(gc, 'GC000201', '甲', prefs=[pref('maker', '梅森')])
    customer(gc, 'GC000202', '乙', prefs=[pref('maker', 'KPM')])
    a = account(store, customer_id='GC000201')
    b = account(store, customer_id='GC000202')
    got = orders(store, gc, root)
    assert set(got) == {a, b}
    assert brands_of(store, got[a])[:4] == ['Meissen'] * 4 and brands_of(store, got[b])[:4] == ['KPM Berlin'] * 4
    assert sorted(got[a]) == sorted(got[b]) == sorted(moomoo(store))
    # 同分保持墨墨顺序（稳定排序）
    base = moomoo(store)
    assert got[a][:4] == [x for x in base if x in got[a][:4]]


def test_unlinked_and_no_record_accounts_get_no_order(store, gc, root, goods):
    customer(gc, 'GC000203', '丙')                       # 档案在，但没有需求 / 喜好
    account(store)                                        # 未关联
    account(store, customer_id='GC000203')                # 已关联、无记录
    account(store, customer_id='GC000299')                # 已关联、GC 里没有这份档案、无浏览
    assert orders(store, gc, root) == {}


def test_no_linked_accounts_never_reads_goodcustomer(store, root, goods):
    account(store)
    with patch('curio.matching.profiles', side_effect=AssertionError('不应读 GoodCustomer')):
        assert personalize.prepare(store, None, root) is None
        assert orders(store, None, root) == {}


def test_browse_signal_lifts_similar_items_but_not_the_viewed_one(store, gc, root, goods):
    a = account(store, customer_id='GC000204')            # GC 无档案：只靠浏览
    for _ in range(3):
        event(store, a, 'item_open', goods['k_vase1'])
    event(store, a, 'item_dwell', goods['k_vase1'], seconds=120)
    got = orders(store, gc, root)[a]
    assert got[0] == goods['k_vase2']                     # 同品牌同器型的另一件最前
    assert got.index(goods['k_vase2']) < got.index(goods['m_vase']) < got.index(goods['m_cup'])
    assert got.index(goods['k_vase1']) > got.index(goods['m_vase'])   # 浏览过的那件本身不加分


def test_home_open_only_is_a_record_but_same_as_moomoo_so_not_sent(store, gc, root, goods):
    a = account(store, customer_id='GC000205')
    event(store, a, 'home_open')
    assert orders(store, gc, root) == {}


def test_dislike_sinks_to_end_still_visible(store, gc, root, goods):
    customer(gc, 'GC000206', '丁', prefs=[pref('maker', '梅森'), pref('pattern', '蓝洋葱', polarity='dislike')])
    a = account(store, customer_id='GC000206')
    got = orders(store, gc, root)[a]
    assert got[-1] == goods['m_onion'] and len(got) == len(GOODS)


def test_dislike_only_counts_as_record(store, gc, root, goods):
    customer(gc, 'GC000207', '戊', prefs=[pref('maker', 'KPM', polarity='dislike')])
    a = account(store, customer_id='GC000207')
    got = orders(store, gc, root)[a]
    assert brands_of(store, got)[-4:] == ['KPM Berlin'] * 4


def test_rejected_in_matching_sinks_for_that_customer(store, gc, root, goods):
    customer(gc, 'GC000208', '己', prefs=[pref('maker', '梅森')])
    a = account(store, customer_id='GC000208')
    with store.connect() as c:
        iid = c.execute('SELECT item_id FROM item_codes WHERE code=?', (goods['m_cup'],)).fetchone()[0]
        c.execute("INSERT INTO match_decisions(customer_id,item_id,decision,at) VALUES('GC000208',?,'rejected',?)",
                  (iid, '2026-11-03T08:00:00.000Z'))
    assert orders(store, gc, root)[a][-1] == goods['m_cup']


def test_need_beats_explicit_beats_inferred(store, gc, root, goods):
    customer(gc, 'GC000209', '庚',
             needs=[{'need_type': 'active_search', 'status': 'open', 'maker': ['KPM'], 'additional_description': 'KPM 杯'}],
             prefs=[pref('maker', '梅森'), pref('maker', 'KPM', source='purchase')])
    a = account(store, customer_id='GC000209')
    got = orders(store, gc, root)[a]
    base = moomoo(store)
    meissen = [x for x in base if x in {goods[k] for k in ('m_cup', 'm_onion', 'm_vase', 'm_plate')}]
    kpm_rest = [x for x in base if x in {goods[k] for k in ('k_vase1', 'k_vase2', 'k_plate')}]
    # 明说想要（KPM 杯）> 明说喜欢（梅森）> 推算口味（购买推出的 KPM），同级保持墨墨顺序
    assert got == [goods['k_cup']] + meissen + kpm_rest


def test_disabled_account_order_removed(store, gc, root, goods):
    customer(gc, 'GC000210', '辛', prefs=[pref('maker', 'KPM')])
    a = account(store, customer_id='GC000210')
    b = account(store, customer_id='GC000210')            # 同一客户两个账号：同一顺序
    got = orders(store, gc, root)
    assert got[a] == got[b]
    account(store, a, 'GC000210', status='disabled')
    assert set(orders(store, gc, root)) == {b}
    # 停用指令已入发件箱（服务器还没回名单）也不再下发
    with store.connect() as c:
        event_id, cmd = sync.enqueue_account_command(c, 'disable_account', b)
        c.execute("INSERT INTO guest_commands VALUES(?,?,?,'disable_account',?,?)",
                  (cmd['command_id'], event_id, b, cmd['target_seq'], '2026-11-03T08:00:00.000Z'))
    assert orders(store, gc, root) == {}


def test_gc_unavailable_uses_browse_only(store, root, goods, tmp_path):
    a = account(store, customer_id='GC000211')
    event(store, a, 'item_open', goods['m_plate'])
    got = orders(store, {'db': str(tmp_path / 'nope'), 'gcdb': 'x'}, root)
    # 同品牌同器型（梅森蓝洋葱盘）> 只同器型（KPM 盘）> 其余；被看的那件本身不加分
    assert got[a][:2] == [goods['m_onion'], goods['k_plate']]
    assert got[a].index(goods['m_plate']) > 2


# ---------- 规则归口单元 ----------

def test_browse_signal_decays_and_caps():
    params = personalize_rules_for(ROOT)
    fresh = judge.browse_signal('item_open', None, 0, params)
    assert fresh == params['open']
    assert judge.browse_signal('item_open', None, params['half_life_days'], params) == pytest.approx(fresh / 2)
    cap = params['dwell_cap_seconds']
    assert judge.browse_signal('item_dwell', cap * 10, 0, params) == pytest.approx(params['dwell_per_second'] * cap)
    assert judge.browse_signal('home_open', None, 0, params) == 0


def test_personal_order_stable_and_set_preserving():
    vocab, params = match_rules_for(ROOT), personalize_rules_for(ROOT)
    pool = [(f'CO-{n:04d}', judge.match_facts({'brand': 'Meissen', 'segments': '{}'}, 'Meissen', vocab))
            for n in range(1, 8)]
    order, matched = rules.personal_order(pool, [], [], [], set(), vocab, params)
    assert order == [c for c, _ in pool] and matched is False
    order, matched = rules.personal_order(pool, [], [pref('maker', '梅森', polarity='dislike')], [], {'CO-0002'},
                                          vocab, params)
    assert order == [c for c, _ in pool] and matched is True      # 全部沉底：相对顺序不变


def test_match_customer_unchanged_by_refactor():
    vocab = match_rules_for(ROOT)
    f = judge.match_facts({'brand': 'Meissen', 'pattern_id': 'MP-001', 'segments': json.dumps({'pattern': '蓝洋葱'})},
                          'Meissen Zwiebelmuster Teller', vocab)
    assert judge.match_customer(f, [], [pref('maker', '梅森')], vocab)['grade'] == 'explicit'
    assert judge.match_customer(f, [], [pref('maker', '梅森'), pref('pattern', '蓝洋葱', polarity='dislike')],
                                vocab) is None
    assert judge.customer_dislikes(f, [pref('pattern', '蓝洋葱', polarity='dislike')], vocab) is True


@pytest.mark.parametrize('text, err', [
    ('browse: 3', '映射'),
    ('browse: {open: -1, dwell_per_second: 0, dwell_cap_seconds: 1, half_life_days: 1, max_score: 0.5, '
     'same: {brand: 0, pattern: 0, form: 0}}', '非负数'),
    ('browse: {open: 1, dwell_per_second: 0, dwell_cap_seconds: 1, half_life_days: 0, max_score: 0.5, '
     'same: {brand: 0, pattern: 0, form: 0}}', '大于 0'),
    ('browse: {open: 1, dwell_per_second: 0, dwell_cap_seconds: 1, half_life_days: 1, max_score: 0.5, same: {brand: 0}}', '恰好'),
    ('browse: {open: 1, dwell_per_second: 0, dwell_cap_seconds: 1, half_life_days: 1, max_score: 0.5, x: 1, '
     'same: {brand: 0, pattern: 0, form: 0}}', '未知键'),
])
def test_personalize_config_validated(tmp_path, text, err):
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config' / 'personalize.yaml').write_text(text)
    with pytest.raises(ValueError, match=err):
        personalize_rules(tmp_path)


def test_repo_config_loads():
    p = personalize_rules(ROOT)
    assert p['half_life_days'] > 0 and set(p['same']) == {'brand', 'pattern', 'form'}


# ---------- 发布：快照里的 orders ----------

pytest_sips = pytest.mark.skipif(shutil.which('sips') is None, reason='需要 macOS 自带 sips')


def ranked_pool(store, cfg, n=12):
    """12 件可发布的货（远程图只经假下载），榜取前 10。KPM 与 Meissen 交替。"""
    codes = []
    for i in range(1, n + 1):
        brand = 'KPM Berlin' if i % 2 else 'Meissen'
        form = ('杯', '花瓶', '盘')[i % 3]
        codes.append(add(store, cfg, i, f'g{i}', brand, form,
                         image=f'https://i.ebayimg.com/images/g/{i}.png'))
    with store.connect() as c:
        publish.set_rank(c, codes[:10])
    return codes


def snapshot_payloads(store):
    with store.connect() as c:
        return [json.loads(r[0]) for r in c.execute("SELECT payload FROM outbox WHERE kind='snapshot' ORDER BY seq")]


@pytest_sips
def test_publish_writes_orders_and_count(store, gc, root, cfg):
    codes = ranked_pool(store, cfg)
    customer(gc, 'GC000301', '甲', prefs=[pref('maker', '梅森')])
    customer(gc, 'GC000302', '乙', prefs=[pref('maker', 'KPM')])
    a = account(store, customer_id='GC000301')
    b = account(store, customer_id='GC000302')
    account(store)                                                    # 未关联
    r = publish.publish(store, root, fetch=Fetch(), gc=gc)
    assert r['orders'] == 2
    snap = snapshot_payloads(store)[-1]
    assert set(snap['orders']) == {a, b}
    assert all(set(o) <= set(snap['pool']) and len(set(o)) == len(o) for o in snap['orders'].values())
    assert snap['rank'] == codes[:10]
    sync.check_envelope({'schema_version': 1, 'event_id': str(uuid.uuid4()), 'kind': 'snapshot',
                         'created_at': '2026-11-03T08:00:00.000Z', 'payload': snap})
    # 快照里不出现客户编号与画像
    text = json.dumps(snap, ensure_ascii=False)
    assert 'GC0003' not in text and '梅森' not in text
    with store.connect() as c:
        assert ranking.publish_status(c)['orders'] == 2
    # 预览（候选快照）按墨墨顺序：不带 orders
    payload, _ = publish.candidate(store, root, Fetch())
    assert 'orders' not in payload


@pytest_sips
def test_publish_without_links_has_no_orders(store, gc, root, cfg):
    ranked_pool(store, cfg)
    r = publish.publish(store, root, fetch=Fetch(), gc=gc)
    assert r['orders'] == 0 and 'orders' not in snapshot_payloads(store)[-1]
    with store.connect() as c:
        assert ranking.publish_status(c)['orders'] == 0


@pytest_sips
def test_oversize_snapshot_drops_orders_but_publishes(store, gc, root, cfg, monkeypatch, caplog):
    ranked_pool(store, cfg)
    customer(gc, 'GC000303', '甲', prefs=[pref('maker', '梅森')])
    account(store, customer_id='GC000303')
    monkeypatch.setattr(publish, 'SNAPSHOT_MAX', 1000)
    r = publish.publish(store, root, fetch=Fetch(), gc=gc)
    assert r['orders'] == 0 and 'orders' not in snapshot_payloads(store)[-1] and '超过上限' in caplog.text


POOL2 = ['CO-0001', 'CO-0002']


@pytest.mark.parametrize('bad', [
    {'x': POOL2},                                   # 账号编号格式
    {'A' * 32: POOL2},                              # 大写账号编号
    {'a' * 32: POOL2 + ['CO-0009']},                # 不在商品池
    {'a' * 32: ['CO-0001', 'CO-0001']},             # 重复
    {'a' * 32: 'CO-0001'},                          # 不是列表
    {'a' * 32: [1, 2]},                             # 不是文字
    {'a' * 32: [['CO-0001']]},                      # 不可哈希的项（不得抛 TypeError）
    {'a' * 32: ['co-0001']},                        # 货号格式
    [POOL2],                                        # 不是对象
])
def test_check_orders_rejects_bad(bad):
    publish.check_orders({'pool': POOL2, 'orders': {'a' * 32: POOL2}})
    with pytest.raises(publish.PublishError):
        publish.check_orders({'pool': POOL2, 'orders': bad})


@pytest.mark.parametrize('bad', [{'x': POOL2}, {'A' * 32: POOL2}, {'a' * 32: ['CO-0001', 'CO-0001']},
                                 {'a' * 32: 'CO-0001'}, [POOL2]])
def test_contract_rejects_bad_orders(bad):
    schema = sync.load_schema('snapshot', 1)
    good = {'schema_version': 1, 'publish_no': 1, 'hide_watermark': 1, 'rank': ['CO-0001'] * 10, 'pool': POOL2,
            'items': [], 'tombstones': []}
    assert not [e for e in sync.validate(dict(good, orders={'a' * 32: POOL2}), schema) if 'orders' in e]
    assert [e for e in sync.validate(dict(good, orders=bad), schema) if 'orders' in e]


def test_with_orders_drops_longest_first_and_counts_customers(monkeypatch):
    pool = ['CO-%04d' % n for n in range(1, 201)]
    payload = {'pool': pool}
    a, b, c = 'a' * 32, 'b' * 32, 'c' * 32
    got = {a: pool[:150], b: pool[:5], c: pool[:5]}
    owner = {a: 'GC000001', b: 'GC000002', c: 'GC000002'}
    full = publish._size(dict(payload, orders=got))
    monkeypatch.setattr(publish, 'SNAPSHOT_MAX', full - 10)
    assert publish._with_orders(payload, got, owner) == 1           # b、c 同一位客户
    assert set(payload['orders']) == {b, c}
    payload = {'pool': pool}
    assert publish._with_orders(payload, {b: pool[:5], c: pool[:5]}, owner) == 1



# ---------- 本机演示：测试服务器 + 临时 GC 库 ----------

def register(url, username):
    s = requests.Session()
    s.headers['Origin'] = url
    r = s.post(url + '/register', allow_redirects=False,
               data={'invite': 'MOMO2026', 'username': username, 'password': 'meissen1710', 'nickname': username,
                     'consent': 'yes', 'next': '/'})
    assert r.status_code == 303
    return s


def pool_of(s, url):
    html = s.get(url + '/').text
    got = re.findall(r'<li class="card"><a class="card-a" href="/item/(CO-\d+)"', html)
    rank = re.findall(r'<li class="rk"><a class="rk-a" href="/item/(CO-\d+)"', html)
    while True:
        r = s.get(url + f'/api/pool?offset={len(got)}').json()
        got += re.findall(r'<li class="card"><a class="card-a" href="/item/(CO-\d+)"', r['html'])
        if r['next_offset'] is None:
            return got, rank


@pytest_sips
def test_demo_two_tastes_on_local_server(store, gc, root, cfg, live):
    codes = ranked_pool(store, cfg, 26)
    with store.connect() as c:
        sync.enqueue_invite(c, 'MOMO2026')
    assert sync.flush(store, root)['sync_status'] == 'ok'
    sa, sb, sc = register(live, 'guest_a'), register(live, 'guest_b'), register(live, 'guest_c')
    assert sync.pull_accounts(store, root)['accounts_status'] == 'ok'
    with store.connect() as c:
        ids = dict(c.execute('SELECT username, account_id FROM guest_accounts').fetchall())
        c.execute('INSERT INTO guest_links VALUES(?,?,?)', (ids['guest_a'], 'GC000401', '2026-11-03T08:00:00.000Z'))
        c.execute('INSERT INTO guest_links VALUES(?,?,?)', (ids['guest_b'], 'GC000402', '2026-11-03T08:00:00.000Z'))
    customer(gc, 'GC000401', '甲', prefs=[pref('maker', '梅森')])
    customer(gc, 'GC000402', '乙', prefs=[pref('maker', 'KPM')])
    assert publish.publish(store, root, fetch=Fetch(), gc=gc)['orders'] == 2
    assert sync.flush(store, root)['sync_status'] == 'ok'
    (pa, ra), (pb, rb), (pc, rc) = pool_of(sa, live), pool_of(sb, live), pool_of(sc, live)
    assert pa != pb and ra == rb == rc == codes[:10]
    assert len(pa) == len(pb) == len(pc) == 26 and sorted(pa) == sorted(pb) == sorted(pc)
    assert pc == moomoo(store)                                     # 未关联：墨墨顺序
    assert set(brands_of(store, pa[:13])) == {'Meissen'} and set(brands_of(store, pb[:13])) == {'KPM Berlin'}
    # 隐藏：顺序里的货随后下架，不出现，其余顺序不变
    with store.connect() as c:
        iid = c.execute('SELECT item_id FROM item_codes WHERE code=?', (pa[0],)).fetchone()[0]
    store.mark_gone(iid)
    assert sync.flush(store, root)['sync_status'] == 'ok'
    assert pool_of(sa, live)[0] == pa[1:]


# ---------- 评审修正 ----------

def test_browse_never_outranks_a_better_grade(store, gc, root, goods):
    """浏览加分有上限：KPM 花瓶被点开、停留很多次，明说想要的梅森杯与明说喜欢的梅森仍在前。"""
    customer(gc, 'GC000501', '壬',
             needs=[{'need_type': 'active_search', 'status': 'open', 'maker': ['梅森'], 'additional_description': '梅森杯'}],
             prefs=[pref('maker', '梅森'), pref('maker', 'KPM', source='purchase')])
    a = account(store, customer_id='GC000501')
    for _ in range(10):
        event(store, a, 'item_open', goods['k_vase1'])
        event(store, a, 'item_dwell', goods['k_vase1'], seconds=1800)
    got = orders(store, gc, root)[a]
    meissen = [goods[k] for k in ('m_onion', 'm_vase', 'm_plate')]
    kpm = [goods[k] for k in ('k_vase1', 'k_vase2', 'k_cup', 'k_plate')]
    assert got[0] == goods['m_cup']                                     # 明说想要
    assert set(got[1:4]) == set(meissen) and set(got[4:]) == set(kpm)   # 明说喜欢 > 推算口味（+浏览）
    assert got[4] == goods['k_vase2']                                   # 同级里浏览信号起作用


def test_max_score_must_stay_below_grade_step(tmp_path):
    (tmp_path / 'config').mkdir()
    shutil.copy(ROOT / 'config' / 'matching.yaml', tmp_path / 'config' / 'matching.yaml')
    text = (ROOT / 'config' / 'personalize.yaml').read_text()
    (tmp_path / 'config' / 'personalize.yaml').write_text(text.replace('max_score: 0.9', 'max_score: 1'))
    with pytest.raises(ValueError, match='最小级差'):
        personalize_rules(tmp_path)


def test_rejection_only_counts_as_record(store, gc, root, goods):
    a = account(store, customer_id='GC000502')                # GC 无档案、无浏览，只有 04 不合适
    with store.connect() as c:
        iid = c.execute('SELECT item_id FROM item_codes WHERE code=?', (goods['k_cup'],)).fetchone()[0]
        c.execute("INSERT INTO match_decisions(customer_id,item_id,decision,at) VALUES('GC000502',?,'rejected',?)",
                  (iid, '2026-11-03T08:00:00.000Z'))
    assert orders(store, gc, root)[a][-1] == goods['k_cup']


def test_merged_customer_follows_surviving_profile(store, gc, root, goods, caplog):
    customer(gc, 'GC000504', '癸', prefs=[pref('maker', 'KPM')])
    customer(gc, 'GC000503', '癸（旧档）')
    old = Path(gc['db']) / 'customers' / 'GC000503' / 'customer.json'
    data = json.loads(old.read_text())
    old.write_text(json.dumps(dict(data, status='merged', merged_into_customer_id='GC000504'), ensure_ascii=False))
    before = {p.name: p.read_bytes() for p in Path(gc['db']).rglob('*') if p.is_file()}
    a = account(store, customer_id='GC000503')
    lost = account(store, customer_id='GC000599')
    caplog.set_level('INFO', logger='curio.personalize')
    got = orders(store, gc, root)
    assert brands_of(store, got[a])[:4] == ['KPM Berlin'] * 4 and lost not in got
    assert 'GC000599' in caplog.text and 'GC000504' in caplog.text
    assert {p.name: p.read_bytes() for p in Path(gc['db']).rglob('*') if p.is_file()} == before   # 只读


def test_failed_disable_command_keeps_order(store, gc, root, goods):
    customer(gc, 'GC000505', '子', prefs=[pref('maker', 'KPM')])
    a = account(store, customer_id='GC000505')
    with store.connect() as c:
        event_id, cmd = sync.enqueue_account_command(c, 'disable_account', a)
        c.execute("INSERT INTO guest_commands VALUES(?,?,?,'disable_account',?,?)",
                  (cmd['command_id'], event_id, a, cmd['target_seq'], '2026-11-03T08:00:00.000Z'))
        c.execute("UPDATE outbox SET failed_at=?, last_error='已被拒收' WHERE event_id=?",
                  ('2026-11-03T08:01:00.000Z', event_id))
    assert a in orders(store, gc, root)


def test_float_dwell_seconds_counts_like_int(store, gc, root, goods):
    a = account(store, customer_id='GC000506')
    b = account(store, customer_id='GC000507')
    event(store, a, 'item_dwell', goods['k_vase1'], seconds=120)
    event(store, b, 'item_dwell', goods['k_vase1'], seconds=120.0)
    got = orders(store, gc, root)
    assert got[a] == got[b] and got[a][0] == goods['k_vase2']


def test_event_older_than_cutoff_ignored(store, gc, root, goods):
    params = personalize_rules_for(root)
    a = account(store, customer_id='GC000508')
    b = account(store, customer_id='GC000509')
    old = personalize.CUTOFF_HALF_LIVES * params['half_life_days'] + 1
    event(store, a, 'item_open', goods['k_vase1'], days_ago=old)
    event(store, b, 'item_open', goods['k_vase1'], days_ago=old - 2)
    got = raw_orders(store, gc, root)
    assert a not in got and b in got                          # 截止之前的事件读都不读（也不算记录）


@pytest_sips
def test_old_event_does_not_change_published_order(store, gc, root, cfg):
    ranked_pool(store, cfg)
    a = account(store, customer_id='GC000510')
    with store.connect() as c:
        code = publish.eligible(c)[-1]['code']
    days = personalize.CUTOFF_HALF_LIVES * personalize_rules_for(root)['half_life_days'] + 1
    event(store, a, 'item_open', code, days_ago=days)
    r = publish.publish(store, root, fetch=Fetch(), gc=gc)
    assert r['orders'] == 0 and 'orders' not in snapshot_payloads(store)[-1]


@pytest_sips
def test_bad_personalize_config_publishes_without_orders(store, gc, root, cfg, caplog):
    ranked_pool(store, cfg)
    customer(gc, 'GC000511', '丑', prefs=[pref('maker', '梅森')])
    account(store, customer_id='GC000511')
    (root / 'config').mkdir()
    (root / 'config' / 'personalize.yaml').write_text('browse: 3')
    (root / 'config' / 'matching.yaml').write_text('grades: nope')
    r = publish.publish(store, root, fetch=Fetch(), gc=gc)
    assert r['orders'] == 0 and 'orders' not in snapshot_payloads(store)[-1] and '准备失败' in caplog.text


@pytest_sips
def test_error_inside_orders_publishes_without_orders(store, gc, root, cfg, caplog, monkeypatch):
    ranked_pool(store, cfg)
    customer(gc, 'GC000512', '寅', prefs=[pref('maker', '梅森')])
    account(store, customer_id='GC000512')

    def boom(*a, **k):
        raise RuntimeError('排序出错')
    monkeypatch.setattr(rules, 'order_by_scores', boom)
    r = publish.publish(store, root, fetch=Fetch(), gc=gc)
    assert r['orders'] == 0 and 'orders' not in snapshot_payloads(store)[-1] and '排序失败' in caplog.text


@pytest_sips
def test_legacy_publishes_table_migrates(tmp_path, gc, cfg):
    path = tmp_path / 'data' / 'curio.db'
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as c:
        c.execute('''CREATE TABLE publishes(publish_no INTEGER PRIMARY KEY, event_id TEXT NOT NULL, created_at TEXT NOT NULL,
            rank TEXT NOT NULL, pool_size INTEGER NOT NULL, tombstones INTEGER NOT NULL, images INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', tomb_json TEXT NOT NULL DEFAULT '[]', status_fp TEXT)''')
        c.execute("INSERT INTO publishes VALUES(0,'ev0','2026-11-01T08:00:00.000Z','[]',0,0,0,'delivered','[]','x')")
    store = Store(path)
    with store.connect() as c:
        assert ranking.publish_status(c)['orders'] == 0
    ranked_pool(store, cfg)
    customer(gc, 'GC000513', '卯', prefs=[pref('maker', '梅森')])
    account(store, customer_id='GC000513')
    r = publish.publish(store, tmp_path, fetch=Fetch(), gc=gc)
    assert r['orders'] == 1
    with store.connect() as c:
        assert ranking.publish_status(c)['orders'] == 1
        assert c.execute('SELECT orders FROM publishes WHERE publish_no=0').fetchone()[0] == 0
