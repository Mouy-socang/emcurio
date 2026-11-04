"""故事 23：每日封存预测与符合度（封存不可改、进池符合度 / 基线 / 提升、榜重合、差异清单、历史固定、接口）。
全部用临时库，不连外部主机；时间固定在 TODAY（柏林）——agreement、拣选、行为事件与排榜请求用同一只假时钟。"""
import json
import re
import shutil
import threading
import urllib.error
import urllib.request
import uuid
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from curio import agreement, publish, ranking
from curio.config import ROOT
from curio.schedule import BERLIN
from curio.store import Store

TODAY = date(2026, 11, 4)
YESTERDAY = TODAY - timedelta(days=1)
D2 = TODAY - timedelta(days=2)


class Clock:
    def __init__(self):
        self.at(TODAY, 12)

    def at(self, day, hour, minute=0):
        self.dt = datetime(day.year, day.month, day.day, hour, minute, tzinfo=BERLIN).astimezone(timezone.utc)

    def iso(self):
        return self.dt.isoformat(timespec='seconds')


@pytest.fixture(autouse=True)
def clock():
    k = Clock()
    with patch('curio.agreement._utcnow', lambda: k.dt), patch('curio.triage.now', k.iso), \
         patch('curio.store.now', k.iso), patch('curio.ranking.now', k.iso):
        yield k


@pytest.fixture
def db(tmp_path):
    return Store(tmp_path / 'curio.db')


def add(db, iid, score=None, k=0, state='new', price=50):
    """直接写一件新货（first_seen 随 k 变早，同分时 k 小的排前）。"""
    fs = (datetime(2026, 10, 1, tzinfo=timezone.utc) - timedelta(minutes=k)).isoformat(timespec='seconds')
    with db.connect() as c:
        c.execute("""INSERT INTO items(item_id,platform,title,listed_price,currency,status,first_seen,rule_version)
            VALUES(?,'ebay',?,?,'EUR','active',?,1)""", (iid, 'Meissen Tasse ' + iid, price, fs))
        c.execute("INSERT INTO triage(item_id,state,backlog,updated_at) VALUES(?,?,0,?)", (iid, state, fs))
        if score is not None:
            c.execute("INSERT INTO scores(item_id,score,source,reason,rule_version,scored_at) VALUES(?,?,'ai','x',1,?)",
                      (iid, score, fs))


def keep(db, iid):
    return db.triage_act('keep', iid, 'k-' + uuid.uuid4().hex)


def seen(db, iids, session='s1'):
    for n in range(0, len(iids), 100):
        out = db.record_owner_events([dict(event_id=uuid.uuid4().hex, type='seen', item_id=i, session_id=session)
                                      for i in iids[n:n + 100]])
        assert out['dropped'] == 0


def seal(db, day=None, session=None):
    return agreement.seal_store(db, day, 'test', session)['sealed']


def result(db, day=None):
    return agreement.day_result(db, day, now_day=TODAY)


def pool_ids(db, day=TODAY):
    with db.connect() as c:
        return json.loads(c.execute("SELECT items FROM daily_predictions WHERE day=? AND kind='pool'",
                                    (day.isoformat(),)).fetchone()[0])


def normal_day(db):
    """200 件按分数 s0 > s1 > …；另有 30 件封存了但没被看到。"""
    for k in range(200):
        add(db, 's%d' % k, score=max(0, 100 - k // 2), k=k)
    for k in range(30):
        add(db, 'x%d' % k, score=100, k=-100 - k)      # 分数最高、但墨墨没看到
    assert seal(db) == ['pool', 'rank']
    seen(db, ['s%d' % k for k in range(200)])
    for k in list(range(12)) + list(range(100, 108)):
        keep(db, 's%d' % k)


# ---------- 封存 ----------

def test_seal_once_and_immutable(db):
    add(db, 'a', 90, 0)
    add(db, 'b', 10, 1)
    add(db, 'c', None, 2)
    add(db, 'd', 50, 3)
    assert seal(db) == ['pool', 'rank']
    add(db, 'late', 99, -5)
    assert seal(db) == []                       # 当天再跑不覆盖
    with db.connect() as c:
        row = c.execute("SELECT * FROM daily_predictions WHERE day=? AND kind='pool'", (TODAY.isoformat(),)).fetchone()
        assert json.loads(row['items']) == ['a', 'd', 'b', 'c']     # 分数高到低，无分最后；不含后来入库的
        assert json.loads(row['scores']) == [90, 50, 10, None]
        assert json.loads(row['source']) == dict(table='composite_scores', fallback='scores', counts={'ai': 3, 'none': 1})
        assert json.loads(row['version']) == dict(method=2, rule_versions=[1], composite_versions=[])
        with pytest.raises(Exception, match='不可修改'):
            c.execute("UPDATE daily_predictions SET items='[]' WHERE day=?", (TODAY.isoformat(),))


def test_delete_blocked(db):
    add(db, 'a', 50)
    seal(db)
    with db.connect() as c:
        c.execute("INSERT INTO daily_agreement VALUES('2026-01-01','{}','x')")
    with db.connect() as c:
        with pytest.raises(Exception, match='不可修改'):
            c.execute('DELETE FROM daily_predictions')
    with db.connect() as c:
        with pytest.raises(Exception, match='不可修改'):
            c.execute('DELETE FROM daily_agreement')


def test_rank_prediction_uses_pool(db):
    for k, s in enumerate((30, 80, 60)):
        add(db, 'p%d' % k, s, k)
        keep(db, 'p%d' % k)
    add(db, 'n0', 99, 9)                        # 新货不进 rank 预测
    seal(db)
    with db.connect() as c:
        rank = json.loads(c.execute("SELECT items FROM daily_predictions WHERE kind='rank'").fetchone()[0])
    assert rank == ['p1', 'p2', 'p0'] and pool_ids(db) == ['n0']


def test_seal_for_run_after_five_berlin(db):
    day = datetime(2026, 11, 3, tzinfo=BERLIN)
    early = (day + timedelta(hours=4, minutes=55)).timestamp()
    late = (day + timedelta(hours=5, minutes=1)).timestamp()
    add(db, 'a', 50)
    assert agreement.seal_for_run(db, early) == dict(prediction_status='before_seal_hour')
    assert agreement.seal_for_run(db, late) == dict(prediction_status='sealed', prediction_day='2026-11-03')
    assert agreement.seal_for_run(db, late + 600)['prediction_status'] == 'already_sealed'


def test_seal_failure_does_not_raise(db):
    ts = datetime(2026, 11, 3, 12, tzinfo=BERLIN).timestamp()
    with patch('curio.agreement.seal_store', side_effect=RuntimeError('boom')):
        out = agreement.seal_for_run(db, ts)
    assert out['prediction_status'] == 'error' and 'boom' in out['prediction_error']


def test_collection_round_seals_after_fetch(tmp_path):
    from curio.curio_monitor import run
    shutil.copytree(ROOT / 'config', tmp_path / 'config')
    (tmp_path / '.env').write_text('EBAY_CLIENT_ID=dummy\nEBAY_CLIENT_SECRET=dummy\n')
    noon = datetime(2026, 11, 3, 12, tzinfo=BERLIN).timestamp()

    def fetch(store, api, config):            # 本轮抓到的第一批新货
        store.ingest(dict(item_id='e1', platform='ebay', title='Meissen Tasse', price=40, currency='EUR',
                          buying='FIXED_PRICE', url='https://www.ebay.de/itm/x', image='https://i.ebayimg.com/x.jpg'), config)
        return 1
    with patch('curio.curio_monitor.collect_ebay', side_effect=fetch), \
         patch('curio.curio_monitor.collect_ka', side_effect=lambda s, c, b=None: s.set('ka_status', 'ok')), \
         patch('curio.curio_monitor.check_existing', return_value={'errors': 0}), \
         patch('curio.sync.run', return_value={'sync_status': 'disabled'}):
        first = run(tmp_path, lambda: noon)
        idle = run(tmp_path, lambda: noon + 120)        # 高峰时段间隔 5 分钟：2 分钟后仍空闲
    assert first['prediction_status'] == 'sealed' and first['prediction_day'] == '2026-11-03'
    assert idle['status'] == 'idle' and idle['prediction_status'] == 'already_sealed'
    db = Store(tmp_path / 'data/curio.db')
    assert 'e1' in pool_ids(db, date(2026, 11, 3))


def test_seal_excludes_earlier_session_seen_keeps_restored_and_current(db, clock):
    for k, iid in enumerate(('a', 'b', 'c', 'd')):
        add(db, iid, 50, k)
    clock.at(YESTERDAY, 20)
    seen(db, ['a', 'b'], session='s0')
    db.record_owner_events([dict(event_id='r1', type='restore', item_id='b', session_id='s0')])
    clock.at(TODAY, 9)
    seen(db, ['c'], session='s1')               # 本次打开里刚看到
    assert agreement.seal_for_feed(db, 's1') is True
    assert pool_ids(db) == ['b', 'c', 'd']      # a 早先看过未选：排除；b 放回了；c 是本次看到的


# ---------- 进池符合度 ----------

def test_normal_day(db):
    normal_day(db)
    p = result(db)['pool']
    assert p['status'] == 'ok' and (p['n'], p['seen'], p['hits']) == (20, 200, 12)
    assert p['agreement'] == pytest.approx(0.6) and p['baseline'] == pytest.approx(0.1) and p['lift'] == pytest.approx(6.0)
    assert [r['item_id'] for r in p['you_only']] == ['s%d' % k for k in range(100, 108)]
    assert [r['item_id'] for r in p['system_only']] == ['s%d' % k for k in range(12, 20)]
    row = p['system_only'][0]
    assert row['system_pos'] == 30 + 13 and row['system_score'] == 94    # 30 件 x 在前
    assert row['name'] and row['price'] == '€50' and 'image' in row
    assert p['kept_in_top'] == 12 and p['system_top'] == 100 and p['late_seal'] is False


def test_no_keep_is_no_data(db):
    for k in range(50):
        add(db, 's%d' % k, 50, k)
    seal(db)
    seen(db, ['s%d' % k for k in range(50)])
    p = result(db)['pool']
    assert p['status'] == 'no_data' and p['n'] == 0 and 'agreement' not in p


def test_late_item_and_undo_not_counted(db):
    for k in range(10):
        add(db, 's%d' % k, 90 - k, k)
    seal(db)
    add(db, 'late', 99, -1)                     # 下午才入库
    seen(db, ['s%d' % k for k in range(10)] + ['late'])
    keep(db, 'late')
    keep(db, 's0')
    keep(db, 's5')
    db.triage_act('undo', None, 'u1')           # 撤销最近一次：s5
    p = result(db)['pool']
    assert (p['n'], p['seen'], p['hits']) == (1, 10, 1) and p['agreement'] == 1.0


def test_keep_without_seen_counts_in_v(db):
    for k in range(3):
        add(db, 's%d' % k, 90 - k, k)
    seal(db)
    keep(db, 's2')                              # 没有 seen 事件
    p = result(db)['pool']
    assert (p['n'], p['seen']) == (1, 1) and p['baseline'] == 1.0 and p['baseline'] <= 1


def test_late_seal_flag(db, clock):
    for k in range(5):
        add(db, 's%d' % k, 90 - k, k)
    clock.at(TODAY, 4)
    keep(db, 's0')                              # 封存前就开始挑
    clock.at(TODAY, 6)
    seal(db)
    clock.at(TODAY, 7)
    seen(db, ['s1', 's2', 's3'])
    keep(db, 's2')
    p = result(db)['pool']
    assert p['late_seal'] is True and p['late_decisions'] == 1 and p['first_decision_at'].startswith('2026-11-04T03:00')
    assert 's0' not in pool_ids(db) and (p['n'], p['seen'], p['hits']) == (1, 3, 0)


def test_no_prediction_and_before_first_seal(db):
    add(db, 'a', 50)
    keep(db, 'a')
    r = result(db)
    assert r['pool']['status'] == 'no_data' and r['pool']['reason'] == 'before_first_seal'
    seal(db, D2)
    r = result(db, YESTERDAY)                   # 首次封存之后、当天没封存
    assert r['pool']['status'] == 'no_prediction' and r['rank']['status'] == 'no_prediction'
    assert result(db, D2 - timedelta(days=5))['pool']['reason'] == 'before_first_seal'
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM daily_agreement').fetchone()[0] == 0


# ---------- 榜 ----------

def stocked_pool(db, n=20):
    for k in range(n):
        add(db, 'p%d' % k, 100 - k, k)
        keep(db, 'p%d' % k)
    with db.connect() as c:
        return {r[0]: r[1] for r in c.execute('SELECT item_id,code FROM item_codes')}


def test_rank_overlap_from_saved_rank(db):
    codes = stocked_pool(db)
    seal(db)
    assert result(db)['rank']['status'] == 'no_data'          # 当天没发布、榜没改
    mine = [codes['p%d' % k] for k in (0, 1, 2, 3, 4, 12, 13, 14, 15, 16, 17, 18)]
    ranking.save_rank(db, mine, 'r1')
    r = result(db)['rank']
    assert r['status'] == 'ok' and r['source'] == 'rank' and r['overlap'] == 5 and r['size'] == 12


def add_publish(db, no, codes, status='pending', at=None):
    at = at or datetime(2026, 11, 4, 10, tzinfo=timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    with db.connect() as c:
        c.execute("""INSERT INTO publishes(publish_no,event_id,created_at,rank,pool_size,tombstones,images,status)
            VALUES(?,?,?,?,17,0,0,?)""", (no, 'e%d' % no, at, json.dumps(codes), status))


def test_rank_overlap_from_last_publish(db):
    codes = stocked_pool(db)
    seal(db)
    ranking.save_rank(db, [codes['p%d' % k] for k in range(12)], 'r1')
    add_publish(db, 1, [codes['p%d' % k] for k in range(12)])
    add_publish(db, 2, [codes['p%d' % k] for k in (0, 1, 2, 3, 4, 12, 13, 14, 15, 16, 7, 8)])
    r = result(db)['rank']
    assert r['source'] == 'publish' and r['overlap'] == 7       # 取当天最后一次发布


def test_dropped_last_publish_uses_earlier(db):
    codes = stocked_pool(db)
    seal(db)
    add_publish(db, 1, [codes['p%d' % k] for k in (0, 1, 2, 12, 13, 14, 15, 16, 17, 18, 19, 11)])
    add_publish(db, 2, [codes['p%d' % k] for k in range(12)], status='dropped')
    r = result(db)['rank']
    assert r['source'] == 'publish' and r['overlap'] == 4


def test_past_day_rank_uses_that_days_last_save(db, clock):
    clock.at(D2, 10)
    codes = stocked_pool(db)
    seal(db)
    ranking.save_rank(db, [codes['p%d' % k] for k in range(12)], 'r1')
    ranking.save_rank(db, [codes['p%d' % k] for k in (0, 1, 2, 12, 13, 14, 15, 16, 17, 18, 19, 11)], 'r2')
    clock.at(TODAY, 10)
    ranking.save_rank(db, [codes['p%d' % k] for k in range(12)], 'r3')        # 今天又改回去
    r = result(db, D2)['rank']
    assert r['status'] == 'ok' and r['overlap'] == 4


def test_pool_action_removing_ranked_item_counts_as_change(db, clock):
    clock.at(YESTERDAY, 10)
    codes = stocked_pool(db)
    ranking.save_rank(db, [codes['p%d' % k] for k in range(12)], 'r1')
    clock.at(TODAY, 10)
    seal(db)
    ranking.pool_action(db, 'out_of_pool', 'p0', 'o1')
    ranking.pool_action(db, 'mark_sold', 'p1', 'o2')
    r = result(db)['rank']
    assert r['status'] == 'ok' and r['source'] == 'rank' and r['overlap'] == 10
    with db.connect() as c:
        assert len(publish.rank(c)) == 10


# ---------- 历史固定 ----------

def test_d2_frozen_and_yesterday_live(db, clock):
    for k in range(10):
        add(db, 's%d' % k, 90 - k, k)
    clock.at(D2, 10)
    seal(db)
    seen(db, ['s%d' % k for k in range(10)])
    keep(db, 's3')
    add(db, 'y', 80, 20)                        # 前天之后才入库，进昨天的封存
    clock.at(YESTERDAY, 10)
    seal(db)
    clock.at(TODAY, 12)
    first = result(db, D2)
    assert first['final'] and first['pool']['hits'] == 0 and first['pool']['n'] == 1
    y1 = result(db, YESTERDAY)
    assert y1['final'] is False and y1['pool']['status'] == 'no_data'
    clock.at(D2, 18)
    keep(db, 's0')                              # 之后又补进前天的记录：结果不变
    clock.at(YESTERDAY, 18)
    seen(db, ['y'])
    keep(db, 'y')                               # 昨天晚到的记录仍计入
    clock.at(TODAY, 12)
    assert result(db, D2) == first
    assert result(db, YESTERDAY)['pool']['n'] == 1
    with db.connect() as c:
        assert [r[0] for r in c.execute('SELECT day FROM daily_agreement')] == [D2.isoformat()]


def test_history_series(db):
    normal_day(db)
    h = agreement.history(db, 30, now_day=TODAY)['days']
    assert len(h) == 30 and h[-1]['day'] == TODAY.isoformat() and h[0]['day'] == (TODAY - timedelta(days=29)).isoformat()
    assert h[-1]['agreement'] == pytest.approx(0.6) and h[-1]['baseline'] == pytest.approx(0.1)
    assert all(d['status'] == 'no_data' for d in h[:-1])
    with pytest.raises(ValueError):
        agreement.history(db, 0)
    with pytest.raises(ValueError):
        agreement.day_result(db, TODAY + timedelta(days=1), now_day=TODAY)


def test_history_no_card_lookups(db):
    normal_day(db)
    with patch('curio.store.card', side_effect=AssertionError('card')):
        agreement.history(db, 30, now_day=TODAY)


# ---------- 接口 ----------

@pytest.fixture
def http(db):
    from curio.review import server
    httpd = server(db, port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = 'http://127.0.0.1:' + str(httpd.server_port)

    def get(path):
        try:
            with urllib.request.urlopen(base + path) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)
    yield get
    httpd.shutdown()


def triggers(db):
    with db.connect() as c:
        return [r[0] for r in c.execute('SELECT trigger FROM daily_predictions')]


def test_feed_request_seals_after_five_and_without_cursor(db, http, clock):
    add(db, 'a', 50)
    clock.at(TODAY, 4)
    assert http('/api/triage/feed?session=s9')[0] == 200 and triggers(db) == []      # 05:00 前不封存
    clock.at(TODAY, 8)
    assert http('/api/triage/feed?session=s9&cursor=WyJ4Il0')[0] in (200, 400) and triggers(db) == []   # 续载不封存
    assert http('/api/triage/feed?session=s9')[0] == 200
    assert set(triggers(db)) == {'feed'}


def test_agreement_api(db, http):
    normal_day(db)
    status, d = http('/api/agreement')
    assert status == 200 and d['today'] == TODAY.isoformat()
    assert d['day']['pool']['agreement'] == pytest.approx(0.6) and d['prev']['pool']['status'] == 'no_data'
    assert len(d['day']['pool']['you_only']) == 8 and len(d['history']) == 30
    assert d['history'][-1]['agreement'] == pytest.approx(0.6)
    status, h = http('/api/agreement/history?days=30')
    assert status == 200 and len(h['days']) == 30
    assert http('/api/agreement?day=bad') == (400, {'error': '日期无效'})
    assert http('/api/agreement?day=0001-01-01') == (400, {'error': '日期无效'})
    assert http('/api/agreement/history?days=999')[0] == 400
    assert http('/api/agreement/history?days=abc') == (400, {'error': '天数须为 1–90'})


def test_page_has_section():
    page = (ROOT / 'curio/triage.html').read_text()
    assert 'id="agreeView"' in page and 'href="/#agree"' in page and re.search(r'>07<', page)
