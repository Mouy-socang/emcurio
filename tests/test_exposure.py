"""故事 28：曝光衰减——累计挂牌天数、扣分、02 排序、03 天数与提示、07 封存 R12 预测、对比脚本。
全部用临时库，不读写真实 data/curio.db，不连外部主机。"""
import io
import json
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from curio import agreement, exposure, judge, ranking, rules, triage
from curio.config import ROOT, scoring_override, scoring_rules, scoring_validate
from curio.schedule import BERLIN
from curio.store import Store

PARAMS = scoring_rules(ROOT)
EXP = PARAMS['exposure']
NOW = datetime(2026, 11, 4, 12, tzinfo=BERLIN)


def at(days_ago, hour=10):
    return (NOW - timedelta(days=days_ago)).replace(hour=hour)


# ---------- 纯函数（I/O 矩阵） ----------

def test_params_from_yaml():
    assert EXP == dict(per_day=5.0, max=30.0, hint_days=3)


def test_never_published_no_penalty():
    assert rules.exposure_days([], NOW) == {}
    assert rules.exposure_penalty(0, EXP) == 0
    assert rules.exposure_sort_key(80, 0) == 80 and rules.exposure_sort_key(None, 0) is None


def test_four_days_on_rank():
    pubs = [(at(4), ['A', 'B']), (at(2), ['A']), (at(1), ['A', 'C'])]
    d = rules.exposure_days(pubs, NOW)
    assert d == {'A': 4, 'B': 2, 'C': 1}
    assert rules.exposure_penalty(d['A'], EXP) == 20


def test_dropped_off_keeps_penalty_capped():
    pubs = [(at(8), ['A']), (at(2), ['B'])]           # A 挂了 6 天，2 天前那次发布不再含它
    d = rules.exposure_days(pubs, NOW)
    assert d['A'] == 6 and rules.exposure_penalty(d['A'], EXP) == 30


def test_back_on_rank_accumulates():
    pubs = [(at(5), ['A']), (at(3), ['B']), (at(1), ['A'])]     # 挂 2 天、下榜、又挂 1 天
    d = rules.exposure_days(pubs, NOW)
    assert d['A'] == 3 and rules.exposure_penalty(d['A'], EXP) == 15


def test_same_day_publishes_and_berlin_dates():
    # 当天多次发布首尾相接；按柏林日期：柏林 23:30 发布到次日 00:30 也算 1 天
    pubs = [(at(1, 8), ['A']), (at(1, 12), ['A']), (at(1, 18), ['A'])]
    assert rules.exposure_days(pubs, NOW) == {'A': 1}
    late = datetime(2026, 11, 3, 23, 30, tzinfo=BERLIN).astimezone(timezone.utc)
    assert rules.exposure_days([(late, ['A'])], datetime(2026, 11, 4, 0, 30, tzinfo=BERLIN)) == {'A': 1}
    assert rules.exposure_days([(at(0, 9), ['A'])], NOW) == {}       # 今天才发布：0 天


def test_validate_exposure():
    raw = json.loads(json.dumps(PARAMS))
    for bad in (dict(per_day=-1, max=30, hint_days=3), dict(per_day=5, max=30, hint_days=0),
                dict(per_day=5, max=30, hint_days=1.5), dict(per_day=5, max=30)):
        with pytest.raises(ValueError, match='exposure'):
            scoring_validate(dict(raw, exposure=bad))
    p = scoring_override(PARAMS, {'exposure.per_day': 10, 'exposure.hint_days': 2})
    assert p['exposure'] == dict(per_day=10.0, max=30.0, hint_days=2) and EXP['per_day'] == 5.0
    with pytest.raises(ValueError, match='未知参数'):
        scoring_override(PARAMS, {'exposure.bogus': 1})


def test_exposure_not_in_composite_version():
    from curio import scoring
    assert scoring.version(PARAMS) == scoring.version(scoring_override(PARAMS, {'exposure.per_day': 9}))


# ---------- 读库 ----------

@pytest.fixture
def db(tmp_path):
    return Store(tmp_path / 'curio.db')


def add(db, iid, comp=None, k=0, keep=True):
    fs = (datetime(2026, 10, 1, tzinfo=timezone.utc) - timedelta(minutes=k)).isoformat(timespec='seconds')
    with db.connect() as c:
        c.execute("""INSERT INTO items(item_id,platform,title,listed_price,currency,status,first_seen,rule_version)
            VALUES(?,'ebay',?,80,'EUR','active',?,1)""", (iid, 'Meissen Teller ' + iid, fs))
        c.execute("INSERT INTO triage(item_id,state,backlog,updated_at) VALUES(?,'new',0,?)", (iid, fs))
        if comp is not None:
            c.execute("INSERT INTO composite_scores VALUES(?,?,0,0,0,0,'r','C:v1','t')", (iid, comp))
    if keep:
        db.triage_act('keep', iid, 'k-' + iid)
    with db.connect() as c:
        return triage.code(c, iid)


def publish_row(db, no, when, codes, status='delivered', raw=None):
    with db.connect() as c:
        c.execute("INSERT INTO publishes(publish_no,event_id,created_at,rank,pool_size,tombstones,images,status) "
                  "VALUES(?,?,?,?,0,0,0,?)",
                  (no, 'e%d' % no, when.astimezone(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z'),
                   raw if raw is not None else json.dumps(codes), status))


def set_rank(db, codes):
    with db.connect() as c:
        c.execute('DELETE FROM publish_rank')
        c.executemany('INSERT INTO publish_rank(pos,code) VALUES(?,?)', list(enumerate(codes, 1)))


def clock(t=NOW):
    return patch('curio.exposure._utcnow', lambda: t.astimezone(timezone.utc))


def test_bad_rank_json_skipped_and_dropped_ignored(db, caplog):
    a = add(db, 'a', 50)
    publish_row(db, 1, at(4), [a])
    publish_row(db, 2, at(3), None, raw='not json')
    publish_row(db, 3, at(2), [], status='dropped')
    with db.connect() as c, caplog.at_level(logging.WARNING):
        assert exposure.days_by_code(c, NOW) == {a: 4}       # 坏的那次跳过：第一次挂到现在
    assert '解析失败' in caplog.text


def test_pool_sort_key_and_displayed_scores(db):
    a = add(db, 'a', 80, k=0)
    b = add(db, 'b', 78, k=1)
    add(db, 'n', None, k=2)
    publish_row(db, 1, at(3), [a])                   # a 累计挂了 3 天：扣 15
    set_rank(db, [a])
    with clock():
        out = ranking.pool(db)
    rows = {r['item_id']: r for r in out['items']}
    assert [r['item_id'] for r in out['items'][:2]] == ['b', 'a']
    assert rows['a']['score'] == 80 and rows['b']['score'] == 78
    assert (rows['a']['exposure_days'], rows['a']['exposure_penalty'], rows['a']['sort_key']) == (3, 15, 65)
    assert (rows['b']['exposure_days'], rows['b']['exposure_penalty'], rows['b']['sort_key']) == (0, 0, 78)
    with db.connect() as c:
        assert c.execute('SELECT score FROM composite_scores WHERE item_id=?', ('a',)).fetchone()[0] == 80


def test_never_published_pool_order_unchanged(db):
    for k, (iid, s) in enumerate((('a', 10.0), ('b', 90.0), ('c', 50.0))):
        add(db, iid, s, k=k)
    with clock():
        out = ranking.pool(db)
    assert [r['item_id'] for r in out['items']] == ['b', 'c', 'a']
    assert all(r['exposure_penalty'] == 0 for r in out['items'])


def test_rank_rows_days_and_hint(db):
    a = add(db, 'a', 80, k=0)
    b = add(db, 'b', 70, k=1)
    c_ = add(db, 'c', 60, k=2)
    d = add(db, 'd', 50, k=3)
    publish_row(db, 1, at(4), [a, b])
    publish_row(db, 2, at(2), [a, d])                # b 挂 2 天后下榜
    set_rank(db, [a, b, c_, d])
    with clock():
        rows = {r['code']: r for r in ranking.rank_state(db)['rows']}
    assert (rows[a]['days_on_rank'], rows[a]['stale_hint']) == (4, True)
    assert (rows[b]['days_on_rank'], rows[b]['stale_hint']) == (2, False)
    assert (rows[c_]['days_on_rank'], rows[c_]['stale_hint']) == (0, False)     # 从没发布过：不显示
    assert (rows[d]['days_on_rank'], rows[d]['stale_hint']) == (2, False)
    assert list(rows) == [a, b, c_, d]               # 不改墨墨排好的顺序，也不移出


def test_sold_no_hint(db):
    a = add(db, 'a', 80)
    publish_row(db, 1, at(5), [a])
    set_rank(db, [a])
    ranking.pool_action(db, 'mark_sold', 'a', 'r-sold')
    with clock():
        assert ranking.rank_state(db)['rows'] == []          # 标记售出照旧从榜里拿掉
        set_rank(db, [a])                                     # 即使仍在榜上：已售出不提示
        row = ranking.rank_state(db)['rows'][0]
    assert row['days_on_rank'] == 5 and row['stale_hint'] is False


def test_dropped_off_not_on_03_penalty_stays(db):
    a = add(db, 'a', 80, k=0)
    b = add(db, 'b', 60, k=1)
    publish_row(db, 1, at(8), [a])
    publish_row(db, 2, at(2), [b])
    set_rank(db, [b])
    with clock():
        assert [r['code'] for r in ranking.rank_state(db)['rows']] == [b]
        rows = {r['item_id']: r for r in ranking.pool(db)['items']}
    assert rows['a']['exposure_penalty'] == 30 and rows['a']['sort_key'] == 50
    assert rows['b']['exposure_penalty'] == 10 and rows['b']['sort_key'] == 50


def test_seal_rank_uses_exposure_method_3(db):
    t = datetime(2026, 11, 4, 12, tzinfo=BERLIN).astimezone(timezone.utc)
    with patch('curio.agreement._utcnow', lambda: t), clock():
        a = add(db, 'a', 80, k=0)
        add(db, 'b', 78, k=1)
        publish_row(db, 1, at(3), [a])
        assert agreement.seal_store(db, NOW.date(), 'test')['sealed'] == ['pool', 'rank']
        with db.connect() as c:
            row = c.execute("SELECT * FROM daily_predictions WHERE kind='rank'").fetchone()
            pool_row = c.execute("SELECT * FROM daily_predictions WHERE kind='pool'").fetchone()
    assert json.loads(row['items']) == ['b', 'a']
    assert json.loads(row['scores']) == [78, 80]                  # 封存的仍是综合分本身
    v = json.loads(row['version'])
    assert v['method'] == agreement.METHOD == 3 and v['exposure'] == EXP
    assert 'exposure' not in json.loads(pool_row['version'])


def test_seal_rank_ties_follow_first_seen(db):
    with clock():
        a = add(db, 'a', 80, k=5)                  # 80 − 15 = 65，与 b 同键；b 的 first_seen 更新
        add(db, 'b', 65, k=0)
        publish_row(db, 1, at(3), [a])
        with db.connect() as c:
            assert [r['item_id'] for r in agreement.prediction_rows(c, 'rank')] == ['b', 'a']
            assert [r['item_id'] for r in agreement.prediction_rows(
                c, 'rank', exposure_params=dict(per_day=0, max=0, hint_days=3))] == ['a', 'b']


# ---------- 对比脚本 ----------

def _compare_mod():
    import importlib.util
    spec = importlib.util.spec_from_file_location('score_compare', ROOT / 'scripts/score_compare.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_compare_exposure_readonly(db, tmp_path):
    import hashlib
    mod = _compare_mod()
    t = datetime(2026, 11, 4, 12, tzinfo=BERLIN).astimezone(timezone.utc)
    with patch('curio.agreement._utcnow', lambda: t), clock():
        a = add(db, 'a', 80, k=0)
        b = add(db, 'b', 78, k=1)
        publish_row(db, 1, at(3), [a, b])
        publish_row(db, 2, at(1), [a])
        agreement.seal_store(db, NOW.date(), 'test')
        before = hashlib.md5(db.path.read_bytes()).hexdigest()
        buf = io.StringIO()
        with patch('curio.scoring.compute', lambda c, root, params, gc_db, today: dict(
                scores={'a': dict(score=80.0), 'b': dict(score=78.0)}, items=[], demand_ok=True,
                version='C:x', params=params)):
            assert mod.main(['--set', 'exposure.per_day=1', '--db', str(db.path), '--gc', str(tmp_path / 'nope')],
                            out=lambda s='': buf.write(s + '\n')) == 0
    text = buf.getvalue()
    assert hashlib.md5(db.path.read_bytes()).hexdigest() == before
    assert '曝光扣分' in text and '每天 5 分' in text and '每天 1 分' in text
    assert 'R12 候选前 12' in text and '3b. 已封存日重算的 07 R12 符合度' in text
    # 当前参数：a 80−15=65、b 78−10=68 → b 在前；对比参数（每天 1 分）：a 77、b 76 → a 在前
    sec = text.split('R12 候选前 12')[1].split('==', 2)[1]
    first = [ln for ln in sec.splitlines() if ln.strip()][0]
    assert first.startswith(' 1 ←   2') and '80.0 −15' in first and '80.0 −3' in first


def test_hint_only_for_live_unsold_at_hint_days(db):
    g = add(db, 'g', 80, k=0)
    e = add(db, 'e', 70, k=1)
    f = add(db, 'f', 60, k=2)
    h = EXP['hint_days']
    publish_row(db, 1, at(5), [g])
    publish_row(db, 2, at(h), [g, e])
    publish_row(db, 3, at(h - 1), [g, e, f])
    set_rank(db, [g, e, f])
    with db.connect() as c:
        c.execute("UPDATE items SET status='gone' WHERE item_id='g'")
    with clock():
        rows = {r['code']: r for r in ranking.rank_state(db)['rows']}
    assert (rows[g]['days_on_rank'], rows[g]['gone'], rows[g]['stale_hint']) == (5, True, False)   # 已下架：不提示
    assert (rows[e]['days_on_rank'], rows[e]['stale_hint']) == (h, True)                            # 正好挂满：提示
    assert (rows[f]['days_on_rank'], rows[f]['stale_hint']) == (h - 1, False)                       # 差一天：不提示


def test_pool_equal_sort_key_later_state_first(db):
    add(db, 'a', 80, k=0)                      # 80 − 10 = 70
    add(db, 'b', 70, k=1)
    with db.connect() as c:
        c.execute("UPDATE triage SET updated_at='2026-11-01T10:00:00+00:00' WHERE item_id='a'")
        c.execute("UPDATE triage SET updated_at='2026-11-02T10:00:00+00:00' WHERE item_id='b'")
        code = triage.code(c, 'a')
    publish_row(db, 1, at(2), [code])
    with clock():
        rows = ranking.pool(db)['items']
    assert [(r['item_id'], r['sort_key']) for r in rows] == [('b', 70), ('a', 70)]


def test_compare_3b_rank_overlap_differs(db, tmp_path):
    mod = _compare_mod()
    t = NOW.astimezone(timezone.utc)
    scores = {'a': dict(score=80.0)}
    with patch('curio.agreement._utcnow', lambda: t), clock():
        a = add(db, 'a', 80, k=0)
        for k in range(12):                    # 12 件 70 分：当前参数下 a（80 − 15 = 65）挤不进前 12
            add(db, 'f%d' % k, 70, k=k + 1)
            scores['f%d' % k] = dict(score=70.0)
        publish_row(db, 1, at(3), [a])
        publish_row(db, 2, at(0, 9), [a])          # 当天的 R12 只有 a
        agreement.seal_store(db, NOW.date(), 'test')
        buf = io.StringIO()
        with patch('curio.scoring.compute', lambda c, root, params, gc_db, today: dict(
                scores=scores, items=[], demand_ok=True, version='C:x', params=params)):
            assert mod.main(['--set', 'exposure.per_day=1', '--db', str(db.path), '--gc', str(tmp_path / 'nope')],
                            out=lambda s='': buf.write(s + '\n')) == 0
    sec = buf.getvalue().split('3b. 已封存日重算的 07 R12 符合度')[1]
    assert '只拿"当前参数"与"对比参数"两栏互相比较' in sec
    assert '2026-11-04：0/12 / 0/12 / 1/12' in sec       # a 在对比参数（77 分）下进前 12


def test_compare_keyed_handles_missing_score():
    mod = _compare_mod()
    assert mod.keyed({'a': dict(score=None), 'b': dict(score=60.0)}, {'b': dict(days=1, penalty=5)}) == {'b': dict(score=55.0)}
