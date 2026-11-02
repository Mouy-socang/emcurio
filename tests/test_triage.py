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


def sk(r):
    return r['state'], r['code']


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
        assert sk(triage.keep(c, 'a')) == ('kept', 'CO-0001')
    with db.connect() as c:
        assert triage.keep(c, 'b')['code'] == 'CO-0002'
        triage.remove(c, 'a', '不要了')
        assert triage.state(c, 'a') == 'removed'
        assert sk(triage.keep(c, 'a')) == ('kept', 'CO-0001')
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
        assert sk(triage.back_to_pool(c, 'a')) == ('kept', 'CO-0001')


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


def test_block_rescreen_moves_untouched_to_auto_removed(db, cfg):
    db.ingest(item('one', title='Meissen Mietangebot'), cfg)
    db.ingest(dict(item('two'), description='Mietangebot in Meißen'), cfg)
    assert st(db, 'two') == 'new'
    db.block('one', 'Mietangebot', 'b1', 1, cfg)
    with db.connect() as c:
        assert triage.state(c, 'two') == 'auto_removed'
        ev = c.execute("SELECT * FROM triage_events WHERE item_id='two' ORDER BY id DESC").fetchone()
        assert (ev['action'], ev['actor'], ev['old_state']) == ('rejudge', 'system', 'new')


def test_review_want_on_out_of_pool_keeps_code(db, cfg):
    db.ingest(item(), cfg)
    db.review('one', 'want', '', None, 'r1', 1)
    with db.connect() as c:
        triage.out_of_pool(c, 'one')
    db.review('one', 'want', '', None, 'r2', 1)
    with db.connect() as c:
        assert triage.state(c, 'one') == 'kept' and triage.code(c, 'one') == 'CO-0001'
        assert c.execute('SELECT count(*) FROM item_codes').fetchone()[0] == 1


def test_review_probe_on_auto_removed_enters_queue(db, cfg):
    db.ingest(item(title='Meissen defekt Tasse'), cfg)
    assert st(db, 'one') == 'auto_removed'
    db.review('one', 'probe', '', None, 'r1', 1)
    with db.connect() as c:
        assert triage.state(c, 'one') == 'new'
        assert [r['item_id'] for r in triage.queue(c)] == ['one']
        assert c.execute("SELECT count(*) FROM triage_events WHERE item_id='one' AND action='probe'").fetchone()[0] == 1


def test_one_undo_reverts_want_on_auto_removed(db, cfg):
    db.ingest(item(title='Meissen defekt Tasse'), cfg)
    db.review('one', 'want', '', None, 'r1', 1)
    with db.connect() as c:
        assert triage.state(c, 'one') == 'kept'
        triage.undo(c)
        assert triage.state(c, 'one') == 'auto_removed' and triage.code(c, 'one') == 'CO-0001'
        with pytest.raises(triage.TriageError, match='没有可撤销的操作'):
            triage.undo(c)


def test_undo_rejects_changed_state(db):
    with db.connect() as c:
        raw_insert(c, 'a')
        triage.keep(c, 'a'); triage.out_of_pool(c, 'a')
        ev = c.execute("SELECT * FROM triage_events WHERE item_id='a' AND action='keep'").fetchone()
        with pytest.raises(triage.TriageError, match='状态已变化'):
            triage.undo_event(c, ev)


def test_gone_item_not_in_queue(db):
    with db.connect() as c:
        raw_insert(c, 'live'); raw_insert(c, 'gone')
        c.execute("UPDATE items SET status='gone' WHERE item_id='gone'")
        for i in ('live', 'gone'):
            triage.system_place(c, i)
        assert [r['item_id'] for r in triage.queue(c)] == ['live']


def test_human_transition_clears_backlog(db):
    with db.connect() as c:
        raw_insert(c, 'a')
        triage.system_place(c, 'a')
        c.execute("UPDATE triage SET backlog=1 WHERE item_id='a'")
        triage.keep(c, 'a'); triage.undo(c)
        assert [r['item_id'] for r in triage.queue(c)] == ['a']


def market_store(tmp_path, monkeypatch):
    monkeypatch.setattr(market, 'DB', tmp_path / 'curio.db')
    store = Store(tmp_path / 'curio.db')
    with store.connect() as c:
        c.execute("INSERT INTO items(item_id,platform,title,description,status,agent_decision,listed_price,currency,url,first_seen,rule_version) "
                  "VALUES('one','ebay','Meissen Tasse','','active','want',100,'EUR','https://www.ebay.de/itm/1','2026-10-19',1)")
    market.init(store)
    return store


def test_market_cancel_does_not_restore_review_skip(tmp_path, monkeypatch):
    store = market_store(tmp_path, monkeypatch)
    store.review('one', 'skip', '太贵', None, 'r1', 1)
    market.set_decision('ebay', 'one', True, '无关')
    market.set_decision('ebay', 'one', False, '恢复')
    assert st(store, 'one') == 'removed'


def test_market_cancel_restores_market_remove(tmp_path, monkeypatch):
    store = market_store(tmp_path, monkeypatch)
    market.set_decision('ebay', 'one', True, '无关')
    with store.connect() as c:
        assert c.execute("SELECT source FROM triage_events WHERE action='remove'").fetchone()[0] == 'market'
    market.set_decision('ebay', 'one', False, '恢复')
    assert st(store, 'one') == 'new'
    market.set_decision('ebay', 'one', True, '无关')
    with store.connect() as c:
        triage.keep(c, 'one')
    with pytest.raises(ValueError, match='状态已变化'):
        market.set_decision('ebay', 'one', False, '恢复')


def test_market_item_missing_from_items_skips_triage(tmp_path, monkeypatch):
    store = market_store(tmp_path, monkeypatch)
    with store.connect() as c:
        c.execute('DROP VIEW market_items')
        c.execute('CREATE TABLE market_items(platform TEXT,item_id TEXT)')
        c.execute("INSERT INTO market_items VALUES('ebay','ghost')")
    market.set_decision('ebay', 'ghost', True, '无关')
    with store.connect() as c:
        assert c.execute("SELECT irrelevant FROM market_decisions WHERE item_id='ghost'").fetchone()[0] == 1
        assert triage.state(c, 'ghost') is None


def seed_case(c, iid, decision, when, rid):
    c.execute("INSERT INTO cases(item_id,decision,reason,agent_decision,rule_version,decided_at,request_id) "
              "VALUES(?,?,'x','probe',1,?,?)", (iid, decision, when, rid))


def test_migration_newer_market_cancel_beats_older_case(db):
    with db.connect() as c:
        raw_insert(c, 'a')
        seed_case(c, 'a', 'irrelevant', '2026-10-01T00:00:00+00:00', 'q1')
        c.execute("INSERT INTO market_decisions VALUES('ebay','a',0,'恢复','2026-10-02T00:00:00.500000+00:00')")
        c.execute("INSERT INTO kv VALUES('triage_migrated_at','2026-11-02T00:00:00+00:00')")
        triage.migrate(c)
        assert triage.state(c, 'a') == 'new' and not triage.has_human(c, 'a')


def test_migration_rerun_applies_newer_imported_case(db):
    with db.connect() as c:
        raw_insert(c, 'a')
        seed_case(c, 'a', 'skip', '2026-10-01T00:00:00+00:00', 'q1')
        c.execute("INSERT INTO kv VALUES('triage_migrated_at','2026-11-02T00:00:00+00:00')")
        triage.migrate(c)
        assert triage.state(c, 'a') == 'removed'
        seed_case(c, 'a', 'want', '2026-10-05T00:00:00+00:00', 'q2')
        assert triage.migrate(c) == dict(events=1, codes=1)
        assert triage.state(c, 'a') == 'kept' and triage.code(c, 'a') == 'CO-0001'
        assert triage.migrate(c) == dict(events=0, codes=0)


def test_migration_bad_first_seen_and_time(db):
    with db.connect() as c:
        raw_insert(c, 'a', first_seen=None)
        c.execute("INSERT INTO kv VALUES('triage_migrated_at','2026-11-02T00:00:00+00:00')")
        triage.migrate(c)
        assert c.execute("SELECT backlog FROM triage WHERE item_id='a'").fetchone()[0] == 0
        c.execute("UPDATE kv SET v='坏' WHERE k='triage_migrated_at'")
        with pytest.raises(triage.TriageError, match='迁移时刻无效'):
            triage.migrate(c)


def test_migrate_script_rejects_missing_db(tmp_path):
    with pytest.raises(SystemExit):
        migrate_triage.main([str(tmp_path / 'nope.db')])
    assert not (tmp_path / 'nope.db').exists()
