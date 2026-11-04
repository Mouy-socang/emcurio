"""故事 19：看图硬筛——AI 打分请求附主图、模型回三项观察、规则归口决定自动移出（全部 mock，临时库，不联网）。"""
import json
from datetime import datetime
from unittest.mock import Mock, patch

import pytest

from curio import ai, judge, triage
from curio.config import load
from curio.schedule import BERLIN
from curio.store import Store

NOON = datetime(2026, 11, 4, 12, tzinfo=BERLIN).timestamp()
KEY = 'sk-ant-KEY-SENTINEL-19'
EBAY = 'https://i.ebayimg.com/images/g/%s/s-l1600.jpg'
KA = 'https://img.kleinanzeigen.de/api/v1/prod-ads/images/aa/%s?rule=$_59.JPG'
CUP = '硬筛（看图）：主图只有杯子，不带碟'
WHITE = '硬筛（看图）：白瓷（含金饰）'
NOTP = '硬筛（看图）：不是瓷器'


@pytest.fixture(autouse=True)
def no_network():
    with patch('requests.sessions.Session.request', side_effect=AssertionError('测试不得联网')):
        yield


@pytest.fixture(autouse=True)
def no_sleep():
    with patch('curio.ai._sleep'):
        yield


@pytest.fixture
def cfg():
    return dict(load(), keywords=['Meissen'])


@pytest.fixture
def db(tmp_path):
    return Store(tmp_path / 'curio.db')


def item(i, image='', title='Meissen Zwiebelmuster Tasse', platform='ebay'):
    return dict(item_id=i, platform=platform, title=title, description='', price=40, currency='EUR',
                buying='FIXED_PRICE', url='https://www.ebay.de/itm/x', image=image)


def answer(rows):
    r = Mock(status_code=200)
    r.json.return_value = {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': json.dumps(rows)}]}
    return r


def obs(iid, score=50, single_cup=False, plain_white=False, not_porcelain=False):
    return dict(item_id=iid, score=score, reason='r', single_cup=single_cup, plain_white=plain_white,
                not_porcelain=not_porcelain)


def state(db, iid):
    with db.connect() as c:
        return triage.state(c, iid)


def checks(db):
    with db.connect() as c:
        return {r['item_id']: dict(r) for r in c.execute('SELECT * FROM visual_checks')}


def last_event(db, iid):
    with db.connect() as c:
        return dict(c.execute('SELECT * FROM triage_events WHERE item_id=? ORDER BY id DESC LIMIT 1', (iid,)).fetchone())


def blocks(post):
    return post.call_args.kwargs['json']['messages'][0]['content']


def images_sent(post):
    return [b['source']['url'] for b in blocks(post) if b['type'] == 'image']


# ---- 验收：单杯、杯碟、白瓷金边

def test_round_removes_single_cup_and_plain_white(db, cfg):
    db.ingest(item('cup', EBAY % 'cup'), cfg)
    db.ingest(item('set', EBAY % 'set'), cfg)
    db.ingest(item('white', KA % 'white', title='Meissen Teller', platform='kleinanzeigen'), cfg)
    post = Mock(return_value=answer([obs('cup', 40, single_cup=True), obs('set', 70), obs('white', 30, plain_white=True)]))
    result = ai.score_round(db, KEY, 100, lambda: NOON, post)
    assert result['ai_status'] == 'ok' and result['ai_scored'] == 3 and result['ai_removed'] == 2
    assert (state(db, 'cup'), state(db, 'set'), state(db, 'white')) == ('auto_removed', 'new', 'auto_removed')
    with db.connect() as c:
        assert {r['item_id']: r['source'] for r in c.execute('SELECT * FROM scores')} == dict.fromkeys(('cup', 'set', 'white'), 'ai')
        listed = {r['item_id']: r['reason'] for r in triage.auto_removed(c)}
    assert listed == {'cup': CUP, 'white': WHITE}
    ev = last_event(db, 'cup')
    assert (ev['old_state'], ev['new_state'], ev['actor'], ev['reason'], ev['score']) == ('new', 'auto_removed', 'system', CUP, 40)
    assert last_event(db, 'white')['reason'] == WHITE
    # 请求体：3 个 image url 块，eBay 改为 s-l400，每张图前有 #item_id 标签
    imgs = images_sent(post)
    assert len(imgs) == 3 and all(b['source']['type'] == 'url' for b in blocks(post) if b['type'] == 'image')
    assert EBAY.replace('s-l1600', 's-l400') % 'cup' in imgs and KA % 'white' in imgs
    bs = blocks(post)
    for i in ('cup', 'set', 'white'):
        at = [b.get('text') for b in bs].index('#' + i)
        assert bs[at + 1]['type'] == 'image'
    # 观察绑定送检时的原地址
    assert checks(db)['cup']['image'] == EBAY % 'cup' and checks(db)['set']['single_cup'] == 0
    # 已看过、分数未过期：下一轮不再请求
    post.reset_mock()
    assert ai.score_round(db, KEY, 100, lambda: NOON, post)['ai_scored'] == 0 and not post.called


def test_not_porcelain_and_priority(db, cfg):
    db.ingest(item('book', EBAY % 'book', title='Meissen Porzellan'), cfg)
    db.ingest(item('both', EBAY % 'both'), cfg)
    post = Mock(return_value=answer([obs('book', not_porcelain=True), obs('both', single_cup=True, plain_white=True)]))
    ai.score_round(db, KEY, 100, lambda: NOON, post)
    assert last_event(db, 'book')['reason'] == NOTP and last_event(db, 'both')['reason'] == CUP


def test_visual_filter_rule():
    assert judge.visual_filter(None) is None and judge.visual_filter({}) is None
    assert judge.visual_filter(dict(single_cup=0, plain_white=0, not_porcelain=0)) is None
    assert judge.visual_filter(dict(single_cup=0, plain_white=1, not_porcelain=1)) == WHITE
    assert judge.visual_filter(dict(single_cup=1, plain_white=1, not_porcelain=1)) == CUP


# ---- 人工决定优先、主图变化

def test_rescued_item_not_removed_by_vision(db, cfg):
    db.ingest(item('r', EBAY % 'r', title='Meissen defekt Tasse'), cfg)       # 判断 skip → 自动移出
    assert state(db, 'r') == 'auto_removed'
    with db.connect() as c:
        triage.rescue(c, 'r', 'ok', source='review')
    assert state(db, 'r') == 'new'
    post = Mock(return_value=answer([obs('r', single_cup=True)]))
    ai.score_round(db, KEY, 100, lambda: NOON, post)
    assert post.called and checks(db)['r']['single_cup'] == 1
    assert state(db, 'r') == 'new' and last_event(db, 'r')['actor'] == 'human'


def test_image_change_voids_observation(db, cfg):
    db.ingest(item('a', EBAY % 'old'), cfg)
    post = Mock(return_value=answer([obs('a', single_cup=True)]))
    ai.score_round(db, KEY, 100, lambda: NOON, post)
    assert state(db, 'a') == 'auto_removed'
    db.ingest(item('a', EBAY % 'new'), cfg)                                   # 主图变了：旧观察作废、重回新货
    assert state(db, 'a') == 'new' and last_event(db, 'a')['old_state'] == 'auto_removed'
    post = Mock(return_value=answer([obs('a', 60)]))
    ai.score_round(db, KEY, 100, lambda: NOON, post)                          # 重回候选，看新图
    assert images_sent(post) == [EBAY.replace('s-l1600', 's-l400') % 'new']
    assert checks(db)['a']['image'] == EBAY % 'new' and state(db, 'a') == 'new'


def test_stale_observation_ignored_by_placement(db, cfg):
    db.ingest(item('a', EBAY % 'x'), cfg)
    with db.connect() as c:
        c.execute("INSERT INTO visual_checks VALUES('a',?,1,0,0,'m','t')", (EBAY % 'other',))
        assert triage.system_place(c, 'a') == 'new'


def test_image_changed_while_scoring_discards_observation(db, cfg):
    db.ingest(item('a', EBAY % 'one'), cfg)

    def post(url, headers, json, timeout):
        db.ingest(item('a', EBAY % 'two'), cfg)
        return answer([obs('a', 55, single_cup=True)])
    ai.score_round(db, KEY, 100, lambda: NOON, post)
    assert checks(db) == {} and state(db, 'a') == 'new'


# ---- 无图 / 外站 / 观察缺失

@pytest.mark.parametrize('image', ['', 'https://i.etsystatic.com/x/il_794xN.jpg', 'http://i.ebayimg.com/images/g/x/s-l500.jpg',
                                   'https://user:pw@i.ebayimg.com/a.jpg', 'https://example.com/a.jpg'])
def test_no_or_foreign_image_scores_only(db, cfg, image):
    db.ingest(item('a', image), cfg)
    post = Mock(return_value=answer([obs('a', 66, single_cup=True)]))
    result = ai.score_round(db, KEY, 100, lambda: NOON, post)
    assert result['ai_scored'] == 1 and result['ai_removed'] == 0 and images_sent(post) == [] and len(blocks(post)) == 1
    assert checks(db) == {} and state(db, 'a') == 'new'


def test_http_400_with_images_retries_once_without_images(db, cfg):
    db.ingest(item('a', EBAY % 'a'), cfg)
    db.ingest(item('b'), cfg)
    bad = Mock(status_code=400)
    post = Mock(side_effect=[bad, answer([obs('a', 61, single_cup=True), obs('b', 52)])])
    result = ai.score_round(db, KEY, 100, lambda: NOON, post)
    assert post.call_count == 2 and result['ai_status'] == 'ok' and result['ai_scored'] == 2
    first, second = (cl.kwargs['json']['messages'][0]['content'] for cl in post.call_args_list)
    assert [b['type'] for b in first].count('image') == 1 and len(second) == 1        # 重发不带图
    assert checks(db) == {} and state(db, 'a') == 'new' and result['ai_removed'] == 0
    with db.connect() as c:
        assert c.execute("SELECT image FROM ai_attempts WHERE item_id='a'").fetchone()[0] == EBAY % 'a'
    # 这张主图已送检：之后（含 24 小时后）不再因"未看过"重发
    post = Mock()
    ai.score_round(db, KEY, 100, lambda: NOON + 25 * 3600, post)
    assert not post.called


def test_http_400_retry_failure_does_not_stall_scoring(db, cfg):
    db.ingest(item('a', EBAY % 'a'), cfg)
    post = Mock(return_value=Mock(status_code=400))
    result = ai.score_round(db, KEY, 100, lambda: NOON, post)
    assert post.call_count == 2 and result['ai_status'] == 'error'
    # 下一轮只为打分重发（不带图）
    post = Mock(return_value=answer([obs('a', 44)]))
    assert ai.score_round(db, KEY, 100, lambda: NOON, post)['ai_scored'] == 1 and images_sent(post) == []


@pytest.mark.parametrize('bad', [{}, {'single_cup': 'true'}, {'plain_white': None}, {'not_porcelain': 1}])
def test_missing_or_non_bool_observation(db, cfg, caplog, bad):
    db.ingest(item('a', EBAY % 'a'), cfg)
    row = dict(obs('a', 61, single_cup=True), **bad)
    if not bad:
        row = dict(item_id='a', score=61, reason='r')
    post = Mock(return_value=answer([row]))
    with caplog.at_level('WARNING'):
        result = ai.score_round(db, KEY, 100, lambda: NOON, post)
    assert result['ai_scored'] == 1 and checks(db) == {} and state(db, 'a') == 'new'
    assert any('看图观察' in r.message for r in caplog.records)
    # 同一张主图只送检一次：之后（含 24 小时后）不再因"未看过"重发
    post.reset_mock()
    ai.score_round(db, KEY, 100, lambda: NOON + 25 * 3600, post)
    assert not post.called


def test_scored_item_without_check_becomes_candidate_once(db, cfg):
    db.ingest(item('a', EBAY % 'a'), cfg)
    with db.connect() as c:                                                   # 故事 19 之前已有 AI 分、未看过图
        c.execute("UPDATE scores SET source='ai'")
        c.execute("INSERT INTO ai_attempts(item_id,attempted_at,rule_version) VALUES('a','2026-11-04T10:00:00+00:00',1)")
    post = Mock(return_value=answer([obs('a', 50, plain_white=True)]))
    ai.score_round(db, KEY, 100, lambda: NOON, post)
    assert post.call_count == 1 and state(db, 'a') == 'auto_removed' and last_event(db, 'a')['reason'] == WHITE
    post.reset_mock()
    ai.score_round(db, KEY, 100, lambda: NOON + 25 * 3600, post)
    assert not post.called


def test_vision_url():
    assert ai.vision_url(EBAY % 'g') == 'https://i.ebayimg.com/images/g/g/s-l400.jpg'
    assert ai.vision_url('https://i.ebayimg.com/thumbs/images/g/x/s-l225.webp') == 'https://i.ebayimg.com/thumbs/images/g/x/s-l400.webp'
    assert ai.vision_url(KA % 'k') == KA % 'k'
    for bad in (None, '', 'ftp://i.ebayimg.com/a.jpg', 'https://i.ebayimg.com:444/a.jpg', 'https://i.etsystatic.com/a.jpg',
                'https://evil.com/i.ebayimg.com/a.jpg', 'https://i.ebayimg.com.evil.com/a.jpg'):
        assert ai.vision_url(bad) is None


def test_parse_observations():
    text = json.dumps([obs('a', single_cup=True), obs('b', plain_white=True), dict(item_id='c', score=5, reason='x', single_cup=True)])
    got = ai.parse(text, {'a', 'b', 'c'}, {'a': 'u', 'b': 'u'})
    assert got['a'][2] == dict(single_cup=1, plain_white=0, not_porcelain=0)
    assert got['b'][2]['plain_white'] == 1 and got['c'][2] is None          # 没附图的货不收观察


# ---- 标题白瓷收紧

@pytest.mark.parametrize('title', ['KPM weiß Goldrand Teller', 'Meissen Teller weiss vergoldet', 'Meissen white gold rim'])
def test_title_white_with_gold_is_plain(title):
    assert judge.hard_filter(title) == '纯白瓷（标题写 weiß）'
    assert judge.judge(title, 40)['decision'] == 'skip'


@pytest.mark.parametrize('title', ['KPM Teller weiß mit blauem Dekor', 'Meissen Rote Rose weiß Goldrand', 'blau-weiß Zwiebelmuster',
                                   'Meissen Teller weiß Wildrose', 'Meissen white roses plate', 'Meissen Tasse weiß Purpur Goldrand',
                                   'Meissen weiß gold Watteau', 'Meissen Teller weiß Goldchinesen', 'Meissen Chinoiserie weiß',
                                   'Meissen Kakiemon weiß', 'Meissen Imari weiß', 'Meissen weiß Vogel Teller',
                                   'Meissen weiß Drache Goldrand', 'Meissen weiß Landschaft', 'Meissen weiß Früchte Teller',
                                   'Meissen weiß Obst', 'Meissen weiß Streublümchen', 'Meissen weiß rot Teller',
                                   'Meissen weiß grün', 'Meissen farbig weiß', 'Famille rose weiß'])
def test_title_white_with_painting_is_not_plain(title):
    assert judge.hard_filter(title) is None


def test_title_white_gold_auto_removed_on_ingest(db, cfg):
    db.ingest(item('kpm', title='KPM weiß Goldrand Teller'), cfg)
    assert state(db, 'kpm') == 'auto_removed'


def test_title_white_rosenthal_is_not_a_rose_decoration():
    # "Rosenthal" 不是玫瑰装饰：写 weiß 的 Rosenthal 白瓷照样按标题白瓷移出；Rote Rose / Rosendekor 仍算有装饰。
    assert judge.hard_filter('Rosenthal Vase weiß') == '纯白瓷（标题写 weiß）'
    assert judge.hard_filter('Meissen Rote Rose weiß Teller') is None
    assert judge.hard_filter('KPM weiß Rosen Tasse') is None
