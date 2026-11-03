"""故事 5（CAP-3）：自动筛除读懂否定、纪念盘、外画标签、已自动移出列表与重判脚本。"""
import pytest
from curio import judge, triage, rules
from curio.config import load
from curio.rules import evaluate
from curio.store import Store
from scripts import rejudge as rejudge_script
from tests.test_std_names import make_kb


@pytest.fixture
def cfg(tmp_path):
    return dict(load(), keywords=['Meissen'], blocked_words=[], cheap_damaged_max=None,
                em_kb_path=str(make_kb(tmp_path / 'EM 西洋瓷器')))


@pytest.fixture
def db(tmp_path):
    return Store(tmp_path / 'curio.db')


def item(i='one', title='Meissen rote Rose Tasse', description='', price=40):
    return dict(item_id=i, platform='ebay', title=title, description=description, price=price, currency='EUR',
                buying='FIXED_PRICE', url='https://www.ebay.de/itm/example', image='')


def ev(title, description='', cfg=None, price=40):
    return evaluate(dict(title=title, description=description, listed_price=price, currency='EUR'), cfg)


def is_damage(r):
    return r['reason'].startswith('破损或修复')


@pytest.mark.parametrize('title,description', [
    ('Meissen Teller, nicht beschädigt, keine Risse oder Chips', ''),
    ('Meissen cup, no chips or cracks', ''),
    ('Meissen Teller', 'Ohne Beschädigung, keine Chips.'),
    ('Meissen Tasse', 'Die Tasse ist nicht beschädigt.'),
    ('Meissen Tasse', 'Haarrisse sind nicht vorhanden'),
    ('Meissen Tasse', 'without any damage, not repaired, no crack'),
    ('Meissen Terrine', 'Die Terrine istnicht beschädigt und gut erhalten'),
    ('Meissen Tasse', 'keine Chips, Risse oder Abplatzer'),
    ('Meissen Tasse', 'Beschädigungen: keine'),
    ('Meissen Tasse', 'Risse: nein'),
    ('Meissen cup', 'not chipped, never repaired'),
    ('Meissen cup', 'free of chips and cracks'),
    ('Meissen Tasse', 'Aber gut erhalten,keine Beschädigungen,nichts restauriert.'),
    ('Meissen Teller', 'Sehr guter Zustand, keine Splits, Risse, Verfärbungen.'),
    ('Meissen Tasse', 'Beschädigung am Karton'),
])
def test_negation_not_removed(cfg, title, description):
    assert not is_damage(ev(title, description, cfg))


@pytest.mark.parametrize('description,label', [
    ('Neuwertig, keine Gebrauchsspuren, Haarriss am Boden', '裂纹'),
    ('ohne Deckel, mit Chip am Fuß', '磕口'),
    ('Versand im Karton, Tasse hat Haarriss', '裂纹'),
    ('kleiner Chip am Unterrand des Henkels Privatverkauf ohne Garantie oder Rücknahme', '磕口'),
])
def test_negation_and_packaging_scoped_to_own_phrase(cfg, description, label):
    r = ev('Meissen Tasse', description, cfg)
    assert r['decision'] == 'skip' and r['reason'].startswith('破损或修复：' + label), r['reason']


def test_damage_in_condition_or_chinese(cfg):
    r = evaluate(dict(title='Meissen Tasse', description='', condition='Beschädigt', listed_price=40, currency='EUR'), cfg)
    assert r['decision'] == 'skip' and r['reason'].startswith('破损或修复：破损')
    for text, label in (('杯沿破损', '破损'), ('盘边有裂纹', '裂纹')):
        r = ev('Meissen Tasse', text, cfg)
        assert r['decision'] == 'skip' and r['reason'].startswith('破损或修复：' + label), r['reason']


def test_real_damage_removed_with_category_and_snippet(cfg):
    r = ev('Meissen Tasse, kleiner Haarriss am Rand', cfg=cfg)
    assert r['decision'] == 'skip'
    assert r['reason'] == '破损或修复：裂纹（"kleiner Haarriss am Rand"）'


@pytest.mark.parametrize('title,label', [
    ('Meissen cup chipped rim', '磕口'), ('Meissen cup with crack', '裂纹'), ('Meissen vase broken handle', '破损'),
    ('Meissen figure, repaired arm', '修复'), ('Meissen figure restored', '修复'), ('Meissen Tasse defekt', '破损'),
    ('Meissen Figur Henkel müsste geklebt werden', '需修理'),
])
def test_english_and_existing_damage_words(cfg, title, label):
    r = ev(title, cfg=cfg)
    assert r['decision'] == 'skip' and r['reason'].startswith('破损或修复：' + label + '（'), r['reason']


def test_contrast_only_negates_clause_before_aber(cfg):
    r = ev('Meissen Tasse', 'keine Chips, aber ein Haarriss', cfg)
    assert r['decision'] == 'skip' and '裂纹' in r['reason'] and 'Haarriss' in r['reason']


def test_packaging_damage_not_removed(cfg):
    assert not is_damage(ev('Meissen Tasse', 'Karton beschädigt, Tasse einwandfrei', cfg))
    assert not is_damage(ev('Meissen Tasse', 'Keine Haftung für Beschädigung beim Versand.', cfg))


def test_gold_wear_not_removed(cfg):
    assert not is_damage(ev('Meissen Tasse', 'leichter Goldabrieb', cfg))
    assert rules.damage_statements('', 'leichter Goldabrieb')[0][0] == '金彩磨损'


def test_cheap_damaged_exception_unchanged(cfg):
    r = ev('Meissen Tasse, kleiner Haarriss am Rand', cfg=dict(cfg, cheap_damaged_max=20), price=10)
    assert r['decision'] == 'probe' and r['reason'] == '破损件标价低于已配置阈值，需询市'


def test_product_v2_imports_rules_damage_statements():
    from curio import product_v2
    assert product_v2.damage_statements is rules.damage_statements


@pytest.mark.parametrize('title', ['Meissen Gedenkteller 1985', 'Meissen Gedenktasse', 'Meissen Erinnerungsteller',
                                   'Meissen commemorative plate', 'Meissen Erinnerungstasse', 'Meissen Gedenkbecher'])
def test_commemorative_plates(cfg, title):
    r = ev(title, cfg=cfg)
    assert r['decision'] == 'skip' and r['reason'].startswith('硬过滤：') and '纪念盘' in r['reason']


def test_overpaint_tag_does_not_change_judgement(cfg):
    plain = ev('Meissen Teller Blumen', cfg=cfg)
    painted = ev('Meissen Teller Hausmalerei Blumen', cfg=cfg)
    assert (painted['decision'], painted['reason']) == (plain['decision'], plain['reason'])
    n = judge.standard_name('Meissen Teller Hausmalerei Blumen', '', cfg)
    base = judge.standard_name('Meissen Teller Blumen', '', cfg)
    assert n['overpaint'] is True and base['overpaint'] is False
    assert n['std_name'] == 'Meissen梅森 外画 盘' and base['std_name'] == 'Meissen梅森 盘'
    assert n['std_key'] == base['std_key'] + '|外画' and n['std_key'].endswith('|外画')


@pytest.mark.parametrize('text', ['HAUSMALER Dekor', 'Aussenmalerei', 'Außenmalerei', 'nachträglich staffiert',
                                  'nachtraeglich bemalt', 'übermalt', 'Overpainted', 'outside decorated',
                                  'Fremdbemalung', 'fremdbemalt', 'Fremdmalerei', 'over-painted', 'over painted',
                                  'overpaint', 'later decorated', 'decorated outside the factory'])
def test_overpaint_words(text):
    assert judge.overpaint('Meissen Teller', text)


def test_overpaint_negated_and_absent():
    assert not judge.overpaint('Meissen Teller', 'nicht übermalt, keine Hausmalerei')
    assert not judge.overpaint('Meissen Teller handbemalt')
    assert not judge.overpaint('Meissen Teller', 'Übermalt: nein. Hausmalerei: keine')
    assert not judge.overpaint('Meissen Teller Hausmalereibedarf ubermalter')


def test_overpaint_compared_only_with_overpaint(db, cfg):
    db.ingest(item('a', 'Meissen Teller Hausmalerei', price=30), cfg)
    db.ingest(item('b', 'Meissen Teller', price=80), cfg)
    db.ingest(item('c', 'Meissen Teller', 'Außenmalerei', price=35), cfg)
    with db.connect() as c:
        rows = {r['item_id']: dict(r) for r in c.execute('SELECT * FROM std_names')}
    assert rows['a']['overpaint'] == 1 and rows['b']['overpaint'] == 0 and rows['c']['overpaint'] == 1
    assert [r['item_id'] for r in db.similar_prices(rows['a']['std_key'])] == ['a', 'c']
    assert [r['item_id'] for r in db.similar_prices(rows['b']['std_key'])] == ['b']


def test_auto_removed_list_and_rescue(db, cfg):
    for i in range(3):
        db.ingest(item('bad%d' % i, 'Meissen Tasse, kleiner Haarriss am Rand'), cfg)
    db.ingest(item('saved', 'Meissen Tasse, kleiner Chip'), cfg)
    db.ingest(item('fine'), cfg)
    with db.connect() as c:
        triage.rescue(c, 'saved')
    with db.connect() as c:
        rows = triage.auto_removed(c)
        assert sorted(r['item_id'] for r in rows) == ['bad0', 'bad1', 'bad2']
        assert all(r['reason'] == '破损或修复：裂纹（"kleiner Haarriss am Rand"）' for r in rows)
        assert all(r['removed_at'] and r['title'] and r['platform'] == 'ebay' and r['price'] == 40 for r in rows)
        assert [r['removed_at'] for r in rows] == sorted((r['removed_at'] for r in rows), reverse=True)
        assert triage.queue(c)[0]['item_id'] == 'saved'
        ev_ = c.execute("SELECT actor,action FROM triage_events WHERE item_id='saved' ORDER BY id DESC LIMIT 1").fetchone()
        assert tuple(ev_) == ('human', 'rescue')
        # 系统落位事件带理由快照
        snap = c.execute("SELECT reason FROM triage_events WHERE item_id='bad0' AND actor='system'").fetchone()[0]
        assert snap.startswith('破损或修复：裂纹')
        assert len(triage.auto_removed(c, limit=2)) == 2 and len(triage.auto_removed(c, limit=2, offset=2)) == 1


def test_list_shows_current_reason_and_event_keeps_snapshot(db, cfg):
    db.ingest(item('x', 'Meissen Tasse, kleiner Haarriss am Rand'), cfg)
    with db.connect() as c:
        c.execute("UPDATE items SET agent_reason='被覆盖' WHERE item_id='x'")
        assert triage.auto_removed(c)[0]['reason'] == '被覆盖'
        snap = c.execute("SELECT reason FROM triage_events WHERE item_id='x' AND actor='system'").fetchone()[0]
        assert snap.startswith('破损或修复：裂纹')


def test_list_reason_follows_rejudge_even_with_human_event(db, cfg):
    db.ingest(item('x', 'Meissen Tasse, kleiner Haarriss am Rand'), cfg)
    with db.connect() as c:
        triage.rescue(c, 'x')
        triage.undo(c, 'x')
        assert triage.state(c, 'x') == 'auto_removed'
        c.execute("INSERT INTO blocked_words VALUES('Haarriss','2026-11-03')")
    db.rejudge_all(cfg)
    with db.connect() as c:
        rows = triage.auto_removed(c)
        assert [r['item_id'] for r in rows] == ['x'] and rows[0]['reason'] == '屏蔽词：Haarriss（标题）'


@pytest.mark.parametrize('limit,offset', [(0, 0), (-1, 0), (10, -1)])
def test_auto_removed_rejects_bad_paging(db, limit, offset):
    with db.connect() as c:
        with pytest.raises(ValueError):
            triage.auto_removed(c, limit, offset)


def _set_old(db, iid, decision, reason):
    with db.connect() as c:
        c.execute('UPDATE items SET agent_decision=?,agent_reason=? WHERE item_id=?', (decision, reason, iid))
        c.execute('UPDATE triage SET state=? WHERE item_id=?', (triage.system_state(decision), iid))


def test_rejudge_all_protects_human_and_moves_untouched(db, cfg):
    db.ingest(item('human', 'Meissen Teller, nicht beschädigt'), cfg)
    db.ingest(item('auto', 'Meissen Teller, nicht beschädigt'), cfg)
    db.ingest(item('kept', 'Meissen Teller, nicht beschädigt'), cfg)
    # 模拟旧规则：两件曾被误判为破损。
    _set_old(db, 'human', 'skip', '破损或修复线索，默认不要')
    _set_old(db, 'auto', 'skip', '破损或修复线索，默认不要')
    with db.connect() as c:
        triage.remove(c, 'human', '人工去掉')
        triage.keep(c, 'kept')
        code = triage.code(c, 'kept')
        cases = c.execute('SELECT count(*) FROM cases').fetchone()[0]
    r = db.rejudge_all(cfg)
    assert r['changed'] == 2 and r['moved'] == 1
    assert sum(r['transitions'].values()) == 2
    assert all(k.startswith('破损或修复线索，默认不要（skip）→') and '（probe）' in k for k in r['transitions'])
    with db.connect() as c:
        assert triage.state(c, 'human') == 'removed'
        assert triage.state(c, 'auto') == 'new'
        assert triage.state(c, 'kept') == 'kept' and triage.code(c, 'kept') == code
        assert c.execute("SELECT agent_decision FROM items WHERE item_id='human'").fetchone()[0] == 'probe'
        assert c.execute('SELECT count(*) FROM cases').fetchone()[0] == cases
    again = db.rejudge_all(cfg)
    assert again['changed'] == 0 and again['std_written'] == 0


def test_rejudge_refreshes_std_names_and_reason_on_auto_removed(db, cfg):
    db.ingest(item('p', 'Meissen Teller Hausmalerei'), cfg)
    db.ingest(item('d', 'Meissen Tasse, kleiner Haarriss am Rand'), cfg)
    with db.connect() as c:
        c.execute("UPDATE std_names SET std_key='Meissen|-|-|FN-009',overpaint=0 WHERE item_id='p'")
        c.execute("UPDATE items SET agent_reason='破损或修复线索，默认不要' WHERE item_id='d'")
    r = db.rejudge_all(cfg)
    assert r['std_written'] == 1 and r['changed'] == 1
    with db.connect() as c:
        assert c.execute("SELECT std_key,overpaint FROM std_names WHERE item_id='p'").fetchone()[:] == ('Meissen|-|-|FN-009|外画', 1)
        assert triage.auto_removed(c)[0]['reason'].startswith('破损或修复：裂纹')


def test_rejudge_all_survives_naming_error(db, cfg, monkeypatch):
    from curio import store as store_mod
    db.ingest(item('a', 'Meissen Teller'), cfg)
    db.ingest(item('b', 'Meissen Tasse, kleiner Haarriss am Rand'), cfg)
    with db.connect() as c:
        c.execute("UPDATE items SET agent_reason='旧' WHERE item_id='b'")
    real = store_mod.standard_name
    monkeypatch.setattr(store_mod, 'standard_name', lambda it, cf: (_ for _ in ()).throw(RuntimeError('x')) if it['item_id'] == 'a' else real(it, cf))
    r = db.rejudge_all(cfg)
    assert r['changed'] == 1 and r['std_failed'] == 1


def test_rejudge_all_skips_std_refresh_when_kb_degraded(db, cfg, tmp_path):
    db.ingest(item('a', 'Meissen Teller Hausmalerei'), cfg)
    with db.connect() as c:
        before = c.execute("SELECT * FROM std_names WHERE item_id='a'").fetchone()[:]
    r = db.rejudge_all(dict(cfg, em_kb_path=str(tmp_path / 'missing')))
    assert r['std_skipped'] and r['std_written'] == 0
    with db.connect() as c:
        assert c.execute("SELECT * FROM std_names WHERE item_id='a'").fetchone()[:] == before


def test_rejudge_all_recomputes_items_changed_during_precompute(db, cfg, monkeypatch):
    from curio import store as store_mod
    db.ingest(item('a', 'Meissen Teller'), cfg)
    real = store_mod.rejudge

    def racing(c, config, precomputed=None):
        # 预算之后、写事务里重判之前，标题被采集改成破损
        c.execute("UPDATE items SET title='Meissen Teller defekt',rule_version=rule_version+1 WHERE item_id='a'")
        return real(c, config, precomputed)
    monkeypatch.setattr(store_mod, 'rejudge', racing)
    r = db.rejudge_all(cfg)
    assert r['changed'] == 1
    with db.connect() as c:
        assert c.execute("SELECT agent_decision FROM items WHERE item_id='a'").fetchone()[0] == 'skip'


def test_overpaint_column_added_once_concurrently(tmp_path):
    import sqlite3
    path = tmp_path / 'old.db'
    Store(path)
    c = sqlite3.connect(path)
    c.execute('ALTER TABLE std_names DROP COLUMN overpaint'); c.commit(); c.close()
    Store(path)  # 补列
    Store(path)  # 已有列：不报错
    c = sqlite3.connect(path)
    assert 'overpaint' in {r[1] for r in c.execute('PRAGMA table_info(std_names)')}


def test_rejudge_script(db, cfg, monkeypatch, capsys):
    db.ingest(item('a', 'Meissen Teller, keine Risse'), cfg)
    monkeypatch.setattr(rejudge_script, 'load', lambda: cfg)
    first = rejudge_script.main([str(db.path)])
    second = rejudge_script.main([str(db.path)])
    assert second['changed'] == 0 and first['checked'] == 1
    assert '"changed": 0' in capsys.readouterr().out
    with pytest.raises(SystemExit):
        rejudge_script.main([str(db.path) + '.missing'])
