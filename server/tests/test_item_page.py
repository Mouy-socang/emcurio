"""故事 12（CAP-10、CAP-11）：客人单件页、预估费用、墓碑与币种——服务器一侧（TestClient，不连任何外部主机）。

覆盖 I/O 矩阵：¥ 显示（汇率 7.80、€185 → "≈ ¥1,443"，费用同样换算）、汇率过期（8 天前 → 只显示欧元、不出现切换）、
币种记忆（选 ¥ 后在另一设备登录仍为 ¥）、复制货号（页面结构；剪贴板行为在浏览器里人工检查）、已售出（隐藏 30 天内
去色、"已售出"、无复制栏）、超过 90 天"此件已不在展出"。
"""
import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import app as server
import guest

from test_snapshot import AUTH, SAME_ORIGIN, TOKEN, Clock, build, hide, make_item, put_image, send

NOW = 1_800_000_000.0           # 2027-01-15（UTC）
TODAY = datetime.fromtimestamp(NOW, timezone.utc).date()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def db(tmp_path):
    return tmp_path / 'server.db'


def make_client(db, clock):
    return TestClient(server.create_app(db_path=db, admin_token=TOKEN, clock=clock, cookie_secure=False,
                                        public_url='https://curio.test'),
                      follow_redirects=False, headers=SAME_ORIGIN)


@pytest.fixture
def client(db, clock):
    return make_client(db, clock)


def day(offset):
    return (TODAY - timedelta(days=offset)).isoformat()


def items_with(client, n=12, fee=None, comment=None, imgs=2):
    out = []
    for k in range(1, n + 1):
        it = make_item(k, [(put_image(client, f'b{k}-{j}'), put_image(client, f't{k}-{j}')) for j in range(imgs)])
        it['fee_tier'], it['fee_amount'], it['fee_label'] = 'B', 42, '30cm 以下花瓶'
        out.append(it)
    out[0].update(price_eur=185, price_band='lt200', seller_text='Sehr guter Zustand.\nKeine Chips.', seller_lang='de')
    if fee:
        out[0].update(fee)
        if fee.get('fee_amount') is None:
            out[0].pop('fee_amount', None)
            out[0].pop('fee_label', None)
    if comment:
        out[0]['comment'] = comment
    return out


def publish(client, no=1, rate=7.80, date=None, tombstones=(), **kw):
    payload = build(no, items_with(client, **kw), tombstones=tombstones)
    if rate is not None:
        payload.update(fx_rate=rate, fx_date=date or day(1))
    r = send(client, 'snapshot', payload)
    assert r.status_code == 200, r.text
    return payload


def register(client, username='wangjie', first=True):
    if first:
        env = {'schema_version': 1, 'event_id': str(uuid.uuid4()), 'kind': 'set_invite_code',
               'created_at': '2026-11-03T08:00:00.000Z',
               'payload': {'schema_version': 1, 'command_id': str(uuid.uuid4()), 'target_seq': 1, 'code': 'MOMO2026'}}
        assert client.post('/admin/events', json=env, headers=AUTH).status_code == 200
    r = client.post('/register', data={'invite': 'MOMO2026', 'username': username, 'password': 'meissen1710',
                                       'nickname': '王姐', 'email': '', 'consent': 'yes', 'next': '/'})
    assert r.status_code == 303


def login(client, username='wangjie'):
    r = client.post('/login', data={'username': username, 'password': 'meissen1710', 'next': '/'})
    assert r.status_code == 303


def choose(client, currency, js=True, next_path='/item/CO-0001'):
    headers = {'Accept': 'application/json'} if js else {}
    return client.post('/currency', data={'currency': currency, 'next': next_path}, headers=headers)


def text_between(html, start, end):
    return html[html.index(start):html.index(end, html.index(start))]


# ---------- 单件页结构 ----------

def test_item_page_full_order_and_wording(client):
    publish(client, comment='钴蓝发色沉稳。')
    register(client)
    r = client.get('/item/CO-0001')
    assert r.status_code == 200
    html = r.text
    order = ['data-gallery', '货号 CO-0001', '<h1 class="h1s"', '€185', '卖家原价', '01</span>品相', '据卖家描述：',
             '02</span>预估费用', '约 €42', '（B 档：30cm 以下花瓶）', '不含保险与进口关税', '03</span>墨墨点评',
             '钴蓝发色沉稳。', '— 墨墨', 'data-copybar', '复制货号', '浏览全部', 'class="site-foot"']
    pos = [html.index(s) for s in order]
    assert pos == sorted(pos), [s for s, p in zip(order, pos)]
    assert '<p class="said-t" lang="de">Sehr guter Zustand.\nKeine Chips.</p>' in html
    # 画廊：缩略图组（第一张 aria-current）、页码、上一张 / 下一张与放大（脚本显示）、放大层
    assert html.count('class="slide"') == 2 and 'aria-current="true"' in html and '1 / 2' in html
    assert 'data-prev hidden' in html and 'data-open-zoom hidden' in html and 'data-zoom-layer' in html
    assert 'role="dialog" aria-modal="true" aria-label="放大查看"' in html
    # 复制栏：货号是 DOM 文字；剪贴板失败时的只读输入框已备好
    assert 'id="copy-code" type="text" value="CO-0001" readonly' in html
    # 不暴露原平台、理由与系统品相等级
    assert not re.search(r'ebay|etsy|kleinanzeigen|https?://|→|一级品', html, re.I)
    assert 'style=' not in html and '<script>' not in html


def test_no_comment_means_no_section_three(client):
    publish(client)
    register(client)
    html = client.get('/item/CO-0001').text
    assert '墨墨点评' not in html and '03</span>' not in html and '02</span>预估费用' in html


def test_ask_tier_shows_please_ask(client):
    publish(client, fee={'fee_tier': 'ask', 'fee_amount': None})
    register(client)
    fee = text_between(client.get('/item/CO-0001').text, '02</span>预估费用', '</section>')
    assert '<p class="ask">请咨询</p>' in fee and '请在微信中联系墨墨' in fee and '不含保险与进口关税' in fee
    assert '€' not in fee and '¥' not in fee


@pytest.mark.parametrize('mutate,detail', [
    (lambda it: it.update(fee_tier='ask', fee_amount=40), '请咨询'),
    (lambda it: it.pop('fee_amount'), '费用金额无效'),
    (lambda it: it.update(fee_amount=42.5), '不符合要求'),
    (lambda it: (it.pop('fee_tier'), it.update(fee_amount=42)), '不符合要求'),
    (lambda it: it.update(fee_eur=42), '不符合要求'),
    (lambda it: it.update(fee_amount=10001), '不符合要求'),
    (lambda it: (it.update(fee_tier='ask'), it.pop('fee_amount')), '请咨询'),           # 请咨询还带档位名称
])
def test_bad_fee_fields_rejected(client, mutate, detail):
    items = items_with(client, n=10, imgs=1)
    mutate(items[0])
    r = send(client, 'snapshot', build(1, items))
    assert r.status_code == 400 and detail in r.json()['detail']


@pytest.mark.parametrize('extra,detail', [
    ({'fx_rate': 7.8}, '不符合要求'), ({'fx_date': '2026-11-02'}, '不符合要求'),
    ({'fx_rate': -1, 'fx_date': '2026-11-02'}, '不符合要求'), ({'fx_rate': 60, 'fx_date': '2026-11-02'}, '不符合要求'), ({'fx_rate': 7.8, 'fx_date': '2026-13-40'}, '日期无效'),
])
def test_bad_fx_rejected(client, extra, detail):
    payload = build(1, items_with(client, n=10, imgs=1))
    payload.update(extra)
    r = send(client, 'snapshot', payload)
    assert r.status_code == 400 and detail in r.json()['detail']


def test_snapshot_without_fee_or_fx_still_renders(client):
    """故事 10 的旧快照（没有档位、没有汇率）：不出现预估费用小节（不显示"请咨询"），点评顺延为 02；只显示欧元。"""
    items = items_with(client, n=10, imgs=1, comment='好')
    for it in items:
        it.pop('fee_tier'), it.pop('fee_amount'), it.pop('fee_label')
    assert send(client, 'snapshot', build(1, items)).status_code == 200
    register(client)
    html = client.get('/item/CO-0001').text
    assert '预估费用' not in html and '请咨询' not in html and '02</span>墨墨点评' in html
    assert 'data-currency' not in html and '€185' in html


def test_fee_label_from_snapshot_wins_and_fallback_matches_config():
    from pathlib import Path            # 服务器端不装 PyYAML：按档位行的固定写法读名称
    text = (Path(__file__).resolve().parents[2] / 'config' / 'fee_tiers.yaml').read_text()
    labels = dict(re.findall(r'^  ([ABCD]): \{amount: \d+, label: ([^,]+), max_cm', text, re.M))
    assert len(labels) == 4 and labels == guest.FEE_LABELS


def test_snapshot_fee_label_is_rendered(client):
    publish(client, fee={'fee_tier': 'B', 'fee_amount': 45, 'fee_label': '小花瓶'})
    register(client)
    assert '（B 档：小花瓶）' in client.get('/item/CO-0001').text


# ---------- 已售出 / 已移出与 90 天 ----------

def test_hidden_within_90_days_shows_sold_treatment(client, clock):
    publish(client)
    register(client)
    clock.now = NOW
    at = datetime.fromtimestamp(NOW - 30 * 86400, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    payload = {'schema_version': 1, 'code': 'CO-0001', 'hide_seq': 5, 'action': 'hide', 'at': at, 'reason': 'sold'}
    assert send(client, 'hide', payload).json()['applied'] is True
    r = client.get('/item/CO-0001')
    html = r.text
    assert r.status_code == 200
    assert html.index('<p class="plate">已售出</p>') < html.index('<h1 class="h1s"')
    assert '<span class="vh">已售出，原价</span><s class="mono big">' in html and '此件已售出。' in html
    assert 'class="obj sold"' in html and 'data-copybar' not in html and '复制货号' not in html
    assert '<a class="btn btn-primary" href="/">浏览全部</a>' in html
    assert '02</span>预估费用' not in html
    clock.now = NOW + 61 * 86400                                       # 隐藏已 91 天
    r = client.get('/item/CO-0001')
    assert r.status_code == 404 and '此件已不在展出' in r.text and '浏览全部' in r.text


def test_removed_but_still_in_snapshot(client):
    publish(client)
    register(client)
    payload = {'schema_version': 1, 'code': 'CO-0001', 'hide_seq': 5, 'action': 'hide',
               'at': datetime.fromtimestamp(NOW - 86400, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z'),
               'reason': 'removed'}
    assert send(client, 'hide', payload).json()['applied'] is True
    html = client.get('/item/CO-0001').text
    assert '<p class="plate">已移出</p>' in html and '<span class="vh">已移出，原价</span>' in html
    assert '已售出' not in html and '此件已移出。' in html and 'data-copybar' not in html


def test_removed_tombstone_and_expiry(client, clock):
    clock.now = NOW
    recent = datetime.fromtimestamp(NOW - 10 * 86400, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    old = datetime.fromtimestamp(NOW - 91 * 86400, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    publish(client, tombstones=[{'code': 'CO-0098', 'status': 'removed', 'at': recent},
                                {'code': 'CO-0099', 'status': 'sold', 'at': old}])
    register(client)
    html = client.get('/item/CO-0098').text
    assert '<p class="plate inline">已移出</p>' in html and '此件已移出。' in html and '复制货号' not in html
    r = client.get('/item/CO-0099')
    assert r.status_code == 404 and '此件已不在展出' in r.text


# ---------- 币种 ----------

def test_cny_display_converts_price_and_fee(client):
    publish(client, rate=7.80)
    register(client)
    html = client.get('/item/CO-0001').text
    assert 'role="radiogroup" aria-label="欧元 / 人民币"' in html
    assert 'value="EUR" role="radio" aria-label="欧元" aria-checked="true"' in html     # 默认欧元
    assert 'data-cny-note hidden' in html
    r = choose(client, 'CNY')
    assert r.status_code == 200 and r.json() == {'ok': True, 'currency': 'CNY'}
    html = client.get('/item/CO-0001').text
    assert 'value="CNY" role="radio" aria-label="人民币" aria-checked="true"' in html
    price = text_between(html, '<p class="price">', '</p>')
    assert '≈ ¥1,443' in price and '约 1443 元人民币' in price and 'data-eur="185.0"' in price
    fee = text_between(html, '02</span>预估费用', '</section>')
    assert '≈ ¥328' in fee and '约 328 元人民币' in fee and '（B 档：30cm 以下花瓶）' in fee     # 42 × 7.8 = 327.6
    assert '<p class="fxnote" data-cny-note>按当日汇率，以欧元为准</p>' in html
    home = client.get('/').text
    assert '≈ ¥1,443' in home and '约 1443 元人民币' in home and '按当日汇率，以欧元为准' in home
    assert 'aria-label="第 1 名，Meissen梅森 花瓶 1，约 1443 元人民币"' in home
    more = client.get('/api/pool?offset=0').json()['html']
    assert '≈ ¥' in more and '€' not in more


def test_currency_saved_on_account_and_follows_to_other_device(client, db, clock):
    publish(client)
    register(client)
    with sqlite3.connect(db) as c:
        rev = c.execute('SELECT account_rev, currency FROM accounts').fetchone()
    assert rev[1] == 'EUR'
    assert choose(client, 'CNY', js=False).status_code == 303          # 不跑脚本：表单提交，回到原页面
    with sqlite3.connect(db) as c:
        after = c.execute('SELECT account_rev, currency FROM accounts').fetchone()
    assert after == (rev[0] + 1, 'CNY')
    assert choose(client, 'CNY').json()['ok'] is True                  # 没变：不递增 account_rev
    with sqlite3.connect(db) as c:
        assert c.execute('SELECT account_rev FROM accounts').fetchone()[0] == rev[0] + 1
    other = make_client(db, clock)                                     # 换一台设备登录
    login(other)
    assert '≈ ¥1,443' in other.get('/item/CO-0001').text
    assert client.get('/item/CO-0001').text.count('≈ ¥1,443') >= 1    # 刷新后仍为 ¥


def test_stale_rate_hides_switch_and_shows_euro(client):
    publish(client, rate=7.80, date=day(8))                            # 汇率日期 8 天前
    register(client)
    choose(client, 'CNY')
    html = client.get('/item/CO-0001').text
    assert 'data-currency' not in html and '¥' not in html and '€185' in html
    assert '汇率暂不可用，暂以欧元显示' in html
    assert '¥' not in client.get('/').text.replace('汇率暂不可用', '')


def test_rate_seven_days_old_still_usable(client):
    publish(client, rate=7.80, date=day(7))
    register(client)
    assert 'data-currency' in client.get('/').text


def test_future_rate_date_is_not_used(client):
    publish(client, rate=7.80, date=day(-2))
    register(client)
    assert 'data-currency' not in client.get('/').text


def test_no_js_switch_keeps_home_filters(client):
    publish(client)
    register(client)
    html = client.get('/?form=FN-044').text
    assert '<input type="hidden" name="next" value="/?form=FN-044">' in html
    r = choose(client, 'CNY', js=False, next_path='/?form=FN-044')
    assert r.status_code == 303 and r.headers['location'] == '/?form=FN-044'
    for bad in ('/?evil=1', '//evil.test/?form=1', '/?form=a b'):
        assert choose(client, 'EUR', js=False, next_path=bad).headers['location'] == '/'


def test_missing_rate_hides_switch(client):
    publish(client, rate=None)
    register(client)
    html = client.get('/').text
    assert 'data-currency' not in html and '汇率暂不可用' not in html     # 选的是欧元：不提示


def test_currency_endpoint_guards(client):
    publish(client)
    assert choose(client, 'CNY').status_code == 401                     # 未登录（脚本）
    r = choose(client, 'CNY', js=False)
    assert r.status_code == 303 and r.headers['location'].startswith('/login?next=')
    register(client)
    assert choose(client, 'USD').status_code == 400
    assert client.post('/currency', data={'currency': 'CNY', 'next': '/'},
                       headers={'Origin': 'https://evil.test'}).status_code == 403
    r = client.post('/currency', data={'currency': 'CNY', 'next': '/'},
                    headers={'Origin': 'https://evil.test', 'Accept': 'application/json'})
    assert r.status_code == 403 and r.json()['ok'] is False
    r = choose(client, 'CNY', js=False, next_path='https://evil.test/')
    assert r.status_code == 303 and r.headers['location'] == '/'          # 不做开放跳转


def test_money_formatting():
    cur = {'code': 'CNY', 'rate': 7.8, 'available': True}
    assert guest.cny(185, 7.8) == 1443 and guest.cny(0.5, 1) == 1
    assert '≈ ¥1,443' in guest.money(185, cur) and guest.money_text(185, cur) == '约 1443 元人民币'
    assert guest.money_text(42, None, approx=True) == '约 €42' and guest.money_text(99.5) == '€99.50'
    assert guest.money(1250).endswith('>€1,250</span>')
    assert guest.fx_usable(7.8, day(7), NOW) and not guest.fx_usable(7.8, day(8), NOW)
    assert guest.fx_usable(7.8, day(0), NOW) and not guest.fx_usable(7.8, day(-1), NOW)
    assert not guest.fx_usable(None, day(1), NOW) and not guest.fx_usable(7.8, 'bad', NOW)


def test_static_script_has_copy_and_currency_wording(client):
    js = client.get('/static/guest.js').text
    for text in ('已复制 ', '，回到微信粘贴到转账备注', '未能自动复制，请长按选择货号 ', '价格已切换为人民币，约数，以欧元为准',
                 'Accept: \'application/json\'', 'data-local', 'window.curioMoney'):
        assert text in js, text
    css = client.get('/static/guest.css').text
    rules = re.findall(r'([^{}]+)\{[^}]*touch-action:none', css)
    assert [r.strip() for r in rules] == ['.zl-stage']                              # 只在放大层内
    assert 'grayscale(1)' in css and 'opacity:.45' in css
