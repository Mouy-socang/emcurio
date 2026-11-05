"""故事 33：拍卖行拍品单独的筛选口径（屏蔽词只看标题、整词；标题无品牌时看描述）；eBay 口径不变。"""
import pytest

from curio import rules, triage
from curio.config import load
from curio.store import Store, judge_config, rejudge


@pytest.fixture
def cfg():
    return dict(load(), blocked_words=['Pins', 'AK', 'Band', 'Horn'], learned_rules=[])


def item(platform='auction_house', title='KPM Berlin Deckelvase Blumenmalerei', description='', price=330.0):
    return dict(item_id='ah-mehlis-124-3238' if platform == 'auction_house' else 'v1|1|0', platform=platform,
                title=title, description=description, listed_price=price, price_text='Limit 330 €',
                currency='EUR', condition='', buying='AUCTION', human_decided=False)


def test_house_ignores_blocked_word_fragments_in_description(cfg):
    desc = 'Unterglasurblaue Szeptermarke, Pinselnummer 12, Goldakzente, Lit.: Band 2, S. 14.'
    r = rules.evaluate(item(description=desc), cfg)
    assert not r['reason'].startswith('屏蔽词')
    assert r['decision'] != 'skip'


def test_house_blocked_word_in_title_is_whole_word_only(cfg):
    assert not rules.evaluate(item(title='Meissen Zuckerdose mit Akanthusblattrelief'), cfg)['reason'].startswith('屏蔽词')
    assert not rules.evaluate(item(title='Herend Einhorn "Vieux Herend"'), cfg)['reason'].startswith('屏蔽词')
    assert not rules.evaluate(item(title='Meissen Hornbecher'), cfg)['reason'].startswith('屏蔽词')
    assert rules.evaluate(item(title='MEISSEN HORN'), cfg)['reason'] == '屏蔽词：Horn（标题）'
    assert not rules.evaluate(item(description='Ein Horn, Band 2.'), cfg)['reason'].startswith('屏蔽词')   # 描述里的整词也不看
    hit = rules.evaluate(item(title='Meissen Horn mit Silbermontierung'), cfg)
    assert (hit['decision'], hit['reason']) == ('skip', '屏蔽词：Horn（标题）')


def test_house_brand_from_description_when_title_has_none(cfg):
    r = rules.evaluate(item(title='Doppelhenkelbecher mit Seladonfond und seltener Nachtszene',
                            description='Meissen, um 1730. Blaue Schwertermarke.'), cfg)
    assert r['reason'] != rules.NO_BRAND and r['brand'] == 'Meissen'
    none = rules.evaluate(item(title='Teller mit Wanli-Dekor', description='China, Ming-Dynastie.'), cfg)
    assert (none['decision'], none['reason']) == ('skip', rules.NO_BRAND)


def test_house_damage_and_hard_filter_still_apply(cfg):
    assert rules.evaluate(item(description='Knauf restauriert.'), cfg)['reason'].startswith('破损或修复')


def test_ebay_unchanged(cfg):
    desc = 'Pinselnummer 12'
    r = rules.evaluate(item('ebay', description=desc), cfg)
    assert (r['decision'], r['reason']) == ('skip', '屏蔽词：Pins（描述）')
    assert rules.evaluate(item('ebay', title='Doppelhenkelbecher', description='Meissen'), cfg)['reason'] == rules.NO_BRAND
    assert rules.evaluate(item('ebay', title='Herend Einhorn'), cfg)['reason'] == '屏蔽词：Horn（标题）'   # eBay 仍按片段


def test_house_kpm_brand_from_description_keeps_krister_warning(cfg):
    r = rules.evaluate(item(title='Kaffeekanne mit Blumendekor', description='KPM Krister, Waldenburg, um 1900.'), cfg)
    assert r['decision'] == 'probe' and 'Krister' in r['reason']


def test_block_on_house_item_needs_whole_title_word(tmp_path):
    db = Store(tmp_path / 'data' / 'curio.db')
    base = load()
    db.ingest(dict(item_id='ah-mehlis-124-3200', platform='auction_house', title='KPM Berlin Bildplatte Morgengruß',
                   description='Pinselnummer 12', currency='EUR', price=480, price_text='Limit 480 €', buying='AUCTION',
                   url='https://x', image='', ends='2026-11-21T09:00:00.000Z'), base)
    with db.connect() as c:
        v = c.execute("SELECT rule_version FROM items").fetchone()[0]
    for word in ('Pins', 'Bildpl'):
        with pytest.raises(ValueError, match='完整的词'):
            db.block('ah-mehlis-124-3200', word, 'r-' + word, v, base)
    db.block('ah-mehlis-124-3200', 'Bildplatte', 'r-ok', v, base)


def test_rejudge_platform_only_moves_unreviewed_house_items(tmp_path):
    db = Store(tmp_path / 'data' / 'curio.db')
    base = load()
    with db.connect() as c:
        c.execute("INSERT INTO blocked_words VALUES('Pins','x')")
    desc = 'Pinselnummer 12, blaue Szeptermarke.'
    for iid, platform in (('ah-mehlis-124-3238', 'auction_house'), ('ah-mehlis-124-3239', 'auction_house'), ('v1|9|0', 'ebay')):
        db.ingest(dict(item_id=iid, platform=platform, title='KPM Berlin Deckelvase Blumenmalerei', description=desc,
                       currency='EUR', price=330, price_text='Limit 330 €', buying='AUCTION', url='https://x', image='',
                       ends='2026-11-21T09:00:00.000Z'), base)
    with db.connect() as c:
        # 模拟故事 32 时按旧口径写下的结论：拍卖行两件被 Pins（描述）去掉；其中一件墨墨已手动去掉（人工决定优先）
        c.execute("UPDATE items SET agent_decision='skip',agent_reason='屏蔽词：Pins（描述）' WHERE platform='auction_house'")
        for iid in ('ah-mehlis-124-3238', 'ah-mehlis-124-3239'):
            triage.system_place(c, iid)
        triage.remove(c, 'ah-mehlis-124-3239', '人工', source='manual')
        assert triage.state(c, 'ah-mehlis-124-3238') == 'auto_removed'
        out = rejudge(c, judge_config(c, base), platform='auction_house')
        ebay_v = c.execute("SELECT rule_version FROM items WHERE item_id='v1|9|0'").fetchone()[0]
        assert out['checked'] == 2 and out['changed'] == 2 and out['moved'] == 1   # 两件的判断都变了；人工去掉的那件不移动
        assert triage.state(c, 'ah-mehlis-124-3238') == 'new'
        assert triage.state(c, 'ah-mehlis-124-3239') == 'removed'
        assert c.execute("SELECT agent_reason, rule_version FROM items WHERE item_id='v1|9|0'").fetchone()[:] == ('屏蔽词：Pins（描述）', ebay_v)


def test_auction_list_source_filter(tmp_path):
    db = Store(tmp_path / 'data' / 'curio.db')
    base = load()
    common = dict(currency='EUR', price=100, buying='AUCTION', url='https://x', image='', description='')
    db.ingest(dict(common, item_id='v1|5|0', platform='ebay', title='Meissen Teller Zwiebelmuster', ends='2026-11-06T10:00:00.000Z'), base)
    db.ingest(dict(common, item_id='ah-mehlis-124-3300', platform='auction_house', title='Meissen Teller Zwiebelmuster',
                   price_text='Limit 100 €', ends='2026-11-21T09:00:00.000Z'), base)
    at = '2026-11-05T12:00:00+00:00'
    ids = lambda **kw: [c['item_id'] for c in db.auction_list(40, at=at, **kw)['items']]
    assert ids() == ['v1|5|0', 'ah-mehlis-124-3300']
    assert ids(source='auction_house') == ['ah-mehlis-124-3300']
    assert ids(source='ebay') == ['v1|5|0']
    first = db.auction_list(1, at=at, source='auction_house')
    assert first['next_cursor'] is None                               # 游标与来源一起生效
    with pytest.raises(ValueError):
        db.auction_list(40, at=at, source='etsy')
