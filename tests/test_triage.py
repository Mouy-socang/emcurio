import threading
import pytest
from curio import triage, market
from curio.config import load
from curio.store import Store
from scripts import migrate_triage


@pytest.fixture
def cfg():
    return dict(load(), keywords=['Meissen'])


@pytest.fixture
def db(tmp_path):
    return Store(tmp_path / 'curio.db')


def item(i='one', price=40, title='Meissen rote Rose Tasse'):
    return dict(item_id=i, platform='ebay', title=title, price=price, currency='EUR', buying='FIXED_PRICE',
                url='https://www.ebay.de/itm/example', image='')


def raw_insert(c, iid, agent='probe', first_seen='2026-11-01T00:00:00+00:00'):
    c.execute("INSERT INTO items(item_id,platform,title,description,status,agent_decision,url,first_seen,rule_version) "
              "VALUES(?,'ebay','Meissen Tasse','','active',?,'https://www.ebay.de/itm/x',?,1)", (iid, agent, first_seen))


def st(db, iid):
    with db.connect() as c:
        return triage.state(c, iid)


def test_ingest_places_new_or_auto_removed(db, cfg):
    db.ingest(item('ok'), cfg)
    db.ingest(item('bad', title='Meissen defekt Tasse'), cfg)
    assert st(db, 'ok') == 'new' and st(db, 'bad') == 'auto_removed'
    with db.connect() as c:
        ev = c.execute("SELECT * FROM triage_events WHERE item_id='ok'").fetchone()
        assert (ev['old_state'], ev['new_state'], ev['actor'], ev['action']) == (None, 'new', 'system', 'ingest')
        assert ev['agent_decision'] and ev['rule_version'] == 1 and ev['at'].endswith('+00:00')


def test_first_keep_then_rekeep_keeps_code(db, cfg):
    db.ingest(item('a'), cfg); db.ingest(item('b'), cfg)
    with db.connect() as c:
        assert triage.keep(c, 'a') == dict(state='kept', code='CO-0001')
    with db.connect() as c:
        assert triage.keep(c, 'b')['code'] == 'CO-0002'
        triage.remove(c, 'a', '不要了')
        assert triage.state(c, 'a') == 'removed'
        assert triage.keep(c, 'a') == dict(state='kept', code='CO-0001')
        assert triage.keep(c, 'a')['code'] == 'CO-0001'  # 重复留下不写事件
        assert c.execute('SELECT count(*) FROM item_codes').fetchone()[0] == 2
        ev = c.execute("SELECT * FROM triage_events WHERE item_id='a' AND action='remove'").fetchone()
        assert ev['actor'] == 'human' and ev['reason'] == '不要了' and ev['old_state'] == 'kept'


def test_code_format_past_9999(db):
    with db.connect() as c:
        raw_insert(c, 'x')
        c.execute("INSERT INTO sqlite_sequence(name,seq) VALUES('item_codes',9999)")
        assert triage.keep(c, 'x')['code'] == 'CO-10000'


def test_concurrent_keep_distinct_codes(db):
    ids = ['i%d' % n for n in range(20)]
    with db.connect() as c:
        for i in ids:
            raw_insert(c, i)
    errors, codes = [], []

    def worker(part):
        try:
            for i in part:
                codes.append(triage.run(db.path, lambda c, i=i: triage.keep(c, i))['code'])
        except Exception as e:  # pragma: no cover
            errors.append(e)
    threads = [threading.Thread(target=worker, args=(ids[k::2],)) for k in range(2)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert not errors
    assert len(set(codes)) == 20 and sorted(codes) == ['CO-%04d' % n for n in range(1, 21)]


def test_undo_keep_keeps_code_and_no_reuse(db):
    with db.connect() as c:
        raw_insert(c, 'a'); raw_insert(c, 'b')
        triage.keep(c, 'a')
        r = triage.undo(c)
        assert r['state'] == 'new' and r['code'] == 'CO-0001' and triage.state(c, 'a') == 'new'
        assert triage.keep(c, 'b')['code'] == 'CO-0002'
        assert triage.keep(c, 'a')['code'] == 'CO-0001'


def test_undo_three_steps_then_nothing(db):
    with db.connect() as c:
        raw_insert(c, 'a'); raw_insert(c, 'b', agent='skip')
        triage.keep(c, 'a'); triage.out_of_pool(c, 'a'); triage.rescue(c, 'b')
        triage.system_place(c, 'a')  # 系统动作不进撤销栈
        assert triage.undo(c)['item_id'] == 'b' and triage.state(c, 'b') == 'auto_removed'
        assert triage.undo(c)['state'] == 'kept'
        assert triage.undo(c)['state'] == 'new'
        with pytest.raises(triage.TriageError, match='没有可撤销的操作'):
            triage.undo(c)
        assert triage.code(c, 'a') == 'CO-0001'


def test_rescue_goes_to_front_of_queue(db):
    with db.connect() as c:
        raw_insert(c, 'old', first_seen='2026-10-30T00:00:00+00:00')
        raw_insert(c, 'newest', first_seen='2026-11-01T00:00:00+00:00')
        raw_insert(c, 'junk', agent='skip', first_seen='2026-10-01T00:00:00+00:00')
        for i in ('old', 'newest', 'junk'):
            triage.system_place(c, i)
        assert [r['item_id'] for r in triage.queue(c)] == ['newest', 'old']
        triage.rescue(c, 'junk')
        assert [r['item_id'] for r in triage.queue(c)] == ['junk', 'newest', 'old']
        with pytest.raises(triage.TriageError, match='不能捞回'):
            triage.rescue(c, 'old')


def test_rejudge_never_moves_human_items(db, cfg):
    db.ingest(item(), cfg)
    db.review('one', 'skip', '太贵', None, 'r1', 1)
    assert st(db, 'one') == 'removed'
    db.ingest(item(price=10), cfg)
    db.ingest(item(price=12, title='Meissen defekt Tasse'), cfg)
    assert st(db, 'one') == 'removed'
    with db.connect() as c:
        row = c.execute("SELECT agent_decision,rule_version FROM items WHERE item_id='one'").fetchone()
        assert row['agent_decision'] == 'skip' and row['rule_version'] == 3
        assert c.execute("SELECT count(*) FROM triage_events WHERE item_id='one' AND actor='system'").fetchone()[0] == 1


def test_system_rejudge_moves_untouched(db, cfg):
    db.ingest(item(), cfg)
    assert st(db, 'one') == 'new'
    db.ingest(item(price=30, title='Meissen defekt Tasse'), cfg)
    assert st(db, 'one') == 'auto_removed'
    db.ingest(item(price=20), cfg)
    assert st(db, 'one') == 'new'


def test_illegal_transition_rejected(db):
    with db.connect() as c:
        raw_insert(c, 'a')
        triage.remove(c, 'a')
        with pytest.raises(triage.TriageError, match='「去掉」状态的货不能移出商品池'):
            triage.out_of_pool(c, 'a')
        with pytest.raises(triage.TriageError):
            triage.back_to_pool(c, 'a')


def test_pool_out_and_back(db):
    with db.connect() as c:
        raw_insert(c, 'a')
        triage.keep(c, 'a'); triage.out_of_pool(c, 'a')
        assert triage.back_to_pool(c, 'a') == dict(state='kept', code='CO-0001')


def test_review_and_block_go_through_triage(db, cfg):
    db.ingest(item('w'), cfg); db.ingest(item('d', title='Meissen defekt'), cfg)
    db.ingest(item('p'), cfg); db.ingest(item('blk', title='Meissen Mietangebot'), cfg)
    db.review('w', 'want', '', None, 'r1', 1)
    db.review('d', 'want', '愿意修复', None, 'r2', 1)
    db.review('p', 'probe', '', None, 'r3', 1)
    db.block('blk', 'Mietangebot', 'b1', 1, cfg)
    with db.connect() as c:
        assert triage.code(c, 'w') == 'CO-0001' and triage.code(c, 'd') == 'CO-0002'
        assert triage.state(c, 'd') == 'kept'
        assert triage.state(c, 'p') == 'new' and triage.has_human(c, 'p')
        assert triage.state(c, 'blk') == 'removed'


def test_market_decision_removes_and_restores(tmp_path, monkeypatch):
    monkeypatch.setattr(market, 'DB', tmp_path / 'curio.db')
    store = Store(tmp_path / 'curio.db')
    with store.connect() as c:
        raw_insert(c, 'one', agent='want')
    market.init(store)
    market.set_decision('ebay', 'one', True, '无关')
    assert st(store, 'one') == 'removed'
    market.set_decision('ebay', 'one', False, '恢复')
    assert st(store, 'one') == 'new'


def test_migration_idempotent(tmp_path, capsys):
    store = Store(tmp_path / 'curio.db')
    with store.connect() as c:
        raw_insert(c, 'irr'); raw_insert(c, 'skip'); raw_insert(c, 'want'); raw_insert(c, 'probe', agent='skip')
        raw_insert(c, 'md'); raw_insert(c, 'mdold')
        raw_insert(c, 'old', first_seen='2026-01-01T00:00:00+00:00')
        raw_insert(c, 'oldskip', agent='skip', first_seen='2026-01-01T00:00:00+00:00')
        raw_insert(c, 'fresh', first_seen='2099-01-01T00:00:00+00:00')
        for n, (iid, d) in enumerate([('irr', 'irrelevant'), ('skip', 'skip'), ('want', 'want'), ('probe', 'probe'),
                                      ('mdold', 'want')]):
            c.execute("INSERT INTO cases(item_id,decision,reason,agent_decision,rule_version,decided_at,request_id) "
                      "VALUES(?,?,'x','probe',1,?,?)", (iid, d, '2026-10-0%dT00:00:00+00:00' % (n + 1), 'q%d' % n))
        c.execute("INSERT INTO market_decisions VALUES('ebay','md',1,'邮票','2026-10-10T00:00:00+00:00')")
        c.execute("INSERT INTO market_decisions VALUES('ebay','mdold',1,'早','2026-09-01T00:00:00+00:00')")
        c.execute("INSERT INTO kv VALUES('triage_migrated_at','2026-11-02T00:00:00+00:00')")
    first = migrate_triage.main([str(store.path)])
    assert first['new_codes'] == 2 and first['triage_rows'] == first['items'] == 9
    with store.connect() as c:
        s = {r[0]: r[1] for r in c.execute('SELECT item_id,state FROM triage')}
        assert s == dict(irr='removed', skip='removed', want='kept', probe='new', md='removed', mdold='kept',
                         old='new', oldskip='auto_removed', fresh='new')
        assert triage.code(c, 'want') == 'CO-0001' and triage.code(c, 'mdold') == 'CO-0002'
        ev = c.execute("SELECT * FROM triage_events WHERE item_id='irr' AND actor='human'").fetchone()
        assert ev['at'] == '2026-10-01T00:00:00+00:00' and ev['action'] == 'migrate'
        queue = [r['item_id'] for r in triage.queue(c)]
        assert queue[0] == 'fresh' and set(queue) == {'fresh', 'probe'}
        backlog = {r[0] for r in c.execute('SELECT item_id FROM triage WHERE backlog=1')}
        assert backlog == {'old'}
    second = migrate_triage.main([str(store.path)])
    assert second['new_events'] == 0 and second['new_codes'] == 0 and second['states'] == first['states']
