"""综合分绝对刻度（scoring.absolute = 1）：成分是留下率与成交量本身的大小，不是在全部货里的名次。"""
import math

import pytest

from curio import judge
from curio.config import ROOT, scoring_override, scoring_rules

ABS = scoring_override(scoring_rules(ROOT), {'absolute': 1, 'ai_share': 0})


def _it(iid, label=None, brand='Meissen', pattern='p1', function='f1', price=100.0, ai=None):
    return dict(item_id=iid, listed_price=price, currency='EUR', brand=brand, pattern_id=pattern,
                function_id=function, label=label, ai_score=ai)


def test_default_config_keeps_percentiles():
    assert scoring_rules(ROOT)['absolute'] == 0


def test_absolute_parts_formulas():
    pt, pv, pr = judge.absolute_parts({'a': 0.25, 'b': 1.4}, {'a': 0.0, 'b': None}, {'a': 10.0, 'b': None}, ABS)
    assert pt == {'a': 25.0, 'b': 100.0}                       # 留下率 × 100，封顶 100
    assert pv == {'a': 50.0, 'b': 50.0}                        # 同价 50；缺值 50
    assert pr['a'] == pytest.approx(100 * (1 - math.exp(-10 / ABS['trend_scale'])))
    assert pr['b'] == 50.0


def test_cheaper_is_higher_value():
    _, pv, _ = judge.absolute_parts({}, {'cheap': -math.log(0.5), 'dear': -math.log(2)}, {}, ABS)
    assert pv['cheap'] > 50 > pv['dear']


def test_taste_reflects_keep_rate_not_rank():
    """很多货都被去掉时，留下率低的同类在百分位下会很高，绝对刻度下仍然低。"""
    junk = [_it('j%d' % i, label=0, brand='Other', pattern=None, function=None) for i in range(200)]
    low = [_it('l%d' % i, label=1 if i == 0 else 0) for i in range(8)]       # 梅森 p1·f1：留下 1/8
    items = junk + low + [_it('x')]
    pct = judge.composite_scores(items, {}, {}, scoring_override(ABS, {'absolute': 0}))
    ab = judge.composite_scores(items, {}, {}, ABS)
    assert pct['x']['momo'] > 90
    assert ab['x']['momo'] < 50                                 # 名次 90+，留下率（含价位修正）不到一半
    assert ab['x']['momo'] > ab['j0']['momo']                   # 次序不变


def test_ai_score_used_as_is_on_absolute():
    p = scoring_override(ABS, {'ai_share': 0.5})
    items = [_it('a', label=1), _it('b', label=0), _it('x', ai=80.0)]
    out = judge.composite_scores(items, {}, {}, p)
    rate, _ = judge.taste(items[2], *judge.taste_counts(items, p), p)
    assert out['x']['momo'] == pytest.approx(0.5 * min(100, 100 * rate) + 0.5 * 80.0)


def test_missing_demand_trend_is_50():
    out = judge.composite_scores([_it('a', label=1), _it('b')], None, {}, ABS)
    assert out['b']['trend'] == 50.0


@pytest.mark.parametrize('sets, msg', [({'absolute': 2}, 'absolute'), ({'absolute': 0.5}, 'absolute'),
                                       ({'trend_scale': 0}, 'trend_scale')])
def test_bad_scale_params_rejected(sets, msg):
    with pytest.raises(ValueError, match=msg):
        scoring_override(scoring_rules(ROOT), sets)
