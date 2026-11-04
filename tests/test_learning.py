"""故事 17（CAP-8，FR-18、FR-19）：判断沉淀——从人工拣选归纳待审规律、确认 / 否决、规则归口执行。
用 tmp_path 下的小型假知识库与临时 curio.db，不连接外部主机、不写真实 data/curio.db。"""
import json
import re
import threading
import urllib.error
import urllib.request

import pytest

from curio import learning, triage
from curio.config import ROOT as CFG_ROOT, learning as learning_config, load
from curio.store import Store
from tests.test_std_names import make_kb

ROOT_CFG = learning_config()


@pytest.fixture
def cfg(tmp_path):
    return dict(load(), em_kb_path=str(make_kb(tmp_path / 'EM 西洋瓷器')))


@pytest.fixture
def db(tmp_path):
    return Store(tmp_path / 'data' / 'curio.db')


PLATE = 'Meissen Blaue Blume Teller'        # 梅森 · 蓝花 · 盘
CUP = 'Meissen Zwiebelmuster Kaffeetasse m. U.'   # 梅森 · 蓝洋葱 · 咖啡杯碟
_n = [0]


def add(db, cfg, title, price=40, iid=None):
    _n[0] += 1
    iid = iid or 'i%d' % _n[0]
    db.ingest(dict(item_id=iid, platform='ebay', title=title, price=price, currency='EUR', buying='FIXED_PRICE',
                   url='https://www.ebay.de/itm/x', image=''), cfg)
    return iid


def act(db, action, iid=None):
    _n[0] += 1
    return db.triage_act(action, iid, 'rq%d' % _n[0])


def many(db, cfg, title, action, n, price=40):
    ids = [add(db, cfg, title, price) for _ in range(n)]
    for i in ids:
        act(db, action, i)
    return ids


def refresh(db):
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        return learning.refresh(c, ROOT_CFG)


def rows(db, status=None):
    with db.connect() as c:
        sql = 'SELECT * FROM learned_rules' + (' WHERE status=?' if status else '') + ' ORDER BY id'
        return [dict(r, dims=json.loads(r['combo_key'])) for r in c.execute(sql, (status,) if status else ())]


def pending(db):
    with db.connect() as c:
        return learning.pending(c)


def decide(db, rule_id, decision):
    _n[0] += 1
    return learning.decide_request(db, rule_id, decision, 'rd%d' % _n[0], CFG_ROOT)


def state(db, iid):
    with db.connect() as c:
        return triage.state(c, iid)


def item(db, iid):
    with db.connect() as c:
        return dict(c.execute('SELECT * FROM items WHERE item_id=?', (iid,)).fetchone())


def score(db, iid):
    with db.connect() as c:
        return dict(c.execute('SELECT * FROM scores WHERE item_id=?', (iid,)).fetchone())


# ---------------------------------------------------------------- 归纳与门槛

def test_threshold_proposes_one_specific_rule(db, cfg):
    many(db, cfg, PLATE, 'remove', 6)
    r = refresh(db)
    assert r == dict(pending=1, proposed=1, withdrawn=0)
    [p] = pending(db)
    assert p['direction'] == 'remove' and p['count'] == 6 and p['total'] == 6 and p['last_at']
    assert p['sentence'] == 'Meissen梅森 蓝花的盘：6 件里你去掉了 6 件——以后自动去掉？'
    [row] = rows(db)
    assert row['dims'] == {'brand': 'Meissen', 'pattern_id': 'MP-050', 'function_id': 'FN-009'}
    # 再刷新不重复提出，计数照旧。
    assert refresh(db) == dict(pending=1, proposed=0, withdrawn=0)


def test_ratio_not_enough(db, cfg):
    many(db, cfg, PLATE, 'remove', 5)
    many(db, cfg, PLATE, 'keep', 3)
    assert refresh(db)['pending'] == 0 and rows(db) == []


def test_undone_actions_do_not_count(db, cfg):
    many(db, cfg, PLATE, 'remove', 5)
    act(db, 'undo')
    assert refresh(db)['pending'] == 0


def test_pending_withdrawn_when_undo_drops_below_threshold(db, cfg):
    many(db, cfg, PLATE, 'remove', 5)
    assert refresh(db)['pending'] == 1
    act(db, 'undo')
    assert refresh(db) == dict(pending=0, proposed=0, withdrawn=1)


def test_one_item_counted_once_by_last_human_direction(db, cfg):
    ids = many(db, cfg, PLATE, 'keep', 5)
    for i in ids[:2]:
        act(db, 'remove', i)          # 留下后又去掉：只按最后一次（去掉）计
    assert refresh(db)['pending'] == 0       # 留下 3、去掉 2


def test_only_human_triage_actions_count(db, cfg):
    ids = [add(db, cfg, PLATE) for _ in range(6)]
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        for i in ids:
            triage.note_probe(c, i, 'probe')   # 询市不是留下 / 去掉 / 捞回
    assert refresh(db)['pending'] == 0


def test_rescue_counts_as_keep(db, cfg):
    cfg = dict(cfg, blocked_words=[])
    ids = [add(db, cfg, CUP) for _ in range(5)]
    with db.connect() as c:          # 先把它们放进"自动移出"，再由墨墨捞回
        c.execute('BEGIN IMMEDIATE')
        c.execute("UPDATE triage SET state='auto_removed'")
    for i in ids:
        act(db, 'rescue', i)
    refresh(db)
    [p] = pending(db)
    assert p['direction'] == 'keep' and p['count'] == 5
    assert '5 件里你留下了 5 件' in p['sentence']


def test_price_band_refines_mixed_combo(db, cfg):
    many(db, cfg, 'Meissen Teller Rose', 'remove', 5, price=30)
    many(db, cfg, 'Meissen Teller Rose', 'keep', 5, price=200)
    refresh(db)
    got = {(r['direction'], r['label']) for r in rows(db)}
    assert got == {('remove', 'Meissen梅森的盘（€50 以下）'), ('keep', 'Meissen梅森的盘（€150–400）')}


def test_more_general_rule_blocks_specific(db, cfg):
    many(db, cfg, PLATE, 'remove', 6)
    dims = {'brand': 'Meissen', 'function_id': 'FN-009'}
    with db.connect() as c:
        learning.ensure(c)
        c.execute('''INSERT INTO learned_rules(combo_key,direction,label,status,count,total,created_at)
            VALUES(?,?,?,?,?,?,?)''', (learning.combo_key(dims), 'remove', 'Meissen梅森的盘',
                                       'confirmed', 6, 6, '2026-11-01T00:00:00+00:00'))
    assert refresh(db)['pending'] == 0


def test_rejected_not_proposed_again(db, cfg):
    many(db, cfg, PLATE, 'remove', 6)
    refresh(db)
    [p] = pending(db)
    assert decide(db, p['id'], 'reject') == dict(id=p['id'], status='rejected', rescored=0, moved=0, count=0)
    many(db, cfg, PLATE, 'remove', 3)
    assert refresh(db)['pending'] == 0
    assert [r['status'] for r in rows(db)] == ['rejected']


def test_comments_are_not_learned(db, cfg):
    ids = [add(db, cfg, PLATE) for _ in range(6)]
    for i in ids:
        db.save_comment(i, '不要这种')
    assert refresh(db)['pending'] == 0


# ---------------------------------------------------------------- 生效（规则归口）

def test_confirmed_removal_auto_removes_new_items_and_can_be_rescued(db, cfg):
    old = many(db, cfg, PLATE, 'remove', 6)
    kept = add(db, cfg, PLATE)
    act(db, 'keep', kept)                  # 墨墨在规律前留下的同组合货
    untouched = add(db, cfg, PLATE)        # 还没人工决定的同组合货
    refresh(db)
    [p] = pending(db)
    r = decide(db, p['id'], 'confirm')
    assert r['status'] == 'confirmed' and r['count'] == 0
    fresh = add(db, cfg, PLATE + ' 26 cm')
    assert state(db, fresh) == 'auto_removed'
    assert item(db, fresh)['agent_reason'] == '已确认规律：Meissen梅森 蓝花的盘自动去掉'
    other = add(db, cfg, CUP)              # 别的组合不受影响
    assert state(db, other) == 'new'
    # 重判：未经人工决定的同组合货移出；人工决定的货不变、理由不被规律改写。
    before_kept = item(db, kept)
    db.rejudge_all(cfg)
    assert state(db, untouched) == 'auto_removed'
    assert state(db, kept) == 'kept' and item(db, kept)['agent_reason'] == before_kept['agent_reason']
    assert all(state(db, i) == 'removed' for i in old)
    # 仍可捞回；捞回后重判不再移出。
    assert act(db, 'rescue', fresh)['state'] == 'new'
    db.rejudge_all(cfg)
    assert state(db, fresh) == 'new'


def test_confirmed_keep_rule_adds_baseline_bonus(db, cfg):
    many(db, cfg, CUP, 'keep', 5)
    before = add(db, cfg, CUP + ' 1. Wahl')
    base = score(db, before)['score']
    rescued = add(db, cfg, CUP)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute("UPDATE triage SET state='auto_removed' WHERE item_id=?", (rescued,))
    act(db, 'rescue', rescued)             # 有人工决定：不受规律影响
    rescued_score = score(db, rescued)['score']
    refresh(db)
    [p] = pending(db)
    assert p['direction'] == 'keep'
    decide(db, p['id'], 'confirm')
    bonus = ROOT_CFG['keep_bonus']
    assert score(db, before)['score'] == min(100, base + bonus)
    assert '已确认规律：Meissen梅森 蓝洋葱的咖啡杯碟' in score(db, before)['reason']
    fresh = add(db, cfg, CUP + ' 1. Wahl')
    assert score(db, fresh)['score'] == min(100, base + bonus) and state(db, fresh) == 'new'
    assert score(db, rescued)['score'] == rescued_score
    plate = add(db, cfg, PLATE)
    assert '已确认规律' not in score(db, plate)['reason']


def test_decide_twice_refused(db, cfg):
    many(db, cfg, PLATE, 'remove', 6)
    refresh(db)
    [p] = pending(db)
    decide(db, p['id'], 'confirm')
    with pytest.raises(ValueError, match='已处理'):
        decide(db, p['id'], 'reject')
    with pytest.raises(ValueError):
        decide(db, 999, 'confirm')
    with pytest.raises(ValueError):
        decide(db, p['id'], 'maybe')


def test_monitor_round_refreshes(db, cfg, tmp_path):
    many(db, cfg, PLATE, 'remove', 6)
    assert learning.run(db, tmp_path) == {'learning_status': 'ok', 'rules_pending': 1, 'rules_proposed': 1}


def test_learning_config_validation(tmp_path):
    (tmp_path / 'config').mkdir()
    f = tmp_path / 'config' / 'learning.yaml'
    f.write_text('min_count: 0\n')
    with pytest.raises(ValueError, match='min_count'):
        learning_config(tmp_path)
    f.write_text('min_ratio: 1.5\n')
    with pytest.raises(ValueError, match='min_ratio'):
        learning_config(tmp_path)
    f.write_text('price_bands: {edges: [150, 50]}\n')
    with pytest.raises(ValueError, match='从小到大'):
        learning_config(tmp_path)
    assert ROOT_CFG['min_count'] == 5 and ROOT_CFG['min_ratio'] == 0.8


# ---------------------------------------------------------------- 后台接口

@pytest.fixture
def http(db, tmp_path):
    from curio.review import server
    httpd = server(db, port=0, root=tmp_path, flush=lambda: None)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = 'http://127.0.0.1:' + str(httpd.server_port)
    html = urllib.request.urlopen(base + '/').read().decode()
    token = re.search("const token='([^']+)'", html)[1]

    def get(path):
        return json.load(urllib.request.urlopen(base + path))

    def post(path, body):
        req = urllib.request.Request(base + path, json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json', 'X-Curio-Token': token})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    yield dict(base=base, html=html, get=get, post=post)
    httpd.shutdown()
    httpd.server_close()


def test_page_has_rules_section(http):
    html = http['html']
    assert '<a class="tc" id="tab05" href="/#rules">' in html and 'id="cRules"' in html
    assert 'id="rulesView"' in html and '暂无待审核的规律' in html and '暂不可用' not in html
    js = urllib.request.urlopen(http['base'] + '/triage.js').read().decode()
    assert 'window.curioRules' in js and '已确认规律' in js and '已否决规律' in js and '/api/rules/decide' in js


def test_api_empty_then_refresh_on_open(http, db, cfg):
    d = http['get']('/api/rules')
    assert (d['count'], d['items'], d['active']) == (0, [], []) and d['config'] == {'min_count': 5, 'min_ratio': 0.8}
    many(db, cfg, PLATE, 'remove', 6)
    assert http['get']('/api/rules')['count'] == 0          # 只读，不归纳
    d = http['get']('/api/rules?refresh=1')
    assert d['count'] == 1 and d['items'][0]['sentence'].endswith('以后自动去掉？')


def test_api_decide_idempotent_and_errors(http, db, cfg):
    many(db, cfg, PLATE, 'remove', 6)
    rule = http['get']('/api/rules?refresh=1')['items'][0]
    body = dict(rule_id=rule['id'], decision='confirm', request_id='r-1')
    status, first = http['post']('/api/rules/decide', body)
    assert status == 200 and first['status'] == 'confirmed' and first['count'] == 0
    assert http['post']('/api/rules/decide', body) == (200, first)                     # 同一 request_id 重放
    status, err = http['post']('/api/rules/decide', dict(body, decision='reject'))
    assert status == 400 and '重复请求内容不同' in err['error']
    status, err = http['post']('/api/rules/decide', dict(body, decision='reject', request_id='r-2'))
    assert status == 400 and '已处理' in err['error']
    d = http['get']('/api/rules')
    assert d['count'] == 0 and d['items'] == [] and [a['id'] for a in d['active']] == [rule['id']]


def test_api_write_failure_then_retry_same_request(http, db, cfg, monkeypatch):
    many(db, cfg, PLATE, 'remove', 6)
    rule = http['get']('/api/rules?refresh=1')['items'][0]
    real = learning.decide
    calls = []

    def flaky(c, rule_id, decision, learn=None, at=None):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError('磁盘出错')
        return real(c, rule_id, decision, learn, at)
    monkeypatch.setattr(learning, 'decide', flaky)
    body = dict(rule_id=rule['id'], decision='reject', request_id='r-x')
    status, err = http['post']('/api/rules/decide', body)
    assert status == 500 and err['error'] == '未保存，请重试'
    assert http['get']('/api/rules')['count'] == 1           # 失败时没有写入
    status, ok = http['post']('/api/rules/decide', body)
    assert status == 200 and ok['status'] == 'rejected'


def test_api_requires_token(http):
    req = urllib.request.Request(http['base'] + '/api/rules/decide', json.dumps({}).encode(),
                                 headers={'Content-Type': 'application/json'})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 403


# ---------------------------------------------------------------- 复核补充

def test_confirm_remove_moves_matching_undecided_new_items_now(db, cfg):
    many(db, cfg, PLATE, 'remove', 6)
    waiting = add(db, cfg, PLATE)
    other = add(db, cfg, CUP)
    refresh(db)
    [p] = pending(db)
    r = decide(db, p['id'], 'confirm')
    assert r['moved'] == 1 and state(db, waiting) == 'auto_removed' and state(db, other) == 'new'
    assert item(db, waiting)['agent_reason'] == '已确认规律：Meissen梅森 蓝花的盘自动去掉'


def test_withdrawn_rule_cannot_be_confirmed(db, cfg):
    many(db, cfg, PLATE, 'remove', 5)
    refresh(db)
    [p] = pending(db)
    act(db, 'undo')
    refresh(db)
    with pytest.raises(ValueError, match='已不在待审核里'):
        decide(db, p['id'], 'confirm')


def test_pending_specific_withdrawn_when_general_confirmed(db, cfg):
    many(db, cfg, PLATE, 'remove', 6)
    refresh(db)
    dims = {'brand': 'Meissen', 'function_id': 'FN-009'}
    with db.connect() as c:
        c.execute('''INSERT INTO learned_rules(combo_key,direction,label,status,count,total,created_at)
            VALUES(?,?,?,?,?,?,?)''', (learning.combo_key(dims), 'remove', 'Meissen梅森的盘', 'confirmed', 6, 6, 'x'))
    r = refresh(db)
    assert r['pending'] == 0 and r['withdrawn'] == 1


def test_retire_stops_applying_and_never_reproposed(db, cfg):
    many(db, cfg, PLATE, 'remove', 6)
    refresh(db)
    [p] = pending(db)
    decide(db, p['id'], 'confirm')
    assert state(db, add(db, cfg, PLATE)) == 'auto_removed'
    with pytest.raises(ValueError, match='不在已生效'):
        decide(db, p['id'] + 99, 'retire')
    assert decide(db, p['id'], 'retire')['status'] == 'retired'
    assert state(db, add(db, cfg, PLATE + ' groß')) == 'new'
    many(db, cfg, PLATE, 'remove', 3)
    assert refresh(db)['pending'] == 0 and [r['status'] for r in rows(db)] == ['retired']
    with db.connect() as c:
        assert learning.active(c) == [] and learning.confirmed(c) == []


def test_opposite_direction_overlap_refused(db, cfg):
    many(db, cfg, PLATE, 'remove', 6)
    refresh(db)
    [p] = pending(db)
    dims = {'brand': 'Meissen'}
    with db.connect() as c:
        c.execute('''INSERT INTO learned_rules(combo_key,direction,label,status,count,total,created_at)
            VALUES(?,?,?,?,?,?,?)''', (learning.combo_key(dims), 'keep', 'Meissen梅森的货', 'confirmed', 9, 9, 'x'))
    with pytest.raises(ValueError, match='方向相反'):
        decide(db, p['id'], 'confirm')


def test_keep_bonus_skipped_when_removal_also_applies():
    from curio import rules
    std = dict(brand='Meissen', function_id='FN-009')
    item_ = dict(listed_price=40, currency='EUR')
    keep = dict(direction='keep', dims={'brand': 'Meissen'}, label='Meissen梅森的货')
    rem = dict(direction='remove', dims={'brand': 'Meissen', 'function_id': 'FN-009'}, label='Meissen梅森的盘')
    assert rules.learned_keep(item_, std, dict(rules=[keep], bonus=10))[1] == 10
    assert rules.learned_keep(item_, std, dict(rules=[keep, rem], bonus=10)) is None


def test_price_band_rule_bounds(db, cfg):
    dims = {'brand': 'Meissen', 'function_id': 'FN-009', 'price': {'currency': 'EUR', 'lo': None, 'hi': 50.0}}
    with db.connect() as c:
        learning.ensure(c)
        c.execute('''INSERT INTO learned_rules(combo_key,direction,label,status,count,total,created_at)
            VALUES(?,?,?,?,?,?,?)''', (learning.combo_key(dims), 'remove', 'Meissen梅森的盘（€50 以下）', 'confirmed', 5, 5, 'x'))
    assert state(db, add(db, cfg, 'Meissen Teller Rose', 30)) == 'auto_removed'
    assert state(db, add(db, cfg, 'Meissen Teller Rose', 50)) == 'new'
    assert state(db, add(db, cfg, 'Meissen Teller Rose', 200)) == 'new'


def test_non_eur_items_have_no_price_band():
    from curio import rules
    bands = ROOT_CFG['price_bands']
    assert rules.price_band(dict(listed_price=30, currency='USD'), bands) is None
    assert rules.price_band(dict(listed_price=30, currency='EUR'), bands) == dict(currency='EUR', lo=None, hi=50.0)
    assert 'price' not in rules.rule_features(dict(brand='Meissen'), dict(listed_price=30, currency='USD'), bands)


def test_overpaint_dimension(db, cfg):
    many(db, cfg, 'Meissen Teller Hausmalerei', 'remove', 5)
    many(db, cfg, 'Meissen Teller Rose', 'keep', 5)
    refresh(db)
    [p] = pending(db)
    assert rows(db)[0]['dims'] == {'brand': 'Meissen', 'function_id': 'FN-009', 'overpaint': True}
    assert p['sentence'].startswith('外画 Meissen梅森的盘：5 件里你去掉了 5 件')


def test_undone_only_item_is_undecided(db, cfg):
    many(db, cfg, PLATE, 'remove', 6)
    refresh(db)
    [p] = pending(db)
    x = add(db, cfg, PLATE + ' klein')
    act(db, 'keep', x)
    act(db, 'undo')                       # 唯一的人工决定被撤销：视为未经人工决定
    with db.connect() as c:
        assert not triage.has_human(c, x)
    decide(db, p['id'], 'confirm')
    assert state(db, x) == 'auto_removed'


def test_kept_item_reingested_with_new_price_unchanged(db, cfg):
    many(db, cfg, PLATE, 'remove', 6)
    kept = add(db, cfg, PLATE, iid='kept1')
    act(db, 'keep', kept)
    refresh(db)
    [p] = pending(db)
    decide(db, p['id'], 'confirm')
    before = item(db, kept)
    add(db, cfg, PLATE, price=35, iid='kept1')
    after = item(db, kept)
    assert (after['agent_decision'], after['agent_reason']) == (before['agent_decision'], before['agent_reason'])
    assert state(db, kept) == 'kept' and '已确认规律' not in after['agent_reason']


def test_block_word_rejudge_keeps_rule_removal(db, cfg):
    many(db, cfg, PLATE, 'remove', 6)
    refresh(db)
    [p] = pending(db)
    decide(db, p['id'], 'confirm')
    gone = add(db, cfg, PLATE + ' Set')
    target = add(db, cfg, 'Meissen Rose Kaffeetasse Werbung')
    db.block(target, 'Werbung', 'blk1', item(db, target)['rule_version'], cfg)
    assert state(db, gone) == 'auto_removed'
    assert item(db, gone)['agent_reason'] == '已确认规律：Meissen梅森 蓝花的盘自动去掉'


def test_fresh_ai_score_untouched_by_keep_bonus(db, cfg):
    from curio.store import write_score
    many(db, cfg, CUP, 'keep', 5)
    x = add(db, cfg, CUP)
    with db.connect() as c:
        write_score(c, x, 77, 'ai', 'AI 理由', item(db, x)['rule_version'])
    refresh(db)
    [p] = pending(db)
    decide(db, p['id'], 'confirm')
    assert (score(db, x)['score'], score(db, x)['source']) == (77, 'ai')


def test_ai_payload_baseline_includes_keep_bonus(db, cfg):
    from curio import ai
    from curio.store import baseline_learned
    many(db, cfg, CUP, 'keep', 5)
    x = add(db, cfg, CUP)
    refresh(db)
    [p] = pending(db)
    decide(db, p['id'], 'confirm')
    with db.connect() as c:
        row = c.execute('SELECT * FROM items WHERE item_id=?', (x,)).fetchone()
        std = c.execute('SELECT * FROM std_names WHERE item_id=?', (x,)).fetchone()
        learned = baseline_learned(c, x)
    assert ai.item_payload(row, std, learned)['baseline_score'] == score(db, x)['score']
    assert ai.item_payload(row, std)['baseline_score'] == score(db, x)['score'] - ROOT_CFG['keep_bonus']


def test_bad_learning_yaml_does_not_break_scoring(db, cfg, monkeypatch):
    import curio.store as store_mod
    many(db, cfg, CUP, 'keep', 5)
    refresh(db)
    [p] = pending(db)
    decide(db, p['id'], 'confirm')

    def boom(root=None):
        raise ValueError('learning.min_count 必须为 1–1000 的整数')
    monkeypatch.setattr(store_mod, 'learning_for', boom)
    x = add(db, cfg, CUP)                  # 入库照常，只是不计规律加分
    assert '已确认规律' not in score(db, x)['reason']


def test_run_reports_reason_on_bad_yaml(db, tmp_path):
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config' / 'learning.yaml').write_text('price_bands: []\n')
    r = learning.run(db, tmp_path)
    assert r['learning_status'] == 'error' and 'price_bands 必须为映射' in r['learning_error']


def test_run_skips_when_nothing_changed(db, cfg, tmp_path):
    many(db, cfg, PLATE, 'remove', 6)
    assert learning.run(db, tmp_path)['rules_proposed'] == 1
    with db.connect() as c:
        mark = c.execute("SELECT v FROM kv WHERE k='learning_sig'").fetchone()[0]
    assert learning.refresh_store(db, ROOT_CFG)['skipped'] is True
    act(db, 'remove', add(db, cfg, PLATE))
    assert learning.refresh_store(db, ROOT_CFG)['skipped'] is False
    with db.connect() as c:
        assert c.execute("SELECT v FROM kv WHERE k='learning_sig'").fetchone()[0] != mark


def test_api_refresh_error_keeps_old_items(http, db, cfg, tmp_path):
    many(db, cfg, PLATE, 'remove', 6)
    assert http['get']('/api/rules?refresh=1')['count'] == 1
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config' / 'learning.yaml').write_text('min_ratio: 2\n')
    d = http['get']('/api/rules?refresh=1')
    assert 'min_ratio' in d['refresh_error'] and d['count'] == 1 and len(d['items']) == 1 and 'config' not in d


def test_api_retire(http, db, cfg):
    many(db, cfg, PLATE, 'remove', 6)
    rule = http['get']('/api/rules?refresh=1')['items'][0]
    assert http['post']('/api/rules/decide', dict(rule_id=rule['id'], decision='confirm', request_id='c1'))[0] == 200
    status, out = http['post']('/api/rules/decide', dict(rule_id=rule['id'], decision='retire', request_id='c2'))
    assert status == 200 and out['status'] == 'retired' and http['get']('/api/rules')['active'] == []


def test_monitor_rounds_carry_learning_status(tmp_path):
    from unittest.mock import patch
    from tests.test_schedule import Clock, at, run_with
    import shutil
    from curio.config import ROOT
    root = tmp_path / 'r'
    shutil.copytree(ROOT / 'config', root / 'config')
    (root / '.env').write_text('EBAY_CLIENT_ID=dummy\nEBAY_CLIENT_SECRET=dummy\n')
    order = []
    import curio.learning as L, curio.sync as S
    real_l, real_s = L.run, S.run

    def lrun(*a, **k):
        order.append('learning'); return real_l(*a, **k)

    def srun(*a, **k):
        order.append('sync'); return real_s(*a, **k)
    clock = Clock(at(2, 12))
    with patch.object(L, 'run', lrun), patch.object(S, 'run', srun):
        active_, *_ = run_with(root, clock)
        clock.ts += 2 * 60          # 高峰时段间隔 5 分钟：2 分钟后仍是空闲轮
        idle, *_ = run_with(root, clock)
    assert active_['learning_status'] == 'ok' and active_['rules_pending'] == 0
    assert idle['status'] == 'idle' and idle['learning_status'] == 'ok' and 'rules_pending' in idle
    assert order == ['learning', 'sync', 'learning', 'sync']
