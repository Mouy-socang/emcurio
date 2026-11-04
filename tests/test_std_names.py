"""CAP-16 标准名称与价格库：用 tmp_path 下的小型假知识库，不依赖真实知识库。"""
import hashlib
import logging
import os
import sqlite3
import pytest
from curio import judge, triage
from curio.config import load
from curio.store import Store

B = '06 Brands and Manufactories 品牌与窑厂'
M = B + '/Meissen'


def write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('﻿' + text, encoding='utf-8')


def make_kb(root):
    for brand in ('Meissen', 'KPM Berlin', 'Herend'):
        (root / B / brand).mkdir(parents=True, exist_ok=True)
    write(root, M + '/4 Patterns 图案/Meissen_图案主题数据.csv',
          '图案ID,中文名称,外文或通用名称,图案编号,档案文件,图片状态\n'
          'MP-001,蓝洋葱,Zwiebelmuster,800101,x.md,\n'
          'MP-050,蓝花,Blaue Blume,,x.md,\n'
          'MP-031,麒麟飞鹭,Ch\'i-lin-Dekor（别名 Grüner Löwe、Fliegender Hund）,,x.md,\n'
          'MP-090,宽前缀,Breitmuster,3xxxxx,x.md,\n'
          'MP-018,御龙,Hofdrache,32xxxx,x.md,\n')
    write(root, M + '/4 Patterns 图案/Meissen_官方图案目录数据.csv',
          '图案编号,名称（德文）,原目录文字,金绘（德文）,金绘（中文）,官方分类,原目录页码,档案路径\n'
          '010110,Gestreute Blümchen,"Gestreute Blümchen, bunt, Goldrand",Goldrand,金边,1.1,10,a/010110 Gestreute Blümchen 散花.md\n'
          '010199,Gestreute Blümchen,"Gestreute Blümchen, bunt, Goldbronze",Goldbronze,重鎏金,1.1,10,a/010199 Gestreute Blümchen 重鎏金散花.md\n'
          '800101,Zwiebelmuster,"Zwiebelmuster, blau",,,5.1,60,a/800101 Zwiebelmuster 蓝洋葱.md\n'
          '010111,Gestreute Blümchen,"Gestreute Blümchen, bunt",-,,1.1,10,a/010111 Gestreute Blümchen.md\n'
          '010112,Gestreute Blümchen,"Gestreute Blümchen, bunt",/,,1.1,10,a/010112 Gestreute Blümchen.md\n'
          '321010,Drache rot,"Drache rot, Goldrand",Goldrand,金边,3.1,30,a/321010 Drache rot 红龙.md\n')
    write(root, M + '/3 Forms and Relief 器型与浮雕/Forms 器型/Meissen_器型索引.csv',
          '器型ID,德文或官方名称,中文名称,分类,资料状态,子文件夹\n'
          'MS-002,Neuer Ausschnitt,新剪裁器型,经典器型,,\n'
          'MS-005,Schwanenservice,天鹅系列器型,经典器型,,\n')
    write(root, '05 General Knowledge 通用知识/买手培训/功能词表.csv',
          '功能ID,中文名称,德文,英文,器物大类,别名与卖家用词,边界说明,常用尺寸或容量,Meissen 实例,来源,状态\n'
          'FN-001,咖啡杯碟,Kaffeetasse m. U.,Coffee cup and saucer,,Kaffeegedeck／Gedeck 指杯（卖家用词 C；Moyu 同意（第 5 条））,,,,,已确认\n'
          'FN-009,盘,Teller,Plate,,Dessertteller（甜点盘）,,,,,已确认\n'
          'FN-016,椭圆大盘,"Platte, oval","Platter, oval",,,,,,,已确认\n'
          'FN-026,带盖汤碗,Terrine,Tureen,,,,,,,已确认\n'
          'FN-044,花瓶,Vase,Vase,,Trichtervase（卖家用词，C）,,,,,已确认\n'
          'FN-055,瓷板,Bildplatte,Plaque,,注意 Porzellanplatte 多指大盘,,,,,已确认\n'
          'FN-074,篮,Korb,Basket,,2026-10-24 补：Meissen 件馆藏著录,,,,,已确认\n'
          'FN-059,鎏金盘,Prunkteller,Display plate,,Prunkschale 另立 FN-064 鎏金碗,,,,,已确认\n')
    return root


@pytest.fixture
def kb(tmp_path):
    return make_kb(tmp_path / 'EM 西洋瓷器')


@pytest.fixture
def cfg(kb):
    return dict(load(), em_kb_path=str(kb))


def tree(root):
    out = {}
    for dirpath, dirs, files in os.walk(root):
        for name in dirs + files:
            p = os.path.join(dirpath, name)
            st = os.stat(p)
            digest = hashlib.sha256(open(p, 'rb').read()).hexdigest() if os.path.isfile(p) else 'dir'
            out[os.path.relpath(p, root)] = (st.st_size, st.st_mtime_ns, digest)
    return out


def name(title, cfg, description=''):
    return judge.standard_name(title, description, cfg)


def test_meissen_all_segments(cfg):
    r = name('Meissen Platte Oval Blaue Blume Neuer Ausschnitt 2. Wahl 28 cm', cfg)
    assert r['std_name'] == 'Meissen梅森 蓝花 新剪裁器型 椭圆大盘'
    assert r['seller_grade'] == '2. Wahl' and 'Wahl' not in r['std_name'] and '等' not in r['std_name']
    assert (r['brand'], r['pattern_id'], r['form_id'], r['function_id']) == ('Meissen', 'MP-050', 'MS-002', 'FN-016')
    assert r['std_key'] == 'Meissen|MP-050|MS-002|FN-016'
    assert r['status'] == 'named' and r['kb_status'] == 'ok' and len(r['kb_fingerprint']) == 16
    assert r['era'] is None


def test_brand_only(cfg):
    # 复合词 Porzellanvase 以 Vase 结尾 → 功能花瓶，可生成名称
    r = name('Meissen Porzellanvase neu!', cfg)
    assert r['std_name'] == 'Meissen梅森 花瓶' and r['function_id'] == 'FN-044'
    # 无器型、无功能 → 不生成名称
    r = name('Meissen Porzellan neu!', cfg)
    assert r['std_name'] is None and r['status'] == 'insufficient' and r['std_key'] == 'Meissen|-|-|-'


def test_non_meissen_ignores_meissen_vocab(cfg):
    r = name('KPM Berlin Rocaille Zwiebelmuster Neuer Ausschnitt Terrine', cfg)
    assert r['brand'] == 'KPM Berlin' and r['brand_display'] == 'KPM柏林'
    assert r['pattern_id'] is None and r['form_id'] is None and r['function_id'] == 'FN-026'
    assert r['std_name'] == 'KPM柏林 带盖汤碗' and r['std_key'] == 'KPM Berlin|-|-|FN-026'


def test_no_brand_not_inferred_from_pattern(cfg):
    r = name('Zwiebelmuster Teller', cfg)
    assert r['brand'] is None and r['pattern_id'] is None
    assert r['std_name'] == '盘' and r['std_key'] == '-|-|-|FN-009'


def test_vocab_details(cfg):
    # 德语形容词 Meissener、目录编号映射到 MP、金绘区分目录行、括号内别名、型录主词、年代
    assert name('Meissener Porzellan 800101 Kaffeetasse um 1900', cfg)['std_name'] == '约1900年 Meissen梅森 蓝洋葱 咖啡杯碟'
    assert name('Meissen Gestreute Blümchen Goldbronze Teller', cfg)['pattern_id'] == '010199'
    assert name('Meissen Gestreute Blümchen Teller', cfg)['pattern'] == '散花'
    assert name('Meissen Grüner Löwe Teller', cfg)['pattern_id'] == 'MP-031'
    assert name('Meissen Teller 19./20. Jh.', cfg)['era'] == '19–20世纪'
    assert name('Meissen Teller 1860-1910', cfg)['era'] == '1860–1910年'
    assert name('Meissen seit 1710 Teller 1800 €', cfg)['era'] is None
    # 反例说明与批注不成词条：Porzellanplatte 不是瓷板，Meissen 不是“篮”
    assert name('Meissen Porzellanplatte', cfg)['function_id'] is None
    assert name('Meissen Prunkschale', cfg)['function_id'] is None
    # 标题优先，描述补充
    r = name('Meissen Schwanenservice', cfg, description='Ein schöner Teller')
    assert r['std_name'] == 'Meissen梅森 天鹅系列器型 盘'
    assert name('Meissen Teller I. Wahl', cfg)['seller_grade'] == 'I. Wahl'


def test_kb_missing(tmp_path, caplog):
    cfg = dict(em_kb_path=str(tmp_path / 'nope'))
    with caplog.at_level(logging.WARNING, logger='curio.judge'):
        r1 = name('Meissen Teller', cfg)
        r2 = name('Meissen Vase', cfg)
    assert r1['kb_status'] == r2['kb_status'] == 'missing'
    assert r1['status'] == 'insufficient' and r1['brand'] is None
    assert sum('知识库不存在' in m for m in caplog.messages) == 1


def test_kb_partial_and_cache_invalidation(cfg, kb):
    fn = kb / '05 General Knowledge 通用知识/买手培训/功能词表.csv'
    before = name('Meissen Teller', cfg)
    fn.rename(fn.with_suffix('.bak'))
    try:
        r = name('Meissen Neuer Ausschnitt Teller', cfg)
        assert r['kb_status'] == 'partial:functions' and r['function_id'] is None
        assert r['std_name'] == 'Meissen梅森 新剪裁器型' and r['kb_fingerprint'] != before['kb_fingerprint']
    finally:
        fn.with_suffix('.bak').rename(fn)
    assert name('Meissen Teller', cfg)['kb_status'] == 'ok'


def test_kb_read_only(cfg, kb):
    before = tree(kb)
    for t in ('Meissen Platte Oval Blaue Blume', 'KPM Berlin Terrine', 'Zwiebelmuster Teller'):
        name(t, cfg)
    judge._KB_CACHE.clear()
    name('Meissen Vase', cfg)
    assert tree(kb) == before


def item(i, title, price=40, description=''):
    return dict(item_id=i, platform='ebay', title=title, description=description, price=price, currency='EUR',
                buying='FIXED_PRICE', url='https://www.ebay.de/itm/example', image='')


@pytest.fixture
def db(tmp_path):
    return Store(tmp_path / 'curio.db')


def test_ingest_writes_std_name_without_changing_judgement(db, cfg, tmp_path):
    from curio.rules import evaluate
    it = item('a', 'Meissen Zwiebelmuster Teller')
    db.ingest(it, cfg)
    with db.connect() as c:
        row = c.execute('SELECT * FROM std_names WHERE item_id=?', ('a',)).fetchone()
        got = c.execute('SELECT agent_decision,agent_reason,fair_price FROM items WHERE item_id=?', ('a',)).fetchone()
    assert row['std_name'] == 'Meissen梅森 蓝洋葱 盘' and row['std_key'] == 'Meissen|MP-001|-|FN-009'
    assert row['kb_status'] == 'ok' and row['generated_at']
    expect = evaluate(dict(it, listed_price=40.0, currency='EUR'), dict(cfg, blocked_words=[]))
    assert tuple(got) == (expect['decision'], expect['reason'], expect.get('fair', ''))
    # 标题变了 → 名称随之更新，仍只一行
    db.ingest(item('a', 'Meissen Zwiebelmuster Vase'), cfg)
    with db.connect() as c:
        rows = c.execute('SELECT std_name FROM std_names WHERE item_id=?', ('a',)).fetchall()
    assert [r[0] for r in rows] == ['Meissen梅森 蓝洋葱 花瓶']


def test_similar_prices_and_by_code(db, cfg):
    db.ingest(item('a', 'Meissen Zwiebelmuster Teller', 40), cfg)
    db.ingest(item('b', 'Meissen Teller Zwiebelmuster 24 cm', 55), cfg)
    db.ingest(item('a', 'Meissen Zwiebelmuster Teller', 35), cfg)
    db.ingest(item('c', 'Meissen Zwiebelmuster Vase', 90), cfg)
    rows = db.similar_prices('Meissen|MP-001|-|FN-009')
    assert [(r['item_id'], r['price']) for r in rows] == [('a', 40), ('b', 55), ('a', 35)]
    assert [r['seen_at'] for r in rows] == sorted(r['seen_at'] for r in rows)
    assert db.similar_prices('Meissen|-|-|-') == []
    with db.connect() as c:
        triage.keep(c, 'b', None, source='test')
        code = c.execute("SELECT code FROM item_codes WHERE item_id='b'").fetchone()[0]
    got = db.by_code(code)
    assert got['code'] == code and got['item_id'] == 'b' and got['std_name'] == 'Meissen梅森 蓝洋葱 盘'
    assert len(got['prices']) == 3 and got['segments']['pattern'] == '蓝洋葱'
    assert db.by_code('CO-9999') == {}


def test_ingest_continues_when_kb_missing(db, tmp_path):
    cfg = dict(load(), em_kb_path=str(tmp_path / 'missing-kb'))
    assert db.ingest(item('a', 'Meissen Teller'), cfg) == 'new'
    with db.connect() as c:
        row = c.execute('SELECT * FROM std_names').fetchone()
    assert row['kb_status'] == 'missing' and row['status'] == 'insufficient' and row['std_name'] is None


def test_backfill_idempotent(db, cfg, kb, monkeypatch, capsys):
    import scripts.backfill_std_names as bf
    for i, t in enumerate(('Meissen Zwiebelmuster Teller', 'Meissen Porzellan', 'KPM Berlin Terrine')):
        db.ingest(item(str(i), t), cfg)
    with db.connect() as c:
        c.execute('DELETE FROM std_names')
    monkeypatch.setattr(bf, 'load', lambda: cfg)
    first = bf.main([str(db.path)])
    with db.connect() as c:
        snap = c.execute('SELECT * FROM std_names ORDER BY item_id').fetchall()
    second = bf.main([str(db.path)])
    with db.connect() as c:
        again = c.execute('SELECT * FROM std_names ORDER BY item_id').fetchall()
    assert first['std_names'] == second['std_names'] == first['items'] == 3
    assert (first['written'], second['written'], second['unchanged']) == (3, 0, 3)
    assert (first['named'], first['insufficient']) == (2, 1)
    assert [tuple(r) for r in snap] == [tuple(r) for r in again]


def test_separator_only_gold_cell_does_not_crash(cfg, db):
    r = name('Meissen Gestreute Blümchen Teller', cfg)
    assert r['pattern_id'] == '010110'
    assert db.ingest(item('g', 'Meissen Gestreute Blümchen Teller'), cfg) == 'new'


def test_code_prefix_longest_wins(cfg):
    assert name('Meissen Teller 321010', cfg)['pattern_id'] == 'MP-018'
    assert name('Meissen Drache rot Teller', cfg)['pattern_id'] == 'MP-018'
    assert name('Meissen Teller 399999', cfg)['pattern_id'] == 'MP-090'


@pytest.mark.parametrize('title', ['Meissen Teller Preis 1850', 'Meissen Teller EUR 1900', 'Meissen Teller gekauft 2019',
                                   'Meissen Teller VB 1800', 'Meissen Teller Versand 1950', 'Meissen Teller € 1900'])
def test_price_and_purchase_years_are_not_eras(cfg, title):
    assert name(title, cfg)['era'] is None


def test_era_only_from_title(cfg):
    assert name('Meissen Teller', cfg, description='hergestellt 1900')['era'] is None
    assert name('Meissen Teller 1900', cfg)['era'] == '1900年'


@pytest.mark.parametrize('title', ['Teller im Stil von Meissen Zwiebelmuster', 'Teller wie Meissen Zwiebelmuster',
                                   'Teller nicht Meissen Zwiebelmuster', 'Meissen-Art Teller Zwiebelmuster',
                                   'Teller à la Meissen', 'Teller Meissen style', 'Teller style of Meissen'])
def test_loose_brand_mentions_are_ignored(cfg, title):
    r = name(title, cfg)
    assert r['brand'] is None and r['pattern_id'] is None


def test_brandless_key_has_no_similar_prices(db, cfg):
    db.ingest(item('x', 'Teller'), cfg)
    db.ingest(item('y', 'Plate'), cfg)
    assert db.similar_prices('-|-|-|FN-009') == []


def test_naming_error_never_breaks_ingest(db, cfg, monkeypatch, caplog):
    import curio.store as store
    def boom(*a, **k):
        raise RuntimeError('naming failed')
    monkeypatch.setattr(store, 'standard_name', boom)
    monkeypatch.setattr(store, '_STD_WARNED', [])
    with caplog.at_level(logging.WARNING, logger='curio.store'):
        assert db.ingest(item('a', 'Meissen Teller'), cfg) == 'new'
        assert db.ingest(item('b', 'Meissen Vase'), cfg) == 'new'
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM items').fetchone()[0] == 2
        assert c.execute('SELECT count(*) FROM triage').fetchone()[0] == 2
        assert c.execute('SELECT count(*) FROM std_names').fetchone()[0] == 0
    assert sum('标准名称生成失败' in m for m in caplog.messages) == 1


def test_restored_kb_refreshes_unchanged_item(db, kb, tmp_path):
    hidden = tmp_path / 'hidden-kb'
    kb.rename(hidden)
    cfg = dict(load(), em_kb_path=str(kb))
    try:
        db.ingest(item('a', 'Meissen Teller'), cfg)
        with db.connect() as c:
            assert c.execute('SELECT kb_status FROM std_names').fetchone()[0] == 'missing'
    finally:
        hidden.rename(kb)
    assert db.ingest(item('a', 'Meissen Teller'), cfg) == 'unchanged'
    with db.connect() as c:
        row = c.execute('SELECT kb_status,std_name FROM std_names').fetchone()
    assert tuple(row) == ('ok', 'Meissen梅森 盘')


def test_missing_row_regenerated_then_stable(db, cfg):
    db.ingest(item('a', 'Meissen Teller'), cfg)
    with db.connect() as c:
        c.execute('DELETE FROM std_names')
    assert db.ingest(item('a', 'Meissen Teller'), cfg) == 'unchanged'
    with db.connect() as c:
        first = c.execute('SELECT std_name,generated_at FROM std_names WHERE item_id=?', ('a',)).fetchone()
    assert first['std_name'] == 'Meissen梅森 盘'
    import time
    time.sleep(1.1)
    assert db.ingest(item('a', 'Meissen Teller'), cfg) == 'unchanged'
    with db.connect() as c:
        again = c.execute('SELECT generated_at FROM std_names WHERE item_id=?', ('a',)).fetchone()[0]
    assert again == first['generated_at']


def test_catalog_chinese_column_wins_over_german(tmp_path):
    """官方图案目录有「名称（中文）」列时，标题用中文名，不再把德文原样写进标题。"""
    root = make_kb(tmp_path / 'EM 西洋瓷器')
    write(root, M + '/4 Patterns 图案/Meissen_官方图案目录数据.csv',
          '图案编号,名称（德文）,名称（中文）,原目录文字,金绘（德文）,金绘（中文）,官方分类,原目录页码,档案路径\n'
          '210110,Frucht- und Blumenmalerei,果实与花卉,"Frucht- und Blumenmalerei, Goldrand",Goldrand,金边,2.1,20,'
          'a/210110 Frucht- und Blumenmalerei.md\n'
          '010110,Gestreute Blümchen,,"Gestreute Blümchen, bunt",Goldrand,金边,1.1,10,a/010110 Gestreute Blümchen 散花.md\n')
    cfg = dict(load(), em_kb_path=str(root))
    r = name('Meissen Teller Frucht- und Blumenmalerei Goldrand', cfg)
    assert r['pattern'] == '果实与花卉' and 'Frucht' not in r['std_name']
    assert name('Meissen Teller Gestreute Blümchen', cfg)['pattern'] == '散花'   # 中文列空时仍取档案名里的中文


def test_brand_display_has_no_berlin():
    """KPM 写成「KPM柏林」；其它常见品牌带中文名。"""
    assert judge.BRAND_DISPLAY['KPM Berlin'] == 'KPM柏林'
    assert judge.BRAND_ZH['Royal Copenhagen'] == '皇家哥本哈根'
