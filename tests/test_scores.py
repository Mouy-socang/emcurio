"""故事 6（CAP-4）：可能要分数——规则基线分、AI 打分（全部 mock，不联网）、预算、快照、排序、复算。"""
import json, shutil
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch
import pytest
import requests
from curio import ai, triage, rules
from curio.config import load, anthropic_key
from curio.schedule import BERLIN
from curio.store import Store, refresh_baseline, write_score
from scripts import score_baseline, top20_keep_rate

ROOT = Path(__file__).resolve().parents[1]
NOON = datetime(2026, 11, 3, 12, tzinfo=BERLIN).timestamp()
KEY = 'sk-ant-KEY-SENTINEL-123'


@pytest.fixture(autouse=True)
def no_network():
    """任何真实 HTTP 请求都直接失败。"""
    with patch('requests.sessions.Session.request', side_effect=AssertionError('测试不得联网')):
        yield


@pytest.fixture(autouse=True)
def no_sleep():
    """重试等待不真睡；记录等待秒数。"""
    slept = []
    with patch('curio.ai._sleep', side_effect=slept.append):
        yield slept


@pytest.fixture
def cfg():
    return dict(load(), keywords=['Meissen'])


@pytest.fixture
def db(tmp_path):
    return Store(tmp_path / 'curio.db')


def item(i, price=40, title='Meissen Zwiebelmuster Tasse', description=''):
    return dict(item_id=i, platform='ebay', title=title, description=description, price=price, currency='EUR',
                buying='FIXED_PRICE', url='https://www.ebay.de/itm/x', image='')


def score_row(db, iid):
    with db.connect() as c:
        r = c.execute('SELECT * FROM scores WHERE item_id=?', (iid,)).fetchone()
        return dict(r) if r else None


def resp(status, payload=None):
    r = Mock(status_code=status)
    r.json.return_value = payload
    return r


def answer(rows):
    return resp(200, {'content': [{'type': 'text', 'text': json.dumps(rows, ensure_ascii=False)}]})


# ---- 规则基线分

def test_baseline_score_components():
    base = dict(agent_decision='probe')
    assert rules.baseline_score(dict(agent_decision='want'))[0] > rules.baseline_score(base)[0]
    full = dict(pattern_id='p', form_id='f', function_id='FN-1')
    assert rules.baseline_score(base, full)[0] > rules.baseline_score(base, dict(form_id='f'))[0] > rules.baseline_score(base)[0]
    one, two = rules.baseline_score(base, dict(seller_grade='I.Wahl')), rules.baseline_score(base, dict(seller_grade='2. Wahl'))
    assert one[0] > rules.baseline_score(base)[0] > two[0] and '2. Wahl' in two[1]
    assert rules.baseline_score(base, dict(overpaint=1))[0] == rules.baseline_score(base)[0]   # 外画不减分
    s, reason = rules.baseline_score(dict(agent_decision='want'), dict(full, seller_grade='erste Wahl'))
    assert 0 <= s <= 100 and isinstance(s, int) and '图案' in reason
    assert rules.seller_grade_level('IV.Wahl') == 4 and rules.seller_grade_level('zweite Wahl') == 2
    assert rules.seller_grade_level('2 .Wahl') == 2 and rules.seller_grade_level('Wahlweise') is None
    assert rules.seller_grade_level('1 Wahl') == 1 and rules.seller_grade_level('III.Wahl') == 3
    assert rules.seller_grade_level('5. Wahl') is None and rules.seller_grade_level('15. Wahl') is None


def test_ingest_writes_baseline_only_for_new(db, cfg):
    db.ingest(item('ok'), cfg)
    db.ingest(item('bad', title='Meissen defekt Tasse'), cfg)
    row = score_row(db, 'ok')
    assert row['source'] == 'rule' and 0 <= row['score'] <= 100 and row['rule_version'] == 1 and row['reason']
    assert score_row(db, 'bad') is None


# ---- 无 key

def test_no_key_disabled_and_queue_by_baseline(tmp_path, db, cfg):
    (tmp_path / '.env').write_text('EBAY_CLIENT_ID=x\n')
    assert anthropic_key(tmp_path) is None
    db.ingest(item('a'), cfg)
    r = ai.run(db, tmp_path, cfg, lambda: NOON)
    assert (r['ai_status'], r['ai_scored'], r['ai_usage']['limit']) == ('disabled', 0, 100)
    assert score_row(db, 'a')['source'] == 'rule'


def test_collection_round_reports_disabled(tmp_path):
    from curio.curio_monitor import run
    shutil.copytree(ROOT / 'config', tmp_path / 'config')
    (tmp_path / '.env').write_text('EBAY_CLIENT_ID=dummy\nEBAY_CLIENT_SECRET=dummy\n')
    with patch('curio.curio_monitor.collect_ebay', return_value=0), \
         patch('curio.curio_monitor.collect_ka', side_effect=lambda s, c, b=None: s.set('ka_status', 'ok')), \
         patch('curio.curio_monitor.check_existing', return_value={'errors': 0}):
        result = run(tmp_path, lambda: NOON)
    assert result['ai_status'] == 'disabled' and result['ai_scored'] == 0
    assert result['ebay_status'] == 'ok' and result['kleinanzeigen_status'] == 'ok'


def test_collection_round_ai_error_does_not_affect_others(tmp_path):
    from curio.curio_monitor import run
    shutil.copytree(ROOT / 'config', tmp_path / 'config')
    (tmp_path / '.env').write_text('EBAY_CLIENT_ID=dummy\nEBAY_CLIENT_SECRET=dummy\nANTHROPIC_API_KEY=' + KEY + '\n')
    def ebay(store, api, config):
        store.ingest(item('e1'), config)
    with patch('curio.curio_monitor.collect_ebay', side_effect=ebay), \
         patch('curio.curio_monitor.collect_ka', side_effect=lambda s, c, b=None: s.set('ka_status', 'ok')), \
         patch('curio.curio_monitor.check_existing', return_value={'errors': 0}), \
         patch('curio.ai.requests.post', return_value=resp(529)):
        result = run(tmp_path, lambda: NOON)
    assert result['ai_status'] == 'error' and KEY not in json.dumps(result) and 'ai_usage' in result
    assert result['ebay_status'] == 'ok' and result['ebay_new'] == 1
    assert score_row(Store(tmp_path / 'data/curio.db'), 'e1')['source'] == 'rule'


# ---- AI 打分

def test_ai_scores_three_items_and_request_shape(db, cfg):
    for i in ('a', 'b', 'c'):
        db.ingest(item(i, description='x' * 900), cfg)
    post = Mock(return_value=answer([{'item_id': i, 'score': s, 'reason': '图案好'} for i, s in (('a', 80), ('b', 55), ('c', 12))]))
    result = ai.score_round(db, KEY, 100, lambda: NOON, post)
    assert result['ai_status'] == 'ok' and result['ai_scored'] == 3
    for i, s in (('a', 80), ('b', 55), ('c', 12)):
        row = score_row(db, i)
        assert (row['score'], row['source'], row['model'], row['reason']) == (s, 'ai', 'claude-haiku-5-5', '图案好')
    (url,), kw = post.call_args
    assert url == 'https://api.anthropic.com/v1/messages'
    assert kw['headers'] == {'x-api-key': KEY, 'anthropic-version': '2026-10-21', 'content-type': 'application/json'}
    body = kw['json']
    assert body['model'] == 'claude-haiku-5-5' and not {'temperature', 'top_p', 'top_k'} & set(body)
    sent = json.loads(body['messages'][0]['content'].rsplit('\n', 1)[1])
    assert {x['item_id'] for x in sent} == {'a', 'b', 'c'} and all(len(x['description']) == 600 for x in sent)
    assert {'title', 'price_eur', 'std_name', 'overpaint', 'baseline_score'} <= set(sent[0])
    # 已是未过期的 AI 分：下一轮不再请求
    post.reset_mock()
    assert ai.score_round(db, KEY, 100, lambda: NOON, post)['ai_scored'] == 0 and not post.called


def test_ai_does_not_touch_triage(db, cfg):
    db.ingest(item('a'), cfg)
    with db.connect() as c:
        before = (c.execute('SELECT * FROM triage').fetchall(), c.execute('SELECT count(*) FROM triage_events').fetchone()[0],
                  c.execute("SELECT agent_decision,agent_reason FROM items").fetchall())
    ai.score_round(db, KEY, 100, lambda: NOON, Mock(return_value=answer([{'item_id': 'a', 'score': 5, 'reason': 'r'}])))
    with db.connect() as c:
        after = (c.execute('SELECT * FROM triage').fetchall(), c.execute('SELECT count(*) FROM triage_events').fetchone()[0],
                 c.execute("SELECT agent_decision,agent_reason FROM items").fetchall())
    assert [tuple(r) for r in before[0]] == [tuple(r) for r in after[0]] and before[1] == after[1]
    assert [tuple(r) for r in before[2]] == [tuple(r) for r in after[2]]


def test_partial_and_out_of_range_keep_old_score(db, cfg, caplog):
    for i in ('a', 'b', 'c'):
        db.ingest(item(i), cfg)
    old = {i: score_row(db, i) for i in 'bc'}
    post = Mock(return_value=answer([{'item_id': 'a', 'score': 70, 'reason': 'ok'}, {'item_id': 'b', 'score': 130, 'reason': 'x'}]))
    with caplog.at_level('WARNING'):
        result = ai.score_round(db, KEY, 100, lambda: NOON, post)
    assert result['ai_scored'] == 1 and result['ai_warnings'] == 1 and result['ai_status'] == 'ok'
    assert score_row(db, 'a')['source'] == 'ai'
    assert score_row(db, 'b') == old['b'] and score_row(db, 'c') == old['c']
    assert any('合法结果' in r.message for r in caplog.records)
    # 本轮不重复请求缺件的货
    assert post.call_count == 1


def test_parse_validation():
    ids = {'a', 'b', 'c', 'd'}
    text = '```json\n[{"item_id":"a","score":50,"reason":"%s"},{"item_id":"b","score":true},{"item_id":"zz","score":3},' \
           '{"item_id":"c","score":-1},{"item_id":"d","score":"9"}]\n```' % ('长' * 60)
    got = ai.parse(text, ids)
    assert list(got) == ['a'] and len(got['a'][1]) == 40
    assert ai.parse('不是 JSON', ids) == {} and ai.parse('[{"item_id":', ids) == {}


@pytest.mark.parametrize('failure', [resp(529), requests.Timeout('timeout with ' + KEY)])
def test_api_error_keeps_baseline_and_hides_key(db, cfg, failure, caplog):
    db.ingest(item('a'), cfg)
    old = score_row(db, 'a')
    post = Mock(side_effect=failure) if isinstance(failure, Exception) else Mock(return_value=failure)
    with caplog.at_level('DEBUG'):
        result = ai.score_round(db, KEY, 100, lambda: NOON, post)
    assert result['ai_status'] == 'error' and result['ai_scored'] == 0
    assert KEY not in json.dumps(result, ensure_ascii=False) and KEY not in caplog.text
    assert score_row(db, 'a') == old
    assert post.call_count == 2 and result['ai_usage']['used'] == 2       # 重试也计预算


def test_run_wrapper_turns_unexpected_errors_into_status(tmp_path, db, cfg):
    (tmp_path / '.env').write_text('ANTHROPIC_API_KEY=' + KEY + '\n')
    with patch('curio.ai.score_round', side_effect=RuntimeError(KEY)):
        result = ai.run(db, tmp_path, cfg, lambda: NOON)
    assert result['ai_status'] == 'error' and KEY not in json.dumps(result)


def test_budget_exhausted(db, cfg):
    db.ingest(item('a'), cfg)
    db.set(ai.KV_AI_USAGE, json.dumps({'date': '2026-11-03', 'requests': 100}))
    post = Mock()
    result = ai.score_round(db, KEY, 100, lambda: NOON, post)
    assert result['ai_status'] == 'budget_exhausted' and not post.called
    # 次日（柏林日期）归零
    tomorrow = datetime(2026, 11, 4, 9, tzinfo=BERLIN).timestamp()
    post.return_value = answer([{'item_id': 'a', 'score': 66, 'reason': 'r'}])
    assert ai.score_round(db, KEY, 100, lambda: tomorrow, post)['ai_status'] == 'ok'


def test_budget_runs_out_mid_round(db, cfg):
    for n in range(25):
        db.ingest(item('i%02d' % n), cfg)
    def post(url, headers, json, timeout):
        ids = [x['item_id'] for x in __import__('json').loads(json['messages'][0]['content'].rsplit('\n', 1)[1])]
        return answer([{'item_id': i, 'score': 50, 'reason': 'r'} for i in ids])
    result = ai.score_round(db, KEY, 1, lambda: NOON, post)
    assert result['ai_status'] == 'budget_exhausted' and result['ai_scored'] == 20


def test_price_change_makes_score_stale(db, cfg):
    db.ingest(item('a'), cfg)
    ai.score_round(db, KEY, 100, lambda: NOON, Mock(return_value=answer([{'item_id': 'a', 'score': 90, 'reason': 'r'}])))
    assert score_row(db, 'a')['source'] == 'ai'
    db.ingest(item('a', price=55), cfg)                          # 价格变化 → rule_version+1
    row = score_row(db, 'a')
    assert row['source'] == 'rule' and row['rule_version'] == 2
    post = Mock(return_value=answer([{'item_id': 'a', 'score': 61, 'reason': 'r2'}]))
    assert ai.score_round(db, KEY, 100, lambda: NOON, post)['ai_scored'] == 1
    assert (score_row(db, 'a')['score'], score_row(db, 'a')['rule_version']) == (61, 2)


def test_examples_from_human_decisions(db, cfg):
    db.ingest(item('k', title='Meissen Schwanenservice Teller'), cfg)
    db.ingest(item('r', title='Meissen Weinlaub Tasse'), cfg)
    with db.connect() as c:
        triage.keep(c, 'k', '喜欢天鹅')
        triage.remove(c, 'r', '太普通')
    with db.connect() as c:
        ex = ai.examples(c)
    assert ex == {'keep': [{'title': 'Meissen Schwanenservice Teller', 'reason': '喜欢天鹅'}],
                  'remove': [{'title': 'Meissen Weinlaub Tasse', 'reason': '太普通'}]}


# ---- 拣选快照与排序

def test_decision_snapshots_score(db, cfg):
    db.ingest(item('a'), cfg)
    with db.connect() as c:
        write_score(c, 'a', 72, 'ai', 'r', 1, model=ai.MODEL)
        triage.keep(c, 'a')
        ev = c.execute("SELECT * FROM triage_events WHERE item_id='a' AND action='keep'").fetchone()
        assert ev['score'] == 72 and ev['rule_version'] == 1 and ev['actor'] == 'human'


def test_queue_order_rescued_then_score_then_unscored(db, cfg):
    db.ingest(item('junk', title='Meissen defekt Teller'), cfg)
    for i in ('s90', 's40', 'none'):
        db.ingest(item(i), cfg)
    with db.connect() as c:
        write_score(c, 's90', 90, 'ai', 'r', 1)
        write_score(c, 's40', 40, 'ai', 'r', 1)
        c.execute("DELETE FROM scores WHERE item_id='none'")
        triage.rescue(c, 'junk')
    with db.connect() as c:
        q = triage.queue(c)
    assert [r['item_id'] for r in q] == ['junk', 's90', 's40', 'none']
    assert q[1]['score'] == 90 and q[1]['score_source'] == 'ai'


# ---- 脚本

def test_score_baseline_script_idempotent(tmp_path, db, cfg, capsys):
    for i in ('a', 'b'):
        db.ingest(item(i), cfg)
    with db.connect() as c:
        c.execute('DELETE FROM scores')
    first = score_baseline.main([str(db.path)])
    second = score_baseline.main([str(db.path)])
    assert (first['written'], second['written'], second['missing']) == (2, 0, 0)


def test_rejudge_all_backfills_baseline(db, cfg):
    db.ingest(item('a'), cfg)
    with db.connect() as c:
        c.execute('DELETE FROM scores')
    assert db.rejudge_all(cfg)['scores_written'] == 1
    assert db.rejudge_all(cfg)['scores_written'] == 0


def test_top20_keep_rate(tmp_path, db, cfg, capsys):
    top20_keep_rate.main([str(db.path)])
    assert '暂无人工' in capsys.readouterr().out
    for n in range(30):
        db.ingest(item('d%02d' % n), cfg)
    with db.connect() as c:
        for n in range(30):
            write_score(c, 'd%02d' % n, n, 'ai', 'r', 1)
        # 第一天：分数最高的 5 件（d20–d24）留下、其余 20 件去掉；第二天：d25–d29 只去掉
        for n in range(25):
            (triage.keep if n >= 20 else triage.remove)(c, 'd%02d' % n)
        c.execute("UPDATE triage_events SET at='2026-11-03T10:00:00+00:00' WHERE actor='human'")
        for n in range(25, 30):
            triage.remove(c, 'd%02d' % n)
        c.execute("UPDATE triage_events SET at='2026-11-04T10:00:00+00:00' WHERE actor='human' AND item_id>='d25'")
    r = top20_keep_rate.main([str(db.path)])
    out = capsys.readouterr().out
    d1, d2 = r['days']
    assert (d1['day'], d1['decided'], d1['kept'], d1['top_n'], d1['top_kept']) == ('2026-11-03', 25, 5, 20, 5)
    assert d1['top_rate'] == 0.25 and d1['avg_rate'] == 0.2
    assert (d2['decided'], d2['kept'], d2['top_rate']) == (5, 0, 0.0)
    assert '2026-11-03' in out and '合计' in out


# ---- 评审补充

def keep_resp(rows, stop='end_turn'):
    return resp(200, {'stop_reason': stop, 'content': [{'type': 'thinking', 'thinking': ''},
                                                       {'type': 'text', 'text': json.dumps(rows)}]})


def sent_ids(kw):
    return [x['item_id'] for x in json.loads(kw['json']['messages'][0]['content'].rsplit('\n', 1)[1])]


def st_new(db, iid):
    with db.connect() as c:
        return triage.state(c, iid) == 'new'


def test_rescue_and_undo_back_to_new_get_baseline(db, cfg):
    db.ingest(item('junk', title='Meissen defekt Teller'), cfg)
    db.ingest(item('k'), cfg)
    assert score_row(db, 'junk') is None
    with db.connect() as c:
        triage.rescue(c, 'junk')
    assert score_row(db, 'junk')['source'] == 'rule'
    with db.connect() as c:
        assert [r['item_id'] for r in triage.queue(c)][0] == 'junk' and triage.queue(c)[0]['score'] is not None
        triage.remove(c, 'junk')
        ev = c.execute("SELECT score FROM triage_events WHERE item_id='junk' AND action='remove'").fetchone()
        assert ev['score'] == score_row(db, 'junk')['score']
        triage.keep(c, 'k')
        c.execute("DELETE FROM scores WHERE item_id='k'")
        triage.undo(c, 'k')                       # 撤销留下 → 回到 new
    assert st_new(db, 'k') and score_row(db, 'k')['source'] == 'rule'


def test_mid_request_reingest_discards_ai_result(db, cfg):
    db.ingest(item('a'), cfg)
    def post(url, headers, json, timeout):
        db.ingest(item('a', price=77), cfg)       # 请求期间改价 → rule_version 2
        return answer([{'item_id': 'a', 'score': 99, 'reason': 'r'}])
    assert ai.score_round(db, KEY, 100, lambda: NOON, post)['ai_scored'] == 0
    row = score_row(db, 'a')
    assert (row['source'], row['rule_version']) == ('rule', 2)


def test_mid_request_keep_discards_ai_result(db, cfg):
    db.ingest(item('a'), cfg)
    def post(url, headers, json, timeout):
        with db.connect() as c:
            triage.keep(c, 'a')
        return answer([{'item_id': 'a', 'score': 99, 'reason': 'r'}])
    assert ai.score_round(db, KEY, 100, lambda: NOON, post)['ai_scored'] == 0
    assert score_row(db, 'a')['source'] == 'rule'


def test_examples_undo_latest_and_cap(db, cfg):
    for n in range(35):
        db.ingest(item('r%02d' % n, title='T%02d' % n), cfg)
    db.ingest(item('u', title='undone'), cfg)
    db.ingest(item('kr', title='kept then removed'), cfg)
    with db.connect() as c:
        triage.keep(c, 'u'); triage.undo(c, 'u')
        triage.keep(c, 'kr'); triage.remove(c, 'kr')
        for n in range(35):
            triage.remove(c, 'r%02d' % n)
        ex = ai.examples(c)
    assert ex['keep'] == [] and len(ex['remove']) == 30
    with db.connect() as c:
        for n in range(35):
            c.execute("DELETE FROM triage_events WHERE item_id=? AND action='remove'", ('r%02d' % n,))
        ex = ai.examples(c)
    assert [e['title'] for e in ex['remove']] == ['kept then removed'] and ex['keep'] == []


def test_attempt_cap_and_budget_exhausted_on_last_request(db, cfg):
    for n in range(60):
        db.ingest(item('i%02d' % n), cfg)
    post = Mock(side_effect=lambda url, headers, json, timeout: answer([{'item_id': i, 'score': 50, 'reason': 'r'} for i in sent_ids({'json': json})]))
    r = ai.score_round(db, KEY, 100, lambda: NOON, post, max_requests=2)
    assert post.call_count == 2 and r['ai_status'] == 'ok' and r['ai_stopped'] == 'round_limit' and r['ai_scored'] == 40
    for n in range(30):
        db.ingest(item('j%02d' % n), cfg)
    db.set(ai.KV_AI_USAGE, json.dumps({'date': '2026-11-03', 'requests': 0}))
    post.reset_mock()
    r = ai.score_round(db, KEY, 1, lambda: NOON, post, max_requests=2)
    assert post.call_count == 1 and r['ai_status'] == 'budget_exhausted'


def test_retry_counts_toward_attempt_cap(db, cfg):
    db.ingest(item('a'), cfg)
    post = Mock(return_value=resp(529))
    r = ai.score_round(db, KEY, 100, lambda: NOON, post, max_requests=1)
    assert post.call_count == 1 and r['ai_status'] == 'error'


def test_retry_succeeds_second_attempt_with_retry_after(db, cfg, no_sleep):
    db.ingest(item('a'), cfg)
    busy = resp(529); busy.headers = {'retry-after': '30'}
    post = Mock(side_effect=[busy, answer([{'item_id': 'a', 'score': 70, 'reason': 'r'}])])
    r = ai.score_round(db, KEY, 100, lambda: NOON, post)
    assert r['ai_status'] == 'ok' and r['ai_scored'] == 1 and post.call_count == 2
    assert no_sleep == [5.0]                       # retry-after 被截到 5 秒
    assert post.call_args.kwargs['timeout'] <= 120


def test_budget_runs_out_during_retry_reports_original_error(db, cfg):
    db.ingest(item('a'), cfg)
    db.set(ai.KV_AI_USAGE, json.dumps({'date': '2026-11-03', 'requests': 99}))
    r = ai.score_round(db, KEY, 100, lambda: NOON, Mock(return_value=resp(529)))
    assert r['ai_status'] == 'error' and 'HTTP 529' in r['ai_error']


def test_round_deadline_stops_before_request(db, cfg):
    db.ingest(item('a'), cfg)
    post = Mock()
    with patch('curio.ai._monotonic', side_effect=[0.0, 500.0, 500.0]):
        r = ai.score_round(db, KEY, 100, lambda: NOON, post)
    assert not post.called and r['ai_status'] == 'ok' and r['ai_stopped'] == 'round_limit'


@pytest.mark.parametrize('stop', ['max_tokens', 'refusal'])
def test_truncated_or_refused_is_batch_error_and_not_resent(db, cfg, stop, caplog):
    db.ingest(item('a'), cfg)
    post = Mock(return_value=keep_resp([{'item_id': 'a', 'score': 80, 'reason': 'r'}], stop))
    with caplog.at_level('WARNING'):
        r = ai.score_round(db, KEY, 100, lambda: NOON, post)
    assert r['ai_status'] == 'error' and stop in r['ai_error'] and score_row(db, 'a')['source'] == 'rule'
    assert any(stop in rec.message for rec in caplog.records)
    post.reset_mock()
    assert ai.score_round(db, KEY, 100, lambda: NOON + 3600, post)['ai_scored'] == 0 and not post.called


def test_omitted_item_skipped_for_24h(db, cfg):
    db.ingest(item('a'), cfg); db.ingest(item('b'), cfg)
    post = Mock(return_value=answer([{'item_id': 'a', 'score': 60, 'reason': 'r'}]))
    ai.score_round(db, KEY, 100, lambda: NOON, post)
    post.reset_mock()
    ai.score_round(db, KEY, 100, lambda: NOON + 3600, post)
    assert not post.called                          # b 一小时后不重发
    post.return_value = answer([{'item_id': 'b', 'score': 61, 'reason': 'r'}])
    ai.score_round(db, KEY, 100, lambda: NOON + 25 * 3600, post)
    assert sent_ids(post.call_args.kwargs) == ['b'] and score_row(db, 'b')['source'] == 'ai'


def test_payload_non_eur_and_untrusted_line(db, cfg):
    db.ingest(dict(item('usd'), currency='USD'), cfg)
    post = Mock(return_value=answer([]))
    ai.score_round(db, KEY, 100, lambda: NOON, post)
    body = post.call_args.kwargs['json']
    sent = json.loads(body['messages'][0]['content'].rsplit('\n', 1)[1])[0]
    assert (sent['price_eur'], sent['price'], sent['currency']) == (None, 40, 'USD')
    assert '不可信' in body['system'] and '忽略' in body['system']


def test_zero_budget_is_disabled(tmp_path, db, cfg):
    (tmp_path / '.env').write_text('ANTHROPIC_API_KEY=' + KEY + '\n')
    db.ingest(item('a'), cfg)
    r = ai.run(db, tmp_path, dict(cfg, ai_budget={'requests_per_day': 0}), lambda: NOON, Mock())
    assert r['ai_status'] == 'disabled' and r['ai_usage']['limit'] == 0


@pytest.mark.parametrize('value,error', [(None, None), ('5', '映射'), ({'per_day': 3}, '未知键'),
                                         ({'requests_per_day': -1}, '非负整数'), ({'requests_per_day': 2.5}, '非负整数'),
                                         ({'requests_per_day': True}, '非负整数'), ({'requests_per_day': 7}, None)])
def test_config_ai_budget_validation(tmp_path, value, error):
    import yaml
    shutil.copytree(ROOT / 'config', tmp_path / 'config')
    path = tmp_path / 'config/settings.yaml'
    settings = yaml.safe_load(path.read_text())
    if value is None:
        settings.pop('ai_budget', None)
    else:
        settings['ai_budget'] = value
    path.write_text(yaml.safe_dump(settings, allow_unicode=True))
    if error:
        with pytest.raises(ValueError, match=error):
            load(tmp_path)
    else:
        assert load(tmp_path)['ai_budget'] == {'requests_per_day': 100 if value is None else 7}


def test_top20_ties_and_scored_average(db, cfg):
    for i in ('x', 'y', 'z'):
        db.ingest(item(i), cfg)
    with db.connect() as c:
        write_score(c, 'x', 50, 'ai', 'r', 1); write_score(c, 'y', 50, 'ai', 'r', 1)
        c.execute("DELETE FROM scores WHERE item_id='z'")
        triage.keep(c, 'x'); triage.remove(c, 'y'); triage.remove(c, 'z')
        r = top20_keep_rate.report(c)
    d = r['days'][0]
    assert (d['decided'], d['unscored'], d['scored'], d['kept'], d['avg_rate']) == (3, 1, 2, 1, 0.5)
