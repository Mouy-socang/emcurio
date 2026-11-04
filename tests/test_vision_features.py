"""故事 21：看图识别功能、器型、图案——与打分同一请求，从知识库词表选编号，存 visual_features，01 / 02 / 详情显示一行
（全部 mock，临时库与临时知识库，不联网）。"""
import json
import logging
import shutil
from datetime import datetime
from unittest.mock import Mock, patch

import pytest

from curio import ai, ranking, store as store_mod, triage
from curio.config import ROOT, load
from curio.schedule import BERLIN
from curio.store import Store
from tests.test_std_names import make_kb

NOON = datetime(2026, 11, 4, 12, tzinfo=BERLIN).timestamp()
KEY = 'sk-ant-KEY-SENTINEL-21'
EBAY = 'https://i.ebayimg.com/images/g/%s/s-l1600.jpg'
DAY = 86400
REAL_KB_PATH = store_mod._kb_path        # autouse 替换之前的原函数


@pytest.fixture(autouse=True)
def no_network():
    with patch('requests.sessions.Session.request', side_effect=AssertionError('测试不得联网')):
        yield


@pytest.fixture(autouse=True)
def no_sleep():
    with patch('curio.ai._sleep'):
        yield


@pytest.fixture
def kb(tmp_path):
    root = make_kb(tmp_path / 'EM 西洋瓷器')
    # 一条没有任何外文 / 别名词的功能：词表仍要按 CSV 编号收进来
    f = root / '05 General Knowledge 通用知识/买手培训/功能词表.csv'
    f.write_text(f.read_text(encoding='utf-8') + 'FN-099,无别名件,,,,,,,,,已确认\n', encoding='utf-8')
    return str(root)


@pytest.fixture(autouse=True)
def display_kb(monkeypatch, kb):
    """卡片显示读的知识库路径指向临时知识库（不读本机 settings.yaml 与真实知识库）。"""
    monkeypatch.setattr(store_mod, '_kb_path', lambda root=None: kb)


@pytest.fixture
def cfg(kb):
    return dict(load(), keywords=['Meissen'], em_kb_path=kb)


@pytest.fixture
def db(tmp_path):
    return Store(tmp_path / 'curio.db')


def item(i, image=None, title='Meissen Zwiebelmuster Kaffeetasse mit Untertasse'):
    return dict(item_id=i, platform='ebay', title=title, description='', price=40, currency='EUR',
                buying='FIXED_PRICE', url='https://www.ebay.de/itm/x', image=EBAY % i if image is None else image)


def answer(rows):
    r = Mock(status_code=200)
    r.json.return_value = {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': json.dumps(rows)}]}
    return r


def row(iid, score=50, function='FN-001', form='MS-002', pattern='MP-001', **extra):
    out = dict(item_id=iid, score=score, reason='r', single_cup=False, plain_white=False, not_porcelain=False,
               function=function, form=form, pattern=pattern)
    out.update(extra)
    return out


def run(db, kb, post, at=NOON, **kw):
    return ai.score_round(db, KEY, 100, lambda: at, post, kb_path=kb, **kw)


def system_text(post, n=-1):
    system = post.call_args_list[n].kwargs['json']['system']
    if isinstance(system, list):
        assert system[-1]['cache_control'] == {'type': 'ephemeral'}
        return ''.join(b['text'] for b in system)
    return system


def sent_items(post, n=-1):
    text = post.call_args_list[n].kwargs['json']['messages'][0]['content'][0]['text']
    return json.loads(text.rsplit('\n', 1)[1])


def score_of(db, iid):
    with db.connect() as c:
        r = c.execute('SELECT score FROM scores WHERE item_id=?', (iid,)).fetchone()
        return r[0] if r else None


def features(db):
    with db.connect() as c:
        return {r['item_id']: dict(r) for r in c.execute('SELECT * FROM visual_features')}


def line(db, iid):
    with db.connect() as c:
        return store_mod.card(c, c.execute('SELECT * FROM items WHERE item_id=?', (iid,)).fetchone())['vision_line']


def keep(db, iid):
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        triage.keep(c, iid)


def images_sent(post, n=-1):
    content = post.call_args_list[n].kwargs['json']['messages'][0]['content']
    return [b['text'][1:] for b in content if b['type'] == 'text' and b['text'].startswith('#')]


# ---- I/O 矩阵

def test_normal_partial_and_all_unknown(db, cfg, kb):
    for i in ('a', 'b', 'c'):
        db.ingest(item(i), cfg)
    post = Mock(return_value=answer([row('a'), row('b', form=None), row('c', function=None, form=None, pattern=None)]))
    result = run(db, kb, post)
    assert result['ai_status'] == 'ok' and result['ai_scored'] == 3 and result['ai_featured'] == 3
    # 词表以"编号 中文名"列入 system，模型只回编号
    system = system_text(post)
    assert 'FN-001 咖啡杯碟' in system and 'MS-002 新剪裁器型' in system and 'MP-001 蓝洋葱' in system
    assert system.count('MP-001 蓝洋葱') == 1                     # 同一编号多条词条只列一次
    assert 'FN-099 无别名件' in system                             # 没有别名词的 CSV 行也在词表里
    f = features(db)
    assert (f['a']['function_code'], f['a']['form_code'], f['a']['pattern_code']) == ('FN-001', 'MS-002', 'MP-001')
    assert f['a']['image'] == EBAY % 'a' and f['a']['model'] == ai.MODEL
    assert line(db, 'a') == '看图：咖啡杯碟 · 新剪裁器型 · 蓝洋葱'
    assert line(db, 'b') == '看图：咖啡杯碟 · 蓝洋葱'
    assert line(db, 'c') == '看图：未识别'
    # 已看过：下一轮不再请求
    post.reset_mock()
    assert run(db, kb, post)['ai_scored'] == 0 and not post.called


def test_invalid_code_is_unknown_and_score_kept(db, cfg, kb, caplog):
    db.ingest(item('a'), cfg)
    db.ingest(item('b'), cfg)
    post = Mock(return_value=answer([row('a', 77, pattern='MP-999', form=' ms-002 '),
                                     row('b', 66, function='Kaffeetasse', form='FN-001')]))
    with caplog.at_level(logging.WARNING):
        run(db, kb, post)
    f = features(db)
    assert f['a']['pattern_code'] is None and f['a']['function_code'] == 'FN-001'
    assert f['b']['function_code'] is None and f['b']['form_code'] is None      # 自由文字、跨类编号都不收
    assert '不在词表' in caplog.text
    with db.connect() as c:
        assert dict(c.execute('SELECT item_id,score FROM scores').fetchall()) == {'a': 77, 'b': 66}
    assert f['a']['form_code'] == 'MS-002'                         # 大小写与空白规整后收
    assert line(db, 'a') == '看图：咖啡杯碟 · 新剪裁器型'


def test_missing_fields_keep_score_and_checks_and_mark_sent(db, cfg, kb):
    db.ingest(item('a'), cfg)
    r = row('a', 61)
    for k in ('function', 'form', 'pattern'):
        del r[k]
    post = Mock(return_value=answer([r]))
    result = run(db, kb, post)
    assert result['ai_scored'] == 1 and result['ai_featured'] == 0
    assert features(db) == {} and line(db, 'a') is None
    with db.connect() as c:
        assert c.execute('SELECT score FROM scores WHERE item_id=?', ('a',)).fetchone()[0] == 61
        assert c.execute('SELECT 1 FROM visual_checks WHERE item_id=?', ('a',)).fetchone()
        assert c.execute('SELECT image FROM feature_attempts WHERE item_id=?', ('a',)).fetchone()[0] == EBAY % 'a'
    post.reset_mock()
    run(db, kb, post)
    assert not post.called                                         # 这张主图已送检，不再重发


def test_kept_items_backfilled_after_scoring_without_score_or_state_change(db, cfg, kb):
    """验收：一批附图新货与两件留下的货，一轮之后特征写入，留下的货分数与状态不变，01 / 02 卡片接口带 vision_line。"""
    for i in ('n1', 'n2', 'k1', 'k2'):
        db.ingest(item(i), cfg)
    keep(db, 'k1')
    keep(db, 'k2')
    with db.connect() as c:
        before = {r['item_id']: dict(r) for r in c.execute("SELECT * FROM scores WHERE item_id IN ('k1','k2')")}
        codes = {i: triage.code(c, i) for i in ('k1', 'k2')}
    names = {i: db.item_detail(i)['name'] for i in ('k1', 'k2')}
    # 留下的货回白瓷 / 非瓷器也不改状态
    post = Mock(return_value=answer([row('n1', 99), row('n2', 98),
                                     row('k1', plain_white=True), row('k2', not_porcelain=True)]))
    result = run(db, kb, post)
    assert result['ai_featured'] == 4 and result['ai_scored'] == 2 and result['ai_removed'] == 0
    # 每轮第一批先补留下的货（标 features_only），同批空位再放需要打分的新货
    sent = sent_items(post)
    assert [x['item_id'] for x in sent][:2] in (['k1', 'k2'], ['k2', 'k1'])
    assert {x['item_id'] for x in sent if x.get('features_only')} == {'k1', 'k2'}
    assert 'features_only' in system_text(post)
    assert set(images_sent(post)) == {'k1', 'k2', 'n1', 'n2'}
    with db.connect() as c:
        after = {r['item_id']: dict(r) for r in c.execute("SELECT * FROM scores WHERE item_id IN ('k1','k2')")}
        assert after == before                                    # 留下的货不写分数
        assert {i: triage.state(c, i) for i in ('k1', 'k2')} == {'k1': 'kept', 'k2': 'kept'}
        assert {i: triage.code(c, i) for i in ('k1', 'k2')} == codes
        assert not c.execute("SELECT 1 FROM ai_attempts WHERE item_id IN ('k1','k2')").fetchone()
    assert set(features(db)) == {'n1', 'n2', 'k1', 'k2'}
    # 01 瀑布流卡片与 02 商品池卡片、详情都带 vision_line
    want = '看图：咖啡杯碟 · 新剪裁器型 · 蓝洋葱'
    feed = db.triage_feed(limit=10)
    assert {c['item_id']: c['vision_line'] for c in feed['items']} == dict.fromkeys(('n1', 'n2'), want)
    pool = ranking.pool(db)
    assert {c['item_id']: c['vision_line'] for c in pool['items']} == dict.fromkeys(('k1', 'k2'), want)
    assert db.item_detail('k1')['vision_line'] == want
    # 标准名称不变
    assert {i: db.item_detail(i)['name'] for i in ('k1', 'k2')} == names


def test_features_only_reply_without_score_accepted(db, cfg, kb):
    db.ingest(item('k'), cfg)
    keep(db, 'k')
    r = row('k')
    del r['score'], r['reason']
    run(db, kb, Mock(return_value=answer([r])))
    assert features(db)['k']['function_code'] == 'FN-001' and score_of(db, 'k') is not None


def test_new_item_with_checks_but_no_features_backfilled_once(db, cfg, kb):
    db.ingest(item('a'), cfg)
    # 故事 19 时看过硬筛（没给词表），已有 AI 分与观察
    post = Mock(return_value=answer([dict(item_id='a', score=55, reason='r', single_cup=False, plain_white=False,
                                          not_porcelain=False)]))
    ai.score_round(db, KEY, 100, lambda: NOON, post)
    assert features(db) == {}
    post.reset_mock()
    assert ai.score_round(db, KEY, 100, lambda: NOON, post)['ai_scored'] == 0 and not post.called
    with db.connect() as c:
        checked = dict(c.execute('SELECT * FROM visual_checks WHERE item_id=?', ('a',)).fetchone())
    # 有词表之后补做一次：只补三项，不重新打分、不覆盖已有观察
    post = Mock(return_value=answer([row('a', 58, plain_white=True)]))
    result = run(db, kb, post)
    assert images_sent(post) == ['a'] and features(db)['a']['pattern_code'] == 'MP-001'
    assert sent_items(post)[0]['features_only'] is True
    assert result['ai_scored'] == 0 and score_of(db, 'a') == 55
    with db.connect() as c:
        assert dict(c.execute('SELECT * FROM visual_checks WHERE item_id=?', ('a',)).fetchone()) == checked
        assert triage.state(c, 'a') == 'new'
    post.reset_mock()
    run(db, kb, post)
    assert not post.called


def test_image_change_voids_features_and_rechecks(db, cfg, kb):
    db.ingest(item('a'), cfg)
    run(db, kb, Mock(return_value=answer([row('a')])))
    assert line(db, 'a')
    db.ingest(item('a', EBAY % 'a-new'), cfg)
    assert line(db, 'a') is None                                   # 主图变了：作废、不显示
    post = Mock(return_value=answer([row('a', pattern='MP-050')]))
    run(db, kb, post)
    assert 'a' in images_sent(post)
    f = features(db)['a']
    assert f['image'] == EBAY % 'a-new' and f['pattern_code'] == 'MP-050'
    assert line(db, 'a') == '看图：咖啡杯碟 · 新剪裁器型 · 蓝花'


def test_image_changed_during_round_discards_features(db, cfg, kb):
    db.ingest(item('a'), cfg)

    def post(*a, **k):
        db.ingest(item('a', EBAY % 'a2'), cfg)
        return answer([row('a')])
    run(db, kb, Mock(side_effect=post))
    assert features(db) == {}


def test_kb_missing_no_vocab_no_display(db, cfg, tmp_path, monkeypatch):
    missing = str(tmp_path / 'no-kb')
    db.ingest(item('a'), cfg)
    post = Mock(return_value=answer([dict(item_id='a', score=50, reason='r', single_cup=False, plain_white=False,
                                          not_porcelain=False)]))
    result = run(db, missing, post)
    system = post.call_args.kwargs['json']['system']
    assert system == ai.SYSTEM and 'FN-' not in system
    assert result['ai_scored'] == 1 and features(db) == {}
    with db.connect() as c:
        assert not c.execute('SELECT 1 FROM feature_attempts').fetchone()
    # 已有三项但显示时知识库缺失：不显示
    with db.connect() as c:
        c.execute("INSERT INTO visual_features(item_id,image,function_code,form_code,pattern_code,model,checked_at) "
                  "VALUES('a',?,'FN-001',NULL,NULL,'m','t')", (EBAY % 'a',))
    monkeypatch.setattr(store_mod, '_kb_path', lambda root=None: missing)
    assert line(db, 'a') is None


def test_image_rejected_marks_feature_attempt(db, cfg, kb):
    db.ingest(item('a'), cfg)
    bad = Mock(status_code=400, headers={})
    post = Mock(side_effect=[bad, answer([dict(item_id='a', score=50, reason='r')])])
    run(db, kb, post)
    assert post.call_count == 2 and images_sent(post, 1) == []
    with db.connect() as c:
        assert c.execute('SELECT image FROM feature_attempts WHERE item_id=?', ('a',)).fetchone()[0] == EBAY % 'a'
    post = Mock(return_value=answer([]))
    run(db, kb, post)
    assert not post.called
    # 7 天后可再为三项送检（只补三项）
    post = Mock(return_value=answer([row('a')]))
    run(db, kb, post, at=NOON + 8 * DAY)
    assert images_sent(post) == ['a'] and features(db)['a']['function_code'] == 'FN-001'


def test_image_rejected_drops_features_only_items_from_retry(db, cfg, kb):
    db.ingest(item('n'), cfg)
    db.ingest(item('k'), cfg)
    keep(db, 'k')
    bad = Mock(status_code=400, headers={})
    post = Mock(side_effect=[bad, answer([dict(item_id='n', score=50, reason='r')])])
    run(db, kb, post)
    assert post.call_count == 2 and [x['item_id'] for x in sent_items(post, 1)] == ['n']
    with db.connect() as c:
        assert {r[0] for r in c.execute('SELECT item_id FROM feature_attempts')} == {'n', 'k'}


def test_only_kept_batch_rejected_sends_no_retry(db, cfg, kb):
    db.ingest(item('k'), cfg)
    keep(db, 'k')
    post = Mock(return_value=Mock(status_code=400, headers={}))
    result = run(db, kb, post)
    assert post.call_count == 1 and result['ai_status'] == 'ok'


def test_omitted_items_not_marked_sent(db, cfg, kb):
    db.ingest(item('a'), cfg)
    db.ingest(item('b'), cfg)
    run(db, kb, Mock(return_value=answer([row('a')])))
    with db.connect() as c:
        assert {r[0] for r in c.execute('SELECT item_id FROM feature_attempts')} == {'a'}


def test_run_passes_kb_path_and_all_paths_have_featured(db, cfg, kb, monkeypatch):
    db.ingest(item('a'), cfg)
    monkeypatch.setattr('curio.config.anthropic_key', lambda root=None: KEY)
    post = Mock(return_value=answer([row('a')]))
    result = ai.run(db, ROOT, cfg, lambda: NOON, post)
    assert 'FN-001 咖啡杯碟' in system_text(post) and result['ai_featured'] == 1
    monkeypatch.setattr('curio.config.anthropic_key', lambda root=None: None)
    assert ai.run(db, ROOT, cfg, lambda: NOON, post)['ai_featured'] == 0
    assert ai.score_round(db, None, 100, lambda: NOON, post)['ai_featured'] == 0
    monkeypatch.setattr('curio.config.anthropic_key', lambda root=None: KEY)
    monkeypatch.setattr(ai, 'score_round', Mock(side_effect=RuntimeError('x')))
    assert ai.run(db, ROOT, cfg, lambda: NOON, post)['ai_featured'] == 0


def test_kb_path_read_from_settings(tmp_path, kb, monkeypatch, db, cfg):
    root = tmp_path / 'root'
    shutil.copytree(ROOT / 'config', root / 'config')
    import yaml
    f = root / 'config/settings.yaml'
    settings = yaml.safe_load(f.read_text()) or {}
    settings['em_kb_path'] = kb
    f.write_text(yaml.safe_dump(settings, allow_unicode=True))
    store_mod._KB_PATH_CACHE.clear()
    assert REAL_KB_PATH(root) == kb
    monkeypatch.setattr(store_mod, '_kb_path', REAL_KB_PATH)
    db.ingest(item('a'), cfg)
    run(db, kb, Mock(return_value=answer([row('a', form='MS-777')])))
    with db.connect() as c:
        c.execute("UPDATE visual_features SET form_code='MS-777'")      # 词表里已没有的编号：显示编号
        assert store_mod.card(c, c.execute("SELECT * FROM items WHERE item_id='a'").fetchone(), root)['vision_line'] \
            == '看图：咖啡杯碟 · MS-777 · 蓝洋葱'


def test_kb_error_does_not_break_cards(db, cfg, kb, monkeypatch):
    db.ingest(item('a'), cfg)
    run(db, kb, Mock(return_value=answer([row('a')])))
    monkeypatch.setattr(ai, 'feature_vocab', Mock(side_effect=OSError('boom')))
    assert line(db, 'a') is None
    assert db.item_detail('a')['vision_line'] is None


def test_no_kb_kept_not_candidate(db, cfg, tmp_path):
    db.ingest(item('k'), cfg)
    keep(db, 'k')
    post = Mock(return_value=answer([]))
    run(db, str(tmp_path / 'no-kb'), post)
    assert not post.called


def test_no_image_items_not_asked_for_features(db, cfg, kb):
    db.ingest(item('a', image=''), cfg)
    post = Mock(return_value=answer([row('a')]))
    run(db, kb, post)
    assert images_sent(post) == [] and features(db) == {}


def test_kept_item_without_vision_image_not_candidate(db, cfg, kb):
    db.ingest(item('k', image='https://i.etsystatic.com/x.jpg'), cfg)
    keep(db, 'k')
    post = Mock(return_value=answer([]))
    run(db, kb, post)
    assert not post.called


def test_parse_features_unit():
    fv = {'function': {'FN-001': '咖啡杯碟'}, 'form': {'MS-002': '新剪裁器型'}, 'pattern': {'MP-001': '蓝洋葱'}}
    text = json.dumps([row('a'), row('b', pattern=7), dict(item_id='c', score=1, reason='')])
    got = ai.parse(text, {'a', 'b', 'c'}, {'a': 'u', 'b': 'u', 'c': 'u'}, fv)
    assert got['a'][3] == dict(function='FN-001', form='MS-002', pattern='MP-001')
    assert got['b'][3]['pattern'] is None and got['c'][3] is None
    assert ai.parse(text, {'a'}, {'a': 'u'})['a'][3] is None       # 没给词表不收三项
    # 某类词表为空：该字段可缺省，按 null
    empty = dict(fv, pattern={})
    r = row('d', function='fn-001')
    del r['pattern']
    assert ai.parse(json.dumps([r]), {'d'}, {'d': 'u'}, empty)['d'][3] == dict(function='FN-001', form='MS-002', pattern=None)
    # features_only 不要求 score
    got = ai.parse(json.dumps([dict(item_id='k', function='FN-001', form=None, pattern=None)]), {'k'}, {'k': 'u'}, fv, {'k'})
    assert got['k'][0] is None and got['k'][3]['function'] == 'FN-001'


def test_first_batch_backfills_pool_then_new_items_get_scored(db, cfg, kb):
    """新货多于一批时：第一批先补留下的货，之后的批次新货打分优先（商品池不会一直轮不到，新货也不会被挤掉）。"""
    new = ['n%02d' % i for i in range(ai.BATCH_SIZE + 5)]
    for i in new + ['k1']:
        db.ingest(item(i), cfg)
    keep(db, 'k1')
    calls = []

    def reply(*a, **kw):
        items = json.loads(kw['json']['messages'][0]['content'][0]['text'].rsplit('\n', 1)[1])
        calls.append(items)
        return answer([row(x['item_id'], 70) for x in items])
    post = Mock(side_effect=reply)
    result = run(db, kb, post)
    assert 'k1' in {x['item_id'] for x in calls[0]}                       # 第一批带上留下的货
    assert all(not x.get('features_only') for x in calls[1])              # 第二批全是要打分的新货
    assert result['ai_scored'] == len(new) and 'k1' in features(db)
