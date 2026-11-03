"""故事 12（CAP-10、CAP-11）：预估费用档位推断、手动改档、ECB 汇率与快照字段——Mac 一侧。

覆盖 I/O 矩阵：杯碟 → A、花瓶 28cm → B、42cm → C、人物瓷偶 25cm 上跳 → C、读不出 → 请咨询、超 1 米 → 请咨询、
手动改档（快照为手动值，恢复自动后回推断值）、ECB 失败沿用上次值与原日期（记一次警告）、每天最多成功取一次。
ECB 请求一律注入假函数（tests/conftest.py 默认拦截真实请求），不连接任何外部主机。
"""
import json
import logging
import re
import shutil
import threading
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

import pytest
import requests
import yaml

from curio import fx, judge, publish, ranking, rules, sync, triage
from curio.config import fee_tiers, load
from curio.schedule import BERLIN
from curio.store import Store, fee_info

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def fees():
    return fee_tiers()


def tier(title, description='', std=None, fees=None):
    return judge.fee_tier(title, description, std, fees or fee_tiers())


# ---------- 档位推断（judge.fee_tier，规则归口） ----------

@pytest.mark.parametrize('title,description,want,reason', [
    ('Meissen Kaffeetasse mit Untertasse', '', 'A', '杯碟类，未写尺寸 → A'),
    ('Meissen Vase H 28 cm', '', 'B', '高 28 cm → B'),
    ('Meissen Vase Zwiebelmuster', 'Höhe 42cm, sehr guter Zustand', 'C', '高 42 cm → C'),
    ('Meissen Figur Gärtnerin 25 cm', '', 'C', '人物瓷偶，尺寸 25 cm → B，难包装上跳一档 → C'),
    ('Meissen Deckeldose Blumenmalerei', 'Schöne Dose, keine Chips.', 'ask', '读不出尺寸 → 请咨询'),
    ('Meissen Bodenvase', 'Höhe 120 cm', 'ask', '高 120 cm，超过 100 cm → 请咨询'),
])
def test_io_matrix(title, description, want, reason):
    got = tier(title, description)
    assert got['tier'] == want and got['reason'] == reason
    amounts = {'A': 30, 'B': 42, 'C': 67, 'D': 140, 'ask': None}
    assert got['amount'] == amounts[want]


@pytest.mark.parametrize('title,description,want,size', [
    ('Meissen Vase 280 mm', '', 'B', 28),                       # 毫米换算
    ('Meissen Vase', 'Höhe: 28,5', 'B', 28.5),                  # 标签后没写单位按厘米、逗号小数
    ('Meissen Teller Ø 24 cm', '', 'A', 24),                    # 杯碟类写了尺寸、不超过 A 上限仍为 A
    ('Meissen Prunkteller', 'Durchmesser 34 cm', 'C', 34),      # 杯碟类超出 A 上限按尺寸
    ('Meissen Platte 40 x 30 x 5 cm', '', 'C', 40),             # 多维取最大
    ('Meissen Vase H 11"', '', 'B', 27.94),                     # 英寸（带高度标签）
    ('Meissen Vase 11 x 6"', '', 'B', 27.94),                   # 英寸（多维）
    ('Meissen Vase Modell "Nr. 30"', '', 'ask', None),          # 型号里的引号不是英寸
    ('Meissen Vase 11 inches', '', 'B', 27.94),
    ('Meissen Vase', 'Höhe 18 cm, Durchmesser 9 cm', 'B', 18),  # 取最大的高度/直径类数值
    ('Meissen Vase', 'Versand im Karton 60 x 40 x 40 cm, Höhe 20 cm', 'B', 20),   # 包装尺寸不算
    ('Meissen Bodenvase H 75 cm', '', 'D', 75),
    ('Meissen Vase H 100 cm', '', 'D', 100),                    # 1 米整仍是 D
    ('Meissen Vase', 'Höhe 1,20 m', 'ask', None),               # 以米写的读不出 → 请咨询
    ('Meissen Figur 70 cm', '', 'ask', 70),                     # D 再上跳 → 请咨询
    ('Meissen Figur', '', 'ask', None),                         # 人物瓷偶读不出尺寸
    ('Meissen Spitzenfigur Tänzerin 20 cm', '', 'ask', None),   # 特别易碎
    ('Meissen Tasse 28 cm hoch', '', 'A', 28),
    ('Meissen Vase 30 cm', '', 'B', 30),                        # 30cm 以下（含）为 B
    ('Meissen Vase 30,5 cm', '', 'C', 30.5),
])
def test_size_reading_and_tiers(title, description, want, size):
    got = tier(title, description)
    assert got['tier'] == want, got
    if size is not None:
        assert got['size_cm'] == pytest.approx(size, abs=0.01)


@pytest.mark.parametrize('title,want', [
    ('Meissen Tasse mit Watteau-Figuren', 'A'),                  # 杯在前：人物是装饰
    ('Meissen Teller mit Figuren Ø 24 cm', 'A'),
    ('Meissen Figur Gärtnerin mit Teller 20 cm', 'C'),           # 人物在前：货本身是瓷偶
    ('Meissen hiccups Vase', 'ask'),                             # 英语词要求词首
    ('Meissen templates Vase', 'ask'),
])
def test_decoration_words_do_not_override_main_form(title, want):
    assert tier(title)['tier'] == want


def test_cup_function_id_beats_figure_words():
    cup = {'function_id': 'FN-001', 'std_name': '梅森 咖啡杯碟', 'segments': '{}'}
    assert tier('Meissen Kaffeetasse mit Figurenmalerei', '', cup)['tier'] == 'A'


def test_chinese_standard_name_alone_decides():
    fig = {'function_id': 'FN-999', 'std_name': '梅森 人物瓷偶', 'segments': '{}'}
    assert tier('Meissen Schäfer 25 cm', '', fig)['tier'] == 'C'
    cup = {'function_id': 'FN-999', 'std_name': '梅森 蓝洋葱 茶杯', 'segments': '{}'}
    assert tier('Meissen Zwiebelmuster', '', cup)['tier'] == 'A'


def test_chinese_words_come_from_config(tmp_path):
    shutil.copytree(ROOT / 'config', tmp_path / 'config')
    path = tmp_path / 'config' / 'fee_tiers.yaml'
    data = yaml.safe_load(path.read_text())
    data['hard_to_pack']['zh_words'] = ['摆件']
    path.write_text(yaml.safe_dump(data, allow_unicode=True))
    std = {'function_id': 'FN-999', 'std_name': '梅森 人物瓷偶', 'segments': '{}'}
    assert tier('Meissen Schäfer 25 cm', '', std, fee_tiers(tmp_path))['tier'] == 'B'
    std = {'function_id': 'FN-999', 'std_name': '梅森 摆件', 'segments': '{}'}
    assert tier('Meissen Schäfer 25 cm', '', std, fee_tiers(tmp_path))['tier'] == 'C'


def test_std_name_form_decides_category():
    figure = {'function_id': 'FN-052', 'std_name': '梅森 塑像', 'segments': json.dumps({'function': '塑像'})}
    assert tier('Meissen Schäfer 25 cm', '', figure)['tier'] == 'C'
    cup = {'function_id': 'FN-003', 'std_name': '梅森 茶杯碟', 'segments': '{}'}
    assert tier('Meissen Zwiebelmuster', '', cup)['tier'] == 'A'
    vase = {'function_id': 'FN-044', 'std_name': '梅森 花瓶', 'segments': '{}'}
    assert tier('Meissen Zwiebelmuster', '', vase)['tier'] == 'ask'


def test_rules_entry_point_reads_item_fields(fees):
    got = rules.fee_tier({'title': 'Meissen Vase', 'description': 'Höhe 42 cm'}, None, fees)
    assert got['tier'] == 'C' and got['label'] == '30–50cm'


def test_fee_config_is_data_and_validated(tmp_path):
    shutil.copytree(ROOT / 'config', tmp_path / 'config')
    assert load(tmp_path)['fee_tiers']['tiers']['B'] == {'amount': 42, 'label': '30cm 以下花瓶', 'max_cm': 30.0}
    path = tmp_path / 'config' / 'fee_tiers.yaml'
    good = yaml.safe_load(path.read_text())
    for bad in ({'tiers': {k: v for k, v in good['tiers'].items() if k != 'D'}},
                {'tiers': dict(good['tiers'], B=dict(good['tiers']['B'], amount=42.5))},
                {'tiers': dict(good['tiers'], C=dict(good['tiers']['C'], max_cm=20))},
                {'cups': {'words': ['(unclosed']}},
                {'fragile': {'function_ids': ['FN-052']}},
                {'cups': {'zh_words': ['(?<!']}}):
        path.write_text(yaml.safe_dump(dict(good, **bad), allow_unicode=True))
        with pytest.raises(ValueError):
            fee_tiers(tmp_path)
    # 金额改了即生效（数据，不改代码）
    path.write_text(yaml.safe_dump(dict(good, tiers=dict(good['tiers'], B=dict(good['tiers']['B'], amount=45))),
                                   allow_unicode=True))
    assert tier('Meissen Vase H 28 cm', fees=fee_tiers(tmp_path))['amount'] == 45


# ---------- 手动改档、快照字段与"有未发布的修改" ----------

@pytest.fixture
def cfg():
    return dict(load(), keywords=['Meissen'])


@pytest.fixture
def db(tmp_path):
    return Store(tmp_path / 'data' / 'curio.db')


def add(db, cfg, n, title=None, description='Sehr guter Zustand.'):
    """留下一件没有图片的货（不需转码）；返回 (item_id, 货号)。"""
    iid = 'it%03d' % n
    db.ingest(dict(item_id=iid, platform='ebay', title=title or f'Meissen Vase Nr {n} H 28 cm', price=100 + n,
                   currency='EUR', buying='FIXED_PRICE', url=f'https://www.ebay.de/itm/{100000000000 + n}',
                   image='', description=description), cfg)
    with db.connect() as c:
        triage.keep(c, iid)
        return iid, triage.code(c, iid)


def ten(db, cfg):
    out = [add(db, cfg, n) for n in range(1, 11)]
    with db.connect() as c:
        publish.set_rank(c, [code for _, code in out])
    return out


def payload_of(db, code):
    snap, _ = publish.candidate(db, ROOT)
    return next(it for it in snap['items'] if it['code'] == code), snap


def test_manual_tier_wins_and_restore_auto(db, cfg):
    (iid, code), *_ = ten(db, cfg)
    it, _ = payload_of(db, code)
    assert (it['fee_tier'], it['fee_amount']) == ('B', 42)
    r = db.save_fee(iid, 'C', 'fee-1')
    assert r['fee']['tier'] == 'C' and r['fee']['manual'] and r['fee']['auto']['tier'] == 'B'
    assert db.save_fee(iid, 'C', 'fee-1') == r                       # 同一 request_id 重放返回首次结果
    with pytest.raises(ValueError):
        db.save_fee(iid, 'D', 'fee-1')                                # 同一编号、内容不同
    it, _ = payload_of(db, code)
    assert (it['fee_tier'], it['fee_amount']) == ('C', 67)
    detail = db.item_detail(iid)
    assert detail['fee']['tier'] == 'C' and detail['fee']['auto']['reason'] == '高 28 cm → B'
    assert [c['tier'] for c in detail['fee_choices']] == ['A', 'B', 'C', 'D', 'ask']
    db.save_fee(iid, 'ask', 'fee-2')
    it, _ = payload_of(db, code)
    assert it['fee_tier'] == 'ask' and 'fee_amount' not in it and 'fee_label' not in it
    db.save_fee(iid, None, 'fee-3')                                   # 恢复自动
    it, _ = payload_of(db, code)
    assert (it['fee_tier'], it['fee_amount']) == ('B', 42)
    assert db.item_detail(iid)['fee']['manual'] is False


def test_choosing_inferred_tier_clears_override_and_is_audited(db, cfg):
    iid, _ = add(db, cfg, 1)
    db.save_fee(iid, 'C', 'a1')
    r = db.save_fee(iid, 'B', 'a2')                                   # 选回推断值 = 恢复自动
    assert r['fee']['manual'] is False and r['fee']['tier'] == 'B'
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM fee_overrides').fetchone()[0] == 0
        events = c.execute("SELECT actor, action, reason FROM triage_events WHERE action='fee' ORDER BY id").fetchall()
        assert [tuple(e) for e in events] == [('human', 'fee', 'B→C（手动）'), ('human', 'fee', 'C→B（自动）')]
    db.save_fee(iid, 'B', 'a3')                                       # 没变化：不再记事件
    with db.connect() as c:
        assert c.execute("SELECT count(*) FROM triage_events WHERE action='fee'").fetchone()[0] == 2
        triage.undo(c)                                                # 撤销不受改档事件影响：撤的是"留下"
        assert triage.state(c, iid) == 'new'


def test_fee_event_does_not_freeze_system_placement(db, cfg):
    db.ingest(dict(item_id='n1', platform='ebay', title='Meissen Vase H 28 cm', price=100, currency='EUR',
                   buying='FIXED_PRICE', url='', image='', description=''), cfg)
    db.save_fee('n1', 'D', 'z1')
    with db.connect() as c:
        assert triage.has_human(c, 'n1') is False


def test_save_fee_rejects_bad_input(db, cfg):
    iid, _ = add(db, cfg, 1)
    for args in ((iid, 'E', 'x1'), (iid, 'b', 'x2'), ('nope', 'A', 'x3'), (iid, 'A', ''), ('', 'A', 'x4')):
        with pytest.raises(ValueError):
            db.save_fee(*args)


def test_reason_never_in_snapshot(db, cfg):
    ten(db, cfg)
    snap, _ = publish.candidate(db, ROOT)
    text = json.dumps(snap, ensure_ascii=False)
    assert '→' not in text and 'reason' not in text and '高 28 cm' not in text


def test_fee_change_marks_unpublished_changes(db, cfg):
    (iid, code), *_ = ten(db, cfg)
    with db.connect() as c:
        before = publish.status_fingerprint(c)
    db.save_fee(iid, 'D', 'f1')
    with db.connect() as c:
        assert publish.status_fingerprint(c) != before
    db.save_fee(iid, None, 'f2')
    with db.connect() as c:
        assert publish.status_fingerprint(c) == before                # 改回原样即无修改


def test_fee_amount_change_in_yaml_marks_unpublished(db, cfg, tmp_path):
    ten(db, cfg)
    shutil.copytree(ROOT / 'config', tmp_path / 'cfgroot' / 'config')
    fees = fee_tiers(tmp_path / 'cfgroot')
    with db.connect() as c:
        before = publish.status_fingerprint(c, fees)
    path = tmp_path / 'cfgroot' / 'config' / 'fee_tiers.yaml'
    data = yaml.safe_load(path.read_text())
    data['tiers']['B']['amount'] = 45
    path.write_text(yaml.safe_dump(data, allow_unicode=True))
    with db.connect() as c:
        assert publish.status_fingerprint(c, fee_tiers(tmp_path / 'cfgroot')) != before
    data['tiers']['B'] = dict(data['tiers']['B'], amount=42, label='小花瓶')
    path.write_text(yaml.safe_dump(data, allow_unicode=True))
    with db.connect() as c:
        assert publish.status_fingerprint(c, fee_tiers(tmp_path / 'cfgroot')) != before


def test_root_config_is_used_by_publish(db, cfg, tmp_path):
    (iid, code), *_ = ten(db, cfg)
    root = tmp_path / 'cfgroot'
    shutil.copytree(ROOT / 'config', root / 'config')
    path = root / 'config' / 'fee_tiers.yaml'
    data = yaml.safe_load(path.read_text())
    data['tiers']['B'] = dict(data['tiers']['B'], amount=45, label='小花瓶')
    path.write_text(yaml.safe_dump(data, allow_unicode=True))
    snap, _ = publish.candidate(db, root)
    it = next(i for i in snap['items'] if i['code'] == code)
    assert (it['fee_amount'], it['fee_label']) == (45, '小花瓶')
    assert db.item_detail(iid, root)['fee']['amount'] == 45


@pytest.mark.parametrize('mutate', [
    lambda p: p['items'][0].update(fee_tier='ask'),                           # 请咨询带金额
    lambda p: p['items'][0].pop('fee_amount'),                                 # A–D 缺金额
    lambda p: p['items'][0].update(fee_amount=10001),
    lambda p: p['items'][0].update(fee_amount=-1),
    lambda p: (p['items'][0].pop('fee_tier'),),                                # 有金额没档位
    lambda p: p.update(fx_rate=7.8),                                           # 汇率缺日期
    lambda p: p.update(fx_rate=60, fx_date='2026-11-02'),                      # 汇率越界
])
def test_mac_and_server_agree_on_fee_and_fx_rules(db, cfg, mutate):
    """同一份坏快照：Mac（Schema 校验 + publish.check_fees）与服务器（jsonschema + snapshot.check_snapshot）都拒收。"""
    import subprocess, sys
    ten(db, cfg)
    snap, _ = publish.candidate(db, ROOT)
    publish.check_fees(snap)
    assert not sync.validate(snap, json.loads((ROOT / 'contract/schemas/snapshot.v1.schema.json').read_text()))
    mutate(snap)
    mac_errors = sync.validate(snap, json.loads((ROOT / 'contract/schemas/snapshot.v1.schema.json').read_text()))
    try:
        publish.check_fees(snap)
        mac_rejects = bool(mac_errors)
    except publish.PublishError:
        mac_rejects = True
    assert mac_rejects
    py = ROOT / 'server' / '.venv' / 'bin' / 'python'
    if not py.exists():
        pytest.skip('需要 server/.venv')
    code = ('import json,sys;from jsonschema import Draft202012Validator;import snapshot;'
            'p=json.load(sys.stdin);s=json.load(open("../contract/schemas/snapshot.v1.schema.json"));'
            'e=list(Draft202012Validator(s).iter_errors(p))\n'
            'try:\n snapshot.check_snapshot(p) if not e else None\nexcept snapshot.BadSnapshot:e=[1]\n'
            'print("reject" if e else "accept")')
    snap['publish_no'] = snap['hide_watermark'] = 1
    r = subprocess.run([str(py), '-c', code], cwd=ROOT / 'server', input=json.dumps(snap), capture_output=True,
                       text=True, timeout=60)
    assert r.stdout.strip() == 'reject', r.stderr


def test_published_snapshot_carries_fee_and_fx(db, cfg):
    (iid, code), *_ = ten(db, cfg)
    db.save_fee(iid, 'A', 'f1')
    fx.refresh(db, clock=lambda: 1_800_000_000, get=fake_ecb('2026-11-02', '7.8'))
    result = publish.publish(db, ROOT)
    with db.connect() as c:
        row = c.execute("SELECT payload FROM outbox WHERE kind='snapshot'").fetchone()
        status = ranking.publish_status(c)
    snap = json.loads(row[0])
    assert snap['publish_no'] == result['publish_no']
    assert (snap['fx_rate'], snap['fx_date']) == (7.8, '2026-11-02')
    first = next(it for it in snap['items'] if it['code'] == code)
    assert (first['fee_tier'], first['fee_amount'], first['fee_label']) == ('A', 30, '普通杯碟')
    assert all(it['fee_tier'] == 'B' and it['fee_amount'] == 42 and it['fee_label'] == '30cm 以下花瓶'
               for it in snap['items'] if it['code'] != code)
    sync.validate(snap, json.loads((ROOT / 'contract/schemas/snapshot.v1.schema.json').read_text()))
    assert status['dirty'] is False
    db.save_fee(iid, 'B', 'f2')                                       # 发布后改档 → 有未发布的修改
    with db.connect() as c:
        assert ranking.publish_status(c)['dirty'] is True


def test_snapshot_without_any_rate_has_no_fx_fields(db, cfg):
    ten(db, cfg)
    snap, _ = publish.candidate(db, ROOT)
    assert 'fx_rate' not in snap and 'fx_date' not in snap


# ---------- ECB 汇率（AD-14） ----------

class Resp:
    def __init__(self, text, status=200):
        self.text, self.status_code = text, status


def fake_ecb(date, value, calls=None):
    def get(url, params, timeout):
        if calls is not None:
            calls.append((url, dict(params), timeout))
        return Resp('KEY,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE\n'
                    f'EXR.D.CNY.EUR.SP00.A,D,CNY,EUR,SP00,A,{date},{value}\n')
    return get


def at(day, hour=12):
    return datetime(2026, 11, day, hour, tzinfo=BERLIN).timestamp()


def test_ecb_fetch_once_per_day(db):
    calls = []
    get = fake_ecb('2026-11-02', '7.8123', calls)
    r = fx.refresh(db, clock=lambda: at(3, 9), get=get)
    assert r == {'fx_status': 'ok', 'fx_rate': 7.8123, 'fx_date': '2026-11-02'}
    url, params, timeout = calls[0]
    assert url.endswith('/EXR/D.CNY.EUR.SP00.A') and params['lastNObservations'] == '1' and timeout
    assert fx.refresh(db, clock=lambda: at(3, 15), get=get)['fx_status'] == 'fresh'
    assert len(calls) == 1                                              # 同一天不再请求
    assert fx.refresh(db, clock=lambda: at(4, 9), get=fake_ecb('2026-11-03', '7.9', calls))['fx_rate'] == 7.9
    with db.connect() as c:
        assert fx.current(c) == (7.9, '2026-11-03')


def test_ecb_failure_keeps_last_value_and_date(db, caplog):
    fx.refresh(db, clock=lambda: at(2), get=fake_ecb('2026-11-01', '7.8'))

    def timeout(*a, **k):
        raise requests.Timeout('slow')
    with caplog.at_level(logging.WARNING, logger='curio.fx'):
        r = fx.refresh(db, clock=lambda: at(3, 9), get=timeout)
    assert r == {'fx_status': 'error', 'fx_rate': 7.8, 'fx_date': '2026-11-01'}
    assert len([x for x in caplog.records if x.levelno == logging.WARNING]) == 1
    calls = []
    assert fx.refresh(db, clock=lambda: at(3, 9) + 600, get=fake_ecb('2026-11-02', '7.85', calls))['fx_status'] == 'wait'
    assert calls == []                                                   # 失败后 1 小时内不重试
    r = fx.refresh(db, clock=lambda: at(3, 11), get=fake_ecb('2026-11-02', '7.85', calls))
    assert r['fx_status'] == 'ok' and len(calls) == 1 and r['fx_date'] == '2026-11-02'


def test_late_fetch_after_ecb_publishes(db):
    calls = []
    fx.refresh(db, clock=lambda: at(3, 9), get=fake_ecb('2026-11-02', '7.8', calls))          # 周二早上：取到周一的
    assert fx.refresh(db, clock=lambda: at(3, 15), get=fake_ecb('2026-11-03', '7.9', calls))['fx_status'] == 'fresh'
    r = fx.refresh(db, clock=lambda: at(3, 17), get=fake_ecb('2026-11-03', '7.9', calls))      # 16:30 后再取一次
    assert r['fx_status'] == 'ok' and r['fx_date'] == '2026-11-03' and len(calls) == 2
    assert fx.refresh(db, clock=lambda: at(3, 18), get=fake_ecb('2026-11-03', '7.9', calls))['fx_status'] == 'fresh'
    assert len(calls) == 2


def test_late_fetch_only_once_and_not_on_weekends(db):
    calls = []
    fx.refresh(db, clock=lambda: at(3, 9), get=fake_ecb('2026-11-02', '7.8', calls))
    fx.refresh(db, clock=lambda: at(3, 17), get=fake_ecb('2026-11-02', '7.8', calls))          # ECB 还没出：仍是昨天
    assert fx.refresh(db, clock=lambda: at(3, 19), get=fake_ecb('2026-11-03', '7.9', calls))['fx_status'] == 'fresh'
    assert len(calls) == 2
    fx.refresh(db, clock=lambda: at(7, 9), get=fake_ecb('2026-11-06', '7.8', calls))           # 周六
    assert fx.refresh(db, clock=lambda: at(7, 17), get=fake_ecb('2026-11-06', '7.8', calls))['fx_status'] == 'fresh'
    assert len(calls) == 3


@pytest.mark.parametrize('resp', [Resp('', 500), Resp('TIME_PERIOD,OBS_VALUE\n2026-11-02,abc\n'),
                                  Resp('TIME_PERIOD,OBS_VALUE\n2026-11-02,0.01\n'), Resp('<html>oops</html>')])
def test_bad_ecb_answers_never_overwrite(db, resp):
    fx.refresh(db, clock=lambda: at(2), get=fake_ecb('2026-11-01', '7.8'))
    r = fx.refresh(db, clock=lambda: at(3), get=lambda *a: resp)
    assert r['fx_status'] == 'error' and (r['fx_rate'], r['fx_date']) == (7.8, '2026-11-01')


def test_no_rate_ever(db):
    r = fx.refresh(db, clock=lambda: at(3), get=lambda *a: Resp('', 503))
    assert r == {'fx_status': 'error'}
    with db.connect() as c:
        assert fx.current(c) is None


def test_default_getter_is_blocked_in_tests(db):
    """conftest 拦截真实 ECB 请求：采集轮里的 fx.run 只记 error，不连外网。"""
    assert fx.run(db, clock=lambda: at(3)) == {'fx_status': 'error'}


def test_parse_picks_latest_row():
    text = 'TIME_PERIOD,OBS_VALUE\n2026-10-30,7.70\n2026-11-02,7.81\n2026-10-31,7.75\n'
    assert fx.parse(text) == (7.81, '2026-11-02')


# ---------- 后台 03 费用档位接口 ----------

@pytest.fixture
def http(db, tmp_path):
    from curio.review import server
    httpd = server(db, port=0, root=tmp_path, fetch=lambda url: b'', flush=lambda: None)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = 'http://127.0.0.1:' + str(httpd.server_port)
    page = urllib.request.urlopen(base + '/').read().decode()
    token = re.search("const token='([^']+)'", page)[1]

    def post(path, body, tok=None):
        req = urllib.request.Request(base + path, json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json', 'X-Curio-Token': tok or token})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def get(path):
        return json.load(urllib.request.urlopen(base + path))
    yield dict(page=page, post=post, get=get)
    httpd.shutdown()
    httpd.server_close()


def test_backoffice_fee_section_and_endpoint(db, cfg, http):
    iid, code = add(db, cfg, 1)
    page = http['page']
    assert '03</span>费用档位' in page and 'id="dFeeOpts"' in page and 'role="radiogroup"' in page and '恢复自动' in page
    d = http['get']('/api/triage/item?id=' + iid)
    assert d['fee']['tier'] == 'B' and d['fee']['auto']['reason'] == '高 28 cm → B'
    status, r = http['post']('/api/triage/fee', {'item_id': iid, 'tier': 'C', 'request_id': 'h1'})
    assert status == 200 and r['fee']['tier'] == 'C' and r['fee']['manual'] is True
    assert http['get']('/api/triage/item?id=' + iid)['fee']['tier'] == 'C'
    status, r = http['post']('/api/triage/fee', {'item_id': iid, 'tier': None, 'request_id': 'h2'})
    assert status == 200 and r['fee']['tier'] == 'B' and r['fee']['manual'] is False
    assert http['post']('/api/triage/fee', {'item_id': iid, 'tier': 'Z', 'request_id': 'h3'})[0] == 400
    assert http['post']('/api/triage/fee', {'item_id': iid, 'tier': 'A', 'request_id': 'h4'}, tok='wrong')[0] == 403


def test_fee_info_helper_matches_rules(db, cfg):
    iid, _ = add(db, cfg, 1, title='Meissen Kaffeetasse mit Untertasse', description='')
    with db.connect() as c:
        item = c.execute('SELECT * FROM items WHERE item_id=?', (iid,)).fetchone()
        info = fee_info(c, item)
    assert info['tier'] == 'A' and info['amount'] == 30 and info['manual'] is False
