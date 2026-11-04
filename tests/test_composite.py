"""故事 27：综合评分（方案 C）——成分与合成、理由、重算与存储、封存改用综合分、对比脚本。
全部用临时库与假 GoodCustomer，不读写真实 data/curio.db 与 GoodCustomer，不连外部主机。"""
import hashlib
import io
import json
import logging
import re
import shutil
import sqlite3
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from curio import agreement, judge, ranking, rules, scoring
from curio.config import ROOT, scoring_override, scoring_rules
from curio.schedule import BERLIN
from curio.store import Store, card

PARAMS = scoring_rules(ROOT)
NAMES = dict(brand={'Meissen': '梅森'}, pattern={'MP-ON': '洋葱'}, function={'F-PL': '盘'})
TODAY = date(2026, 11, 4)


def item(iid, brand='Meissen', pattern='MP-ON', function='F-PL', price=80, currency='EUR', label=None, ai=None):
    return dict(item_id=iid, brand=brand, pattern_id=pattern, function_id=function, listed_price=price,
                currency=currency, label=label, ai_score=ai)


def onion_world():
    """梅森洋葱盘 20 件有标签、留下 6 件；另有 40 件别的品牌全去掉（全局留下率 6/60 = 10%）；目标件 t 没有标签。"""
    out = [item('o%d' % k, label=1 if k < 6 else 0, price=60 + k) for k in range(20)]
    out += [item('x%d' % k, brand='Herend', pattern=None, function='F-CUP', price=40, label=0) for k in range(40)]
    out.append(item('t', price=70))
    return out


# ---------- 成分与理由（I/O 矩阵） ----------

def test_many_peers_taste_above_global_and_reason_excludes_self():
    items = onion_world()
    cnt, g = judge.taste_counts(items, PARAMS)
    assert g == pytest.approx(0.1)
    rate, why = judge.taste(item('t', price=70), cnt, g, PARAMS)
    assert rate > g and (why['kept'], why['total']) == (6, 20)
    out = rules.composite_scores(items, {}, NAMES, PARAMS)
    assert '口味 ' in out['t']['reason'] and '梅森·洋葱·盘：你留下 6/20' in out['t']['reason']
    # 留一：有标签的那几件看不到自己的标签
    assert '你留下 5/19' in out['o0']['reason'] and '你留下 6/19' in out['o10']['reason']
    assert out['t']['momo'] > out['x0']['momo']


def test_no_brand_global_rate_value_50_trend_lowest():
    items = onion_world() + [item('nb', brand=None, pattern=None, function=None, price=30)]
    cnt, g = judge.taste_counts(items, PARAMS)
    assert judge.taste(items[-1], cnt, g, PARAMS)[0] == pytest.approx(g)
    demand = {('b', 'Meissen'): 10.0, ('b', 'Herend'): 1.0}
    out = rules.composite_scores(items, demand, NAMES, PARAMS)
    nb = out['nb']
    assert nb['value'] == 50.0
    assert nb['trend'] == min(r['trend'] for r in out.values()) == 0.0
    assert '无品牌' in nb['reason'] and '性价比 50（无品牌）' in nb['reason']


def test_cheap_but_poor_taste_is_gated():
    parts = dict(momo=20.0, value=95.0, trend=40.0, customer=0.0)
    with_value = judge.composite_total(parts, PARAMS)
    without = judge.composite_total(dict(parts, value=0.0), PARAMS)
    assert with_value - without == pytest.approx(25 * 95 * 0.04 / 100)
    assert with_value == pytest.approx((55 * 20 + 25 * 95 * 0.04 + 20 * 40) / 100)


def test_non_euro_value_is_50():
    items = onion_world() + [item('gb', price=50, currency='GBP')]
    out = rules.composite_scores(items, {}, NAMES, PARAMS)
    assert out['gb']['value'] == 50.0 and '性价比 50（非欧元标价）' in out['gb']['reason']
    assert '同类中位 €' in out['t']['reason']


def test_value_cheaper_than_peers_ranks_higher():
    items = onion_world()
    out = rules.composite_scores(items + [item('cheap', price=20), item('dear', price=400)], {}, NAMES, PARAMS)
    assert out['cheap']['value'] > out['t']['value'] > out['dear']['value']


def test_ai_score_blends_half():
    items = onion_world()
    items[-1] = item('t', price=70, ai=90)
    items.append(item('u', price=70, ai=10))
    out = rules.composite_scores(items, {}, NAMES, PARAMS)
    plain = judge.percentiles({it['item_id']: judge.taste(it, *judge.taste_counts(items, PARAMS), PARAMS)[0] for it in items})
    assert out['t']['momo'] == pytest.approx(0.5 * plain['t'] + 0.5 * 100)
    assert out['u']['momo'] == pytest.approx(0.5 * plain['u'] + 0.5 * 0)
    assert 'AI 分 90' in out['t']['reason']


def test_gc_demand_brand_pattern_function_decay_and_refunds():
    aliases = [('meissen', 'Meissen'), ('梅森', 'Meissen'), ('赫伦', 'Herend')]
    patterns, functions = [('蓝洋葱', 'MP-ON'), ('洋葱', 'MP-ON')], [('盘', 'F-PL')]
    orders = [dict(product_name='梅森洋葱盘', session_date='2026-11-04'),
              dict(product_name='梅森洋葱盘', session_date='2026-05-08'),     # 180 天前：减半
              dict(product_name='梅森洋葱盘', session_date='2026-11-01', refunded=True),
              dict(product_name='某杯子', maker_guess='Herend', session_date='bad')]
    dem = rules.gc_demand(orders, aliases, patterns, functions, TODAY, PARAMS)
    assert dem[('b', 'Meissen')] == pytest.approx(1.5) and dem[('bp', 'Meissen', 'MP-ON')] == pytest.approx(1.5)
    assert dem[('bf', 'Meissen', 'F-PL')] == pytest.approx(1.5)
    assert dem[('b', 'Herend')] == pytest.approx(0.5 ** (365 / 180))
    it = item('t')
    assert judge.trend(it, dem, PARAMS) > judge.trend(item('h', brand='Herend'), dem, PARAMS) > 0


def test_customer_weight_grows_with_data():
    assert rules.customer_weights(0, PARAMS) == (55.0, 0.0)
    m, c = rules.customer_weights(5, PARAMS)
    assert (m, c) == pytest.approx((55 - 17.5, 17.5))
    m, c = rules.customer_weights(1000, PARAMS)
    assert c < 35 and m + c == pytest.approx(55)


def test_percentiles_none_is_50():
    assert judge.percentiles({'a': 1, 'b': 2, 'c': 3, 'd': None}) == {'a': 0.0, 'b': 50.0, 'c': 100.0, 'd': 50.0}


def test_reason_format():
    items = onion_world()
    r = rules.composite_scores(items, {('b', 'Meissen'): 3.0}, NAMES, PARAMS)['t']['reason']
    # 形如 "口味 82（梅森·洋葱·盘：你留下 6/20）· 性价比 40（同类中位 €80）· 客群 75"
    assert re.fullmatch(r'口味 \d+（梅森·洋葱·盘：你留下 6/20）· 性价比 \d+（同类中位 €70）· 客群 \d+', r), r


def test_override_unknown_and_valid():
    p = scoring_override(PARAMS, {'value': 35, 'trend_pattern': 2, 'min_peers': 3})
    assert p['weights']['value'] == 35 and p['trend_weights']['pattern'] == 2 and p['min_peers'] == 3
    assert PARAMS['weights']['value'] == 25          # 原参数不变
    with pytest.raises(ValueError, match='未知参数'):
        scoring_override(PARAMS, {'bogus': 1})


# ---------- 重算与存储 ----------

@pytest.fixture
def db(tmp_path):
    return Store(tmp_path / 'curio.db')


def add(db, iid, state='new', brand='Meissen', pattern='MP-ON', function='F-PL', price=80, currency='EUR', score=None,
        source='rule', k=0, backlog=0):
    fs = (datetime(2026, 10, 1, tzinfo=timezone.utc) - timedelta(minutes=k)).isoformat(timespec='seconds')
    with db.connect() as c:
        c.execute("""INSERT INTO items(item_id,platform,title,listed_price,currency,status,first_seen,rule_version)
            VALUES(?,'ebay',?,?,?,'active',?,1)""", (iid, 'Meissen Teller ' + iid, price, currency, fs))
        c.execute('INSERT INTO triage(item_id,state,backlog,updated_at) VALUES(?,?,?,?)', (iid, state, backlog, fs))
        if brand:
            c.execute("""INSERT INTO std_names(item_id,std_name,std_key,status,brand,pattern_id,function_id,kb_status,generated_at)
                VALUES(?,?,?,'named',?,?,?,'ok',?)""", (iid, 'Meissen 盘 ' + iid, brand + '|x', brand, pattern, function, fs))
        if score is not None:
            c.execute('INSERT INTO scores(item_id,score,source,reason,rule_version,scored_at) VALUES(?,?,?,?,1,?)',
                      (iid, score, source, 'old', fs))


def stock(db):
    for k in range(20):
        add(db, 'o%d' % k, state='kept' if k < 6 else 'removed', price=60 + k, k=k)
    for k in range(5):
        add(db, 'n%d' % k, price=50 + 10 * k, k=30 + k, score=40)
    add(db, 'bl', backlog=1, k=40)
    add(db, 'ar', state='auto_removed', k=41)
    add(db, 'nobrand', brand=None, k=42)


def gc_dir(tmp_path, rows):
    d = tmp_path / 'gc'
    (d / 'orders').mkdir(parents=True)
    (d / 'orders' / 'order_items.jsonl').write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in rows) + '\nnot json\n')
    return str(d)


def test_refresh_writes_every_item_in_one_go(db, tmp_path):
    stock(db)
    gc = gc_dir(tmp_path, [dict(product_name='梅森盘', session_date='2026-11-01')])
    out = scoring.refresh(db, ROOT, gc_db=gc, today=TODAY)
    assert out['composite_status'] == 'ok' and out['composite_trend'] == 'ok'
    with db.connect() as c:
        n_items = c.execute('SELECT count(*) FROM items').fetchone()[0]
        rows = {r['item_id']: dict(r) for r in c.execute('SELECT * FROM composite_scores')}
    assert out['composite_count'] == n_items == len(rows)          # 含 backlog、自动移出、已处理
    assert {'bl', 'ar', 'o0'} <= set(rows)
    assert all(0 <= r['score'] <= 100 and r['reason'] and r['version'].startswith('C:') for r in rows.values())
    assert rows['nobrand']['trend'] == 0.0
    # 再算一次是整表重写，不留旧行
    with db.connect() as c:
        c.execute("DELETE FROM items WHERE item_id='ar'")
    scoring.refresh(db, ROOT, gc_db=gc, today=TODAY)
    with db.connect() as c:
        assert c.execute("SELECT count(*) FROM composite_scores WHERE item_id='ar'").fetchone()[0] == 0


def test_goodcustomer_no_usable_orders_counts_as_missing(db, tmp_path):
    stock(db)
    gc = gc_dir(tmp_path, [dict(product_name='梅森盘', session_date='2026-11-01', refunded=True)])     # 另有一行坏行
    out = scoring.refresh(db, ROOT, gc_db=gc, today=TODAY)
    assert out['composite_trend'] == 'missing'
    with db.connect() as c:
        assert all(r['trend'] == 50.0 and '客群数据缺' in r['reason'] for r in c.execute('SELECT trend, reason FROM composite_scores'))


def test_goodcustomer_missing_trend_50_and_warning(db, tmp_path, caplog):
    stock(db)
    with caplog.at_level(logging.WARNING, logger='curio.scoring'):
        out = scoring.refresh(db, ROOT, gc_db=str(tmp_path / 'nope'), today=TODAY)
    assert out['composite_trend'] == 'missing'
    assert any('GoodCustomer' in r.getMessage() for r in caplog.records)
    with db.connect() as c:
        rows = c.execute('SELECT trend, reason FROM composite_scores').fetchall()
    assert rows and all(r['trend'] == 50.0 and '客群数据缺' in r['reason'] for r in rows)


def test_seen_unpicked_counts_as_not_kept(db):
    add(db, 'a', k=0)
    add(db, 'b', k=1)
    out = db.record_owner_events([dict(event_id='e1', type='seen', item_id='a', session_id='s')])
    assert out['dropped'] == 0
    with db.connect() as c:
        labels = {it['item_id']: it['label'] for it in scoring.load_items(c)}
    assert labels == {'a': 0, 'b': None}


def test_refresh_failure_only_records_status(db):
    with patch('curio.scoring.compute', side_effect=RuntimeError('boom')):
        out = scoring.refresh_for_run(db, ROOT, 0)
    assert out['composite_status'] == 'error' and 'boom' in out['composite_error']


def test_card_prefers_composite_and_falls_back(db, tmp_path):
    stock(db)
    scoring.refresh(db, ROOT, gc_db=str(tmp_path / 'nope'), today=TODAY)
    with db.connect() as c:
        cs = c.execute("SELECT score, reason FROM composite_scores WHERE item_id='n0'").fetchone()
        got = card(c, c.execute("SELECT * FROM items WHERE item_id='n0'").fetchone())
        assert got['score'] == round(cs['score']) and got['score_reason'] == cs['reason']
        c.execute("DELETE FROM composite_scores WHERE item_id='n0'")
        got = card(c, c.execute("SELECT * FROM items WHERE item_id='n0'").fetchone())
        assert (got['score'], got['score_reason']) == (40, 'old')


def test_pool_orders_by_composite(db):
    for iid, s in (('a', 10.0), ('b', 90.0), ('c', None)):
        add(db, iid, state='kept', score=50)
        if s is not None:
            with db.connect() as c:
                c.execute("INSERT INTO composite_scores VALUES(?,?,0,0,0,0,'r','C:x','t')", (iid, s))
    out = ranking.pool(db)
    assert [r['item_id'] for r in out['items']] == ['b', 'c', 'a']     # 无综合分的 c 退回旧分 50
    assert all(r['pool_at'] for r in out['items'])


def test_seal_uses_composite_with_fallback(db):
    with patch('curio.agreement._utcnow', lambda: datetime(2026, 11, 4, 12, tzinfo=BERLIN).astimezone(timezone.utc)):
        add(db, 'a', score=95, k=0)
        add(db, 'b', score=10, k=1)
        add(db, 'c', score=60, k=2)
        with db.connect() as c:
            c.execute("INSERT INTO composite_scores VALUES('a',20,0,0,0,0,'r','C:v1','t')")
            c.execute("INSERT INTO composite_scores VALUES('b',80,0,0,0,0,'r','C:v1','t')")
        assert agreement.seal_store(db, TODAY, 'test')['sealed'] == ['pool', 'rank']
        with db.connect() as c:
            row = c.execute("SELECT * FROM daily_predictions WHERE kind='pool'").fetchone()
    assert json.loads(row['items']) == ['b', 'c', 'a']
    assert json.loads(row['source'])['counts'] == {'composite': 2, 'rule': 1}
    assert json.loads(row['version']) == dict(method=2, rule_versions=[1], composite_versions=['C:v1'])


def test_seal_rank_uses_composite(db):
    with patch('curio.agreement._utcnow', lambda: datetime(2026, 11, 4, 12, tzinfo=BERLIN).astimezone(timezone.utc)):
        for k, (iid, old, comp) in enumerate((('p', 90, 10.0), ('q', 10, 90.0), ('r', 50, 50.0))):
            add(db, iid, score=old, k=k)
            db.triage_act('keep', iid, 'k-' + iid)                # 留下才有货号、进商品池
            with db.connect() as c:
                c.execute("INSERT OR REPLACE INTO composite_scores VALUES(?,?,0,0,0,0,'r','C:v1','t')", (iid, comp))
        agreement.seal_store(db, TODAY, 'test')
        with db.connect() as c:
            row = c.execute("SELECT * FROM daily_predictions WHERE kind='rank'").fetchone()
    assert json.loads(row['items']) == ['q', 'r', 'p']           # 综合分与旧分相反：按综合分
    assert json.loads(row['source'])['counts'] == {'composite': 3}


def _compare_mod():
    import importlib.util
    spec = importlib.util.spec_from_file_location('score_compare', ROOT / 'scripts/score_compare.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_compare_order_by_matches_seal_sql_on_ties(db):
    mod = _compare_mod()
    # 同分、first_seen 有早有晚、有空的；还有无分的
    for k, iid in enumerate(('b', 'a', 'c', 'd', 'e')):
        add(db, iid, k=k)
    with db.connect() as c:
        c.execute("UPDATE items SET first_seen='' WHERE item_id='c'")
        c.execute("UPDATE items SET first_seen=NULL WHERE item_id='d'")
        c.execute("UPDATE items SET first_seen=(SELECT first_seen FROM items WHERE item_id='b') WHERE item_id='a'")
        for iid in ('a', 'b', 'c', 'd'):
            c.execute("INSERT INTO composite_scores VALUES(?,50,0,0,0,0,'r','C:v1','t')", (iid,))
        sql_order = [r[0] for r in agreement.prediction_rows(c, 'pool')]
        fs = mod._first_seen(c)
        scores = {r['item_id']: dict(score=r['score']) for r in c.execute('SELECT * FROM composite_scores')}
    assert sql_order[-1] == 'e'
    assert mod.order_by(['e', 'd', 'c', 'b', 'a'], scores, fs) == sql_order


def test_compare_sealed_agreement_matches_07(db):
    mod = _compare_mod()
    clock = datetime(2026, 11, 4, 9, tzinfo=BERLIN).astimezone(timezone.utc)
    iso = lambda: clock.isoformat(timespec='seconds')
    with patch('curio.agreement._utcnow', lambda: clock), patch('curio.triage.now', iso), patch('curio.store.now', iso):
        for k in range(6):
            add(db, 'i%d' % k, score=90 - k, k=k)
        agreement.seal_store(db, TODAY, 'test')
        clock = datetime(2026, 11, 4, 10, tzinfo=BERLIN).astimezone(timezone.utc)
        db.record_owner_events([dict(event_id='e%d' % k, type='seen', item_id='i%d' % k, session_id='s') for k in range(6)])
        for iid in ('i0', 'i4'):
            db.triage_act('keep', iid, 'k-' + iid)
        with db.connect() as c:
            want = agreement.pool_agreement(c, TODAY, cards=False)
            got = mod.sealed_agreement(c, TODAY, None, mod._first_seen(c))
    assert want['status'] == 'ok' and want['n'] == 2
    assert got['agreement'] == want['agreement'] == 0.5 and got['baseline'] == want['baseline']


def test_compare_header_shares_and_caveat(db, tmp_path):
    mod = _compare_mod()
    stock(db)
    buf = []
    mod.main(['--set', 'value=35', '--db', str(db.path), '--gc', str(tmp_path / 'nope')], out=buf.append)
    text = '\n'.join(buf)
    assert '当前：口味 55.0% · 性价比 25.0% · 客群 20.0% · 客人 0.0%' in text
    assert '对比：口味 50.0% · 性价比 31.8% · 客群 18.2% · 客人 0.0%' in text
    assert '只拿"当前参数"与"对比参数"两栏互相比较' in text


def test_compare_retries_then_gives_up(db, capsys):
    mod = _compare_mod()
    calls = []

    def boom(path):
        calls.append(1)
        raise sqlite3.DatabaseError('database disk image is malformed')
    with patch.object(mod, 'connect_readonly', boom), patch.object(mod, 'READ_PAUSE', 0):
        with pytest.raises(SystemExit) as e:
            mod.main(['--set', 'value=35', '--db', str(db.path)])
    assert e.value.code == 1 and len(calls) == 3 and '读取 curio.db 失败' in capsys.readouterr().err


def test_leave_one_out_equals_counts_without_item():
    items = onion_world()
    target = dict(items[3])                 # 有标签（留下）的梅森洋葱盘
    cnt, g = judge.taste_counts(items, PARAMS)
    rate, why = judge.taste(target, cnt, g, PARAMS)
    rest = [it for it in items if it['item_id'] != target['item_id']]
    cnt2, g2 = judge.taste_counts(rest, PARAMS)
    rate2, why2 = judge.taste(dict(target, label=None), cnt2, g2, PARAMS)
    assert rate == pytest.approx(rate2) and why == why2


def test_percentiles_all_equal_is_50():
    assert judge.percentiles({'a': 0.1, 'b': 0.1, 'c': None}) == {'a': 50.0, 'b': 50.0, 'c': 50.0}
    assert judge.percentiles({'a': 7}) == {'a': 50.0}


def test_longer_alias_wins():
    with patch('curio.scoring._matching_brands', lambda root: {'Dresden Porcelain': ['德累斯顿'], 'Augarten': ['奥加滕', '维也纳奥加滕']}):
        _, aliases, _, _ = scoring.vocab_names(ROOT)
    names = [a for a, _ in aliases]
    assert names.index('维也纳奥加滕') < names.index('奥加滕') and names.index('德累斯顿') < names.index('奥加滕')
    assert rules.gc_demand([dict(product_name='维也纳奥加滕杯', session_date='2026-11-04')], aliases, [], [], TODAY, PARAMS) \
        == {('b', 'Augarten'): 1.0}


def test_version_includes_algo():
    v = scoring.version(PARAMS)
    with patch('curio.scoring.ALGO', scoring.ALGO + 1):
        assert scoring.version(PARAMS) != v


def test_collection_round_scores_everything_before_seal(tmp_path):
    from curio.curio_monitor import run
    shutil.copytree(ROOT / 'config', tmp_path / 'config')
    (tmp_path / '.env').write_text('EBAY_CLIENT_ID=dummy\nEBAY_CLIENT_SECRET=dummy\n')
    gc = gc_dir(tmp_path, [dict(product_name='梅森杯', session_date='2026-10-01')])
    noon = datetime(2026, 11, 3, 12, tzinfo=BERLIN).timestamp()

    def fetch(store, api, config):
        for k in range(3):
            store.ingest(dict(item_id='e%d' % k, platform='ebay', title='Meissen Tasse %d' % k, price=40 + k, currency='EUR',
                              buying='FIXED_PRICE', url='https://www.ebay.de/itm/x', image='https://i.ebayimg.com/x.jpg'), config)
        return 3
    with patch('curio.scoring._gc_db', lambda root: gc), \
         patch('curio.curio_monitor.collect_ebay', side_effect=fetch), \
         patch('curio.curio_monitor.collect_ka', side_effect=lambda s, c, b=None: s.set('ka_status', 'ok')), \
         patch('curio.curio_monitor.check_existing', return_value={'errors': 0}), \
         patch('curio.sync.run', return_value={'sync_status': 'disabled'}):
        first = run(tmp_path, lambda: noon)
        idle = run(tmp_path, lambda: noon + 120)
    assert first['composite_status'] == 'ok' and first['composite_trend'] == 'ok'
    assert first['prediction_status'] == 'sealed'
    assert idle['status'] == 'idle' and idle['composite_status'] == 'ok'
    db = Store(tmp_path / 'data/curio.db')
    with db.connect() as c:
        n = c.execute('SELECT count(*) FROM items').fetchone()[0]
        assert c.execute("SELECT count(*) FROM composite_scores WHERE reason<>''").fetchone()[0] == n == 3
        row = c.execute("SELECT * FROM daily_predictions WHERE kind='pool'").fetchone()
    assert json.loads(row['source'])['counts'] == {'composite': 3}


# ---------- 界面（无 JS 测试设施：只核对接线） ----------

def test_ui_wiring():
    js = (ROOT / 'curio/triage.js').read_text()
    assert 'function scoreOpen(id) { return handled.has(id) || beh.isSeen(id); }' in js
    assert "push('seen', id); revealScore(id);" in js and "if (action === 'keep') revealScore(id);" in js
    assert "sc.classList.add('veil')" in js
    html = (ROOT / 'curio/triage.html').read_text()
    assert 'id="sortScore"' in html and 'id="sortTime"' in html and '.fsc.veil{visibility:hidden}' in html
    rk = (ROOT / 'curio/ranking.js').read_text()
    assert 'function scoreEl(c)' in rk and "setPoolSort('time')" in rk


# ---------- 对比脚本 ----------

def _md5(path):
    return hashlib.md5(path.read_bytes()).hexdigest()


def test_compare_script_readonly(db, tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location('score_compare', ROOT / 'scripts/score_compare.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    stock(db)
    with patch('curio.agreement._utcnow', lambda: datetime(2026, 11, 4, 12, tzinfo=BERLIN).astimezone(timezone.utc)):
        agreement.seal_store(db, TODAY, 'test')
    path = db.path
    before = _md5(path)
    buf = io.StringIO()
    assert mod.main(['--set', 'value=35', '--db', str(path), '--gc', str(tmp_path / 'nope')],
                    out=lambda s='': buf.write(s + '\n')) == 0
    text = buf.getvalue()
    assert '回测' in text and '当前：前' in text and '对比：前' in text and '随机基线' in text
    assert 'R12 候选前 12' in text and '商品池前 20' in text and '2026-11-04' in text
    assert _md5(path) == before
    with pytest.raises(SystemExit) as e:
        mod.main(['--set', 'bogus=1', '--db', str(path)])
    assert e.value.code == 2
