"""故事 16（CAP-5）：后台 04 匹配——规则（judge.match_customer）、按客人汇总、确认 / 不合适、单件页链接可达（AD-29）、
坏数据与资料库不可用、后台接口。

GoodCustomer 一律用临时资料库（tmp_path），从不读写真实 ~/Desktop/goodcustomerDB；站点地址只写进临时 .env，
任何非 127.0.0.1 的请求直接失败。界面交互（展开、复制、390px 布局）见故事 Verification 的人工检查。
"""
import json
import logging
import re
import threading
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
import requests

from curio import matching, rules, triage
from curio.config import load, match_rules, match_rules_for
from curio.store import Store

ROOT = Path(__file__).resolve().parents[1]
REAL_GC = Path('~/Desktop/goodcustomerDB').expanduser().resolve()
SITE = 'https://shop.example.com'
_real_request = requests.sessions.Session.request


@pytest.fixture(autouse=True)
def only_localhost():
    def guard(self, method, url, *a, **kw):
        assert urlsplit(url).hostname == '127.0.0.1', '测试不得连接外部主机'
        return _real_request(self, method, url, *a, **kw)
    with patch('requests.sessions.Session.request', guard), patch('curio.sync._notify'):
        yield


@pytest.fixture(autouse=True)
def fresh_cache():
    matching._cache.clear()
    from curio import guests
    guests._gc_cache.clear()
    yield
    matching._cache.clear()
    guests._gc_cache.clear()


# ---------- 临时 GoodCustomer 资料库 ----------

def customer(db, cid, name, needs=(), prefs=(), root='customers', status='active', raw_lines=()):
    d = Path(db) / root / cid
    (d / '06_结构化结果').mkdir(parents=True, exist_ok=True)
    (d / 'customer.json').write_text(json.dumps({'customer_id': cid, 'display_name': name, 'aliases': [],
                                                 'status': status, 'platform_accounts': []}, ensure_ascii=False))
    lines = [json.dumps(n, ensure_ascii=False) for n in needs] + list(raw_lines)
    (d / '06_结构化结果' / 'needs.jsonl').write_text('\n'.join(lines) + '\n' if lines else '')
    (d / '06_结构化结果' / 'preferences.jsonl').write_text(
        ''.join(json.dumps(p, ensure_ascii=False) + '\n' for p in prefs))


def need(desc, maker=(), patterns=(), status='open', filters=()):
    out = {'need_type': 'active_search', 'status': status, 'additional_description': desc}
    if maker:
        out['maker'] = list(maker)
    if patterns:
        out['pattern_names_raw'] = list(patterns)
    if filters:
        out['object_filters'] = list(filters)
    return out


def pref(dim, value, polarity='like', source='explicit', status='active'):
    return {'dimension': dim, 'value': value, 'polarity': polarity, 'source_type': source, 'status': status}


@pytest.fixture
def gc(tmp_path):
    db = tmp_path / 'goodcustomerDB'
    (db / '_system').mkdir(parents=True)
    (db / '_system' / 'db.json').write_text('{"schema_version": "gc_schema_v3_2_local"}')
    assert db.resolve() != REAL_GC
    return {'db': str(db), 'gcdb': str(tmp_path / 'no-gcdb.py')}


@pytest.fixture
def root(tmp_path):
    (tmp_path / '.env').write_text(f'CURIO_SERVER_URL={SITE}\nCURIO_ADMIN_TOKEN=test-token\n')
    return tmp_path


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / 'data' / 'curio.db')


@pytest.fixture
def cfg():
    return dict(load(), keywords=['Meissen'])


def add(store, cfg, iid, title, brand, function=None, pattern=None, pattern_id=None, score=None, keep=True):
    store.ingest(dict(item_id=iid, platform='ebay', title=title, price=480, currency='EUR', buying='FIXED_PRICE',
                      url='https://www.ebay.de/itm/1', image='', description='Sehr guter Zustand.'), cfg)
    seg = {'brand': brand, 'pattern': pattern, 'function': function}
    name = ' '.join(x for x in (brand, pattern, function) if x)
    with store.connect() as c:
        c.execute('''INSERT OR REPLACE INTO std_names(item_id,std_name,std_key,status,brand,pattern_id,form_id,function_id,
            era,seller_grade,segments,kb_status,kb_fingerprint,generated_at,overpaint)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)''',
                  (iid, name, f'{brand}|-|-|-', 'named', brand, pattern_id, None, None, None, None,
                   json.dumps(seg, ensure_ascii=False), 'ok', 'fp', '2026-11-03T00:00:00+00:00'))
        if score is not None:
            c.execute("INSERT OR REPLACE INTO scores(item_id,score,source,reason,rule_version,scored_at) "
                      "VALUES(?,?,'rule','测试',1,'2026-11-03T00:00:00+00:00')", (iid, score))
        if keep:
            triage.keep(c, iid)
        return triage.code(c, iid)


def deliver(store, *codes, hidden=()):
    """模拟一份已送达快照（publishes=delivered + publish_items + published）。"""
    with store.connect() as c:
        no = (c.execute('SELECT MAX(publish_no) FROM publishes').fetchone()[0] or 0) + 1
        c.execute("INSERT INTO publishes(publish_no,event_id,created_at,rank,pool_size,tombstones,images,status) "
                  "VALUES(?,?,?,?,?,0,0,'delivered')", (no, 'ev%d' % no, '2026-11-03T08:00:00.000Z', '[]', len(codes)))
        for code in codes:
            iid = c.execute('SELECT item_id FROM item_codes WHERE code=?', (code,)).fetchone()[0]
            c.execute('INSERT INTO publish_items(publish_no,code,item_id) VALUES(?,?,?)', (no, code, iid))
            c.execute('INSERT OR REPLACE INTO published(code,item_id,publish_no,hidden) VALUES(?,?,?,?)',
                      (code, iid, no, 1 if code in hidden else 0))


@pytest.fixture
def scene(store, cfg, gc):
    """王姐明说想要塞弗尔蓝色杯碟；李先生从购买推出喜欢 Meissen；
    赵女士喜欢 Meissen 但不喜欢蓝洋葱；钱先生只喜欢杯（没写品牌）。"""
    codes = dict(
        sevres=add(store, cfg, 'it-sevres', 'Sèvres Tasse bleu céleste', 'Sèvres', function='杯', score=50),
        vase=add(store, cfg, 'it-vase', 'Meissen Vase Blumen', 'Meissen', function='花瓶', score=70),
        onion=add(store, cfg, 'it-onion', 'Meissen Zwiebelmuster Teller', 'Meissen', function='盘',
                  pattern='蓝洋葱', pattern_id='MP-001', score=60),
        herend=add(store, cfg, 'it-herend', 'Herend Tasse', 'Herend', function='杯', score=40))
    db = gc['db']
    customer(db, 'GC000101', '王姐', needs=[need('塞弗尔蓝色系列的杯、碟、壶', maker=['Sèvres'], patterns=['塞弗尔蓝色系列'])])
    customer(db, 'GC000102', '李先生', prefs=[pref('maker', 'Meissen', source='purchase')])
    customer(db, 'GC000103', '赵女士', prefs=[pref('maker', 'Meissen'), pref('pattern', '蓝洋葱', polarity='dislike')])
    customer(db, 'GC000104', '钱先生', prefs=[pref('object_type', '杯碟')])
    return codes


def by_name(view):
    return {c['name']: c for c in view['customers']}


def codes_of(row):
    return [it['code'] for it in row['items']]


# ---------- I/O 矩阵 ----------

def test_need_beats_inferred_and_rows_grouped_by_customer(store, gc, root, scene):
    v = matching.view(store, gc, root)
    assert v['available'] is True
    names = [c['name'] for c in v['customers']]
    assert names[0] == '王姐'                                   # 有明说想要的排前
    rows = by_name(v)
    wang = rows['王姐']
    assert codes_of(wang) == [scene['sevres']] and wang['has_need'] and wang['total'] == 1
    it = wang['items'][0]
    assert it['grade'] == 'need' and it['grade_label'] == '明说想要'
    assert it['reasons'] == ['明说想要：塞弗尔蓝色系列的杯、碟、壶']
    li = rows['李先生']
    assert set(codes_of(li)) == {scene['vase'], scene['onion']} and not li['has_need']
    assert all(x['grade'] == 'inferred' and x['grade_label'] == '推算口味' for x in li['items'])
    assert li['items'][0]['reasons'] == ['推算口味：Meissen（购买）']
    assert codes_of(li) == [scene['vase'], scene['onion']]      # 同级按分数
    assert v['count'] == 3                                      # 有未处理匹配的客人数


def test_dislike_pattern_excludes_and_form_only_never_matches(store, gc, root, scene):
    rows = by_name(matching.view(store, gc, root))
    zhao = rows['赵女士']
    assert codes_of(zhao) == [scene['vase']]                    # 蓝洋葱的货不匹配
    assert zhao['items'][0]['grade'] == 'explicit' and zhao['items'][0]['reasons'] == ['明说喜欢：Meissen']
    assert '钱先生' not in rows                                  # 只对上器型（别的品牌的杯）不算


def test_reject_removes_for_that_customer_and_updates_count(store, gc, root, scene):
    r = matching.decide(store, gc, 'GC000102', 'it-vase', 'rejected', 'rq-1', root=root)
    assert r['decision'] == 'rejected'
    assert matching.decide(store, gc, 'GC000102', 'it-vase', 'rejected', 'rq-1', root=root) == r          # 同一 request_id 重放
    rows = by_name(matching.view(store, gc, root))
    assert codes_of(rows['李先生']) == [scene['onion']]
    assert scene['vase'] in codes_of(rows['赵女士'])            # 只对该客人不再提出
    with pytest.raises(ValueError, match='请先撤销'):
        matching.decide(store, gc, 'GC000102', 'it-vase', 'confirmed', 'rq-2', root=root)
    matching.decide(store, gc, 'GC000102', 'it-onion', 'rejected', 'rq-3', root=root)
    v = matching.view(store, gc, root)
    assert '李先生' not in by_name(v) and v['count'] == 2


def test_confirm_keeps_row_but_not_counted(store, gc, root, scene):
    matching.decide(store, gc, 'GC000101', 'it-sevres', 'confirmed', 'rq-c', root=root)
    v = matching.view(store, gc, root)
    wang = by_name(v)['王姐']
    assert wang['items'][0]['decision'] == 'confirmed' and wang['pending'] == 0
    assert v['count'] == 2


def test_decide_validation_writes_nothing(store, gc, root, scene):
    for args in (('GC000101', 'it-sevres', 'maybe'), ('nope', 'it-sevres', 'confirmed'),
                 ('GC000101', 'no-such', 'confirmed')):
        with pytest.raises(ValueError):
            matching.decide(store, gc, *args, 'rq-bad-' + args[2], root=root)
    with store.connect() as c:
        triage.transition(c, 'it-herend', 'out_of_pool', source='backoffice')
    with pytest.raises(ValueError, match='不在商品池'):
        matching.decide(store, gc, 'GC000101', 'it-herend', 'confirmed', 'rq-out', root=root)
    with store.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM match_decisions').fetchone()[0] == 0


def test_link_only_when_delivered_and_not_hidden(store, gc, root, scene):
    def link():
        return by_name(matching.view(store, gc, root))['王姐']['items'][0]
    it = link()
    assert it['link'] is None and it['link_reason'] == '请先发布这件货'            # 未发布
    with store.connect() as c:                                                  # 只在待送达快照里：仍不可用
        c.execute("INSERT INTO publishes(publish_no,event_id,created_at,rank,pool_size,tombstones,images,status) "
                  "VALUES(1,'ev-p','2026-11-03T08:00:00.000Z','[]',1,0,0,'pending')")
        c.execute('INSERT INTO publish_items VALUES(1,?,?)', (scene['sevres'], 'it-sevres'))
        c.execute('INSERT INTO published(code,item_id,publish_no) VALUES(?,?,1)', (scene['sevres'], 'it-sevres'))
    assert link()['link'] is None
    with store.connect() as c:
        c.execute("UPDATE publishes SET status='delivered' WHERE publish_no=1")
    it = link()
    assert it['link'] == SITE + '/item/' + scene['sevres'] and it['link_reason'] is None   # 已发布
    assert re.fullmatch(r'https://shop\.example\.com/item/CO-[0-9]{4,8}', it['link'])
    with store.connect() as c:                                                  # 已发布后售出（隐藏）
        c.execute('UPDATE published SET hidden=1 WHERE code=?', (scene['sevres'],))
    it = link()
    assert it['link'] is None and it['link_reason'] == '请先发布这件货'


def test_link_not_per_customer(store, gc, root, scene):
    deliver(store, scene['vase'])
    rows = by_name(matching.view(store, gc, root))
    links = {rows[n]['items'][[x['code'] for x in rows[n]['items']].index(scene['vase'])]['link'] for n in ('李先生', '赵女士')}
    assert links == {SITE + '/item/' + scene['vase']}


def test_site_not_configured_disables_copy(store, gc, tmp_path, scene):
    bare = tmp_path / 'bare'
    bare.mkdir()
    deliver(store, scene['sevres'])
    v = matching.view(store, gc, bare)
    it = by_name(v)['王姐']['items'][0]
    assert v['site'] is False and it['link'] is None and '站点地址未配置' in it['link_reason']


def test_bad_json_line_skipped_with_warning(store, gc, root, scene, caplog):
    customer(gc['db'], 'GC000101', '王姐', needs=[need('塞弗尔蓝色系列的杯、碟、壶', maker=['Sèvres'], patterns=['塞弗尔蓝色系列'])],
             raw_lines=['{not json', '[1, 2]'])
    with caplog.at_level(logging.WARNING, logger='curio.matching'):
        v = matching.view(store, gc, root)
    assert codes_of(by_name(v)['王姐']) == [scene['sevres']]
    assert v['warnings'] == 2
    warns = [r for r in caplog.records if 'needs.jsonl' in r.getMessage()]
    assert len(warns) == 1 and '2 行' in warns[0].getMessage()


def test_gc_unavailable(store, root, tmp_path, scene):
    v = matching.view(store, {'db': str(tmp_path / 'missing'), 'gcdb': ''}, root)
    assert v == dict(available=False, error='找不到 GoodCustomer 资料库', count=0, customers=[], site=True,
                     site_problem=None, warnings=0, unreadable=0)
    assert matching.view(store, None, root)['available'] is False


def test_skips_merged_archived_and_reads_potential_customers(store, gc, root, scene):
    db = gc['db']
    customer(db, 'GC000105', '已合并', prefs=[pref('maker', 'Meissen')], status='merged')
    customer(db, 'GC000106', '已归档', prefs=[pref('maker', 'Meissen')], status='archived')
    customer(db, 'GC000107', '潜在', prefs=[pref('maker', '梅森')], root='潜在客户')
    names = set(by_name(matching.view(store, gc, root)))
    assert '潜在' in names and not names & {'已合并', '已归档'}


def test_sold_and_out_of_pool_items_not_matched(store, gc, root, scene):
    with store.connect() as c:
        triage.transition(c, 'it-vase', 'mark_sold', source='backoffice')
        triage.transition(c, 'it-onion', 'out_of_pool', source='backoffice')
    assert '李先生' not in by_name(matching.view(store, gc, root))


def test_gc_files_never_written(store, gc, root, scene):
    files = sorted(p for p in Path(gc['db']).rglob('*') if p.is_file())
    before = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in files}
    matching.view(store, gc, root)
    matching.decide(store, gc, 'GC000101', 'it-sevres', 'confirmed', 'rq-w', root=root)
    after = sorted(p for p in Path(gc['db']).rglob('*') if p.is_file())
    assert after == files and {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in files} == before


# ---------- 规则（judge.match_customer） ----------

@pytest.fixture
def vocab():
    return match_rules_for(ROOT)


def facts(vocab, brand, title, function=None, pattern=None, pattern_id=None):
    std = {'brand': brand, 'pattern_id': pattern_id,
           'segments': json.dumps({'pattern': pattern, 'function': function}, ensure_ascii=False)}
    return rules.match_facts(std, title, vocab)


def test_rule_brand_required_and_aliases(vocab):
    f = facts(vocab, 'Meissen', 'Meissen Rote Rose Tasse', function='杯碟', pattern='红玫瑰', pattern_id='MP-014')
    assert rules.match_customer(f, [], [pref('pattern', '梅森红玫瑰')], vocab)['reasons'] == ['明说喜欢：梅森红玫瑰']
    assert rules.match_customer(f, [], [pref('pattern', '红玫瑰')], vocab) is None          # 图案对上、品牌没提到
    assert rules.match_customer(f, [], [pref('form', '杯')], vocab) is None
    assert rules.match_customer(f, [], [pref('maker', 'KPM')], vocab) is None
    m = rules.match_customer(f, [], [pref('maker', 'Meissen', source='purchase'), pref('form', '杯碟')], vocab)
    assert m['grade'] == 'inferred'                                       # 明说的只有器型，品牌来自购买
    assert rules.match_customer(facts(vocab, None, 'Tasse'), [], [pref('maker', 'Meissen')], vocab) is None


def test_rule_negated_brand_mentions(vocab):
    f = facts(vocab, 'Meissen', 'Meissen Vase', function='花瓶')
    assert rules.match_customer(f, [], [pref('maker', 'KPM、梅森以外的小牌子')], vocab) is None
    # 排除词管整句（含"、"并列）：这条喜欢不算提到梅森，也不算提到 KPM
    assert rules.match_customer(f, [], [pref('maker', '梅森、KPM以外的不要')], vocab) is None
    kpm = facts(vocab, 'KPM Berlin', 'KPM Vase', function='花瓶')
    assert rules.match_customer(kpm, [], [pref('maker', '梅森、KPM以外的不要')], vocab) is None
    assert rules.match_customer(f, [], [pref('maker', '除了KPM、梅森都喜欢')], vocab) is None
    # 不喜欢"梅森、老德以外的小牌子"不是不喜欢梅森
    keep = [pref('maker', 'Meissen'), pref('maker', '梅森、老德以外的小牌子', polarity='dislike')]
    assert rules.match_customer(f, [], keep, vocab)['grade'] == 'explicit'
    assert rules.match_customer(f, [need('梅森、KPM以外的杯子', maker=['梅森以外'])], [], vocab) is None
    assert rules.match_customer(f, [need('画得好的瓷板画（不一定梅森）')], [], vocab) is None
    assert rules.match_customer(f, [], [pref('maker', '除了梅森')], vocab) is None
    # 不喜欢的喜好不做否定检查："不要梅森"就是不喜欢梅森（不是双重否定）
    assert rules.match_customer(f, [], [pref('maker', 'Meissen'), pref('maker', '不要梅森', polarity='dislike')], vocab) is None
    assert rules.match_customer(f, [], [pref('maker', 'Meissen'), pref('maker', 'Meissen', polarity='dislike')], vocab) is None


def test_rule_needs(vocab):
    cup = facts(vocab, 'Meissen', 'Meissen Tasse', function='杯碟', pattern='红玫瑰', pattern_id='MP-014')
    assert rules.match_customer(cup, [need('一等天鹅颈红玫瑰杯碟两组', patterns=['红玫瑰'])], [], vocab) is None  # 没提品牌
    ok = need('梅森红玫瑰杯碟两组', patterns=['红玫瑰'])
    assert rules.match_customer(cup, [ok], [], vocab)['grade'] == 'need'
    assert rules.match_customer(cup, [need('梅森蓝洋葱杯', patterns=['蓝洋葱'])], [], vocab) is None   # 图案不对
    assert rules.match_customer(cup, [need('梅森的壶', maker=['Meissen'])], [], vocab) is None       # 器型不对
    assert rules.match_customer(cup, [need('梅森的东西', maker=['梅森'])], [], vocab)['grade'] == 'need'
    assert rules.match_customer(cup, [dict(ok, status='closed')], [], vocab) is None
    assert rules.match_customer(cup, [ok], [pref('pattern', '红玫瑰（梅森）', polarity='dislike')], vocab) is None
    assert rules.match_customer(cup, [ok], [pref('pattern', '红玫瑰', polarity='dislike', status='historical')],
                                vocab)['grade'] == 'need'                                  # 非 active 的不算
    long = need('梅森' + '很长的描述' * 20)
    assert rules.match_customer(cup, [long], [], vocab)['reasons'][0].endswith('…')


def test_rule_ignores_unmapped_dimensions_and_bad_rows(vocab):
    f = facts(vocab, 'Meissen', 'Meissen Vase', function='花瓶')
    assert rules.match_customer(f, ['x', None], ['y', pref('motif_subject', '梅森')], vocab) is None


def test_matching_yaml_validated(tmp_path):
    (tmp_path / 'config').mkdir()
    good = (ROOT / 'config' / 'matching.yaml').read_text()
    (tmp_path / 'config' / 'matching.yaml').write_text(good)
    assert match_rules(tmp_path)['grades']['need']['label'] == '明说想要'
    bad = good.replace('weight: 3', 'weight: 0')
    (tmp_path / 'config' / 'matching.yaml').write_text(bad)
    with pytest.raises(ValueError, match='weight'):
        match_rules(tmp_path)
    (tmp_path / 'config' / 'matching.yaml').write_text(good.replace('pattern: [pattern]', 'colour: [pattern]'))
    with pytest.raises(ValueError, match='dimensions'):
        match_rules(tmp_path)


# ---------- 后台接口 ----------

@pytest.fixture
def http(store, root, gc, scene):
    from curio.review import server
    httpd = server(store, port=0, root=root, flush=lambda: None, gc=gc, pull=lambda: {'accounts_status': 'ok'})
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = 'http://127.0.0.1:' + str(httpd.server_port)
    html = urllib.request.urlopen(base + '/').read().decode()
    token = re.search("const token='([^']+)'", html)[1]

    def get(path):
        try:
            with urllib.request.urlopen(base + path) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def post(path, body, tok=None):
        req = urllib.request.Request(base + path, json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json', 'X-Curio-Token': tok or token})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)
    yield dict(html=html, get=get, post=post, base=base)
    httpd.shutdown()
    httpd.server_close()


def test_backoffice_page_has_match_section(http):
    html = http['html']
    assert '<a class="tc" id="tab04" href="/#match">' in html and 'id="cMatch"' in html
    assert 'id="matchView"' in html and '暂无配上货的客人' in html
    assert '<b>匹配</b><span class="na">暂不可用' not in html
    js = urllib.request.urlopen(http['base'] + '/triage.js').read().decode()
    assert 'window.curioMatches' in js
    assert '已复制单件页链接 · 去微信发给' in js and '未能自动复制，请长按选择链接' in js and '未保存' in js
    assert "'aria-disabled', 'true'" in js
    rk = urllib.request.urlopen(http['base'] + '/ranking.js').read().decode()
    assert "'#match'" in rk and "'tab04'" in rk


def test_api_match_and_decide(http, scene):
    status, v = http['get']('/api/match')
    assert status == 200 and v['count'] == 3 and v['customers'][0]['name'] == '王姐'
    body = {'customer_id': 'GC000102', 'item_id': 'it-vase', 'decision': 'rejected', 'request_id': 'h-1'}
    status, r = http['post']('/api/match/decide', body)
    assert status == 200 and r['decision'] == 'rejected'
    assert http['post']('/api/match/decide', body) == (200, r)
    status, r = http['post']('/api/match/decide', dict(body, decision='confirmed', request_id='h-2'))
    assert status == 400 and '不合适' in r['error']
    status, r = http['post']('/api/match/decide', dict(body, request_id='h-1', item_id='it-onion'))
    assert status == 400 and '重复请求内容不同' in r['error']
    assert http['post']('/api/match/decide', dict(body, request_id='h-3'), tok='wrong')[0] == 403
    status, v = http['get']('/api/match')
    assert codes_of(by_name(v)['李先生']) == [scene['onion']]


# ---------- 复查后补充 ----------

def test_customer_order_need_then_pending_then_total(store, gc, root, scene):
    db = gc['db']
    customer(db, 'GC000201', 'A客人', prefs=[pref('pattern', 'Sèvres 蓝色系列')])                    # 1 件
    customer(db, 'GC000202', 'B客人', prefs=[pref('maker', 'Meissen'), pref('maker', 'Sèvres')])   # 3 件
    order = [c['name'] for c in matching.view(store, gc, root)['customers']]
    assert order == ['王姐', 'B客人', '李先生', 'A客人', '赵女士']        # 件数顺序与名字顺序不一致
    for iid in ('it-sevres', 'it-vase', 'it-onion'):
        matching.decide(store, gc, 'GC000202', iid, 'confirmed', 'rq-o-' + iid, root=root)
    order = [c['name'] for c in matching.view(store, gc, root)['customers']]
    assert order == ['王姐', '李先生', 'A客人', '赵女士', 'B客人']        # 处理完的不压在有待处理的前面


def test_title_only_detection_and_dislike(store, gc, root, cfg, vocab, scene):
    f = rules.match_facts({'brand': 'Meissen', 'segments': '{}'}, 'Meissen Zwiebelmuster Teller', vocab)
    assert f['forms'] == {'plate'} and f['pattern_groups'] == [0]
    likes = [pref('maker', 'Meissen')]
    assert rules.match_customer(f, [], likes, vocab)['grade'] == 'explicit'
    assert rules.match_customer(f, [], likes + [pref('pattern', '蓝洋葱', polarity='dislike')], vocab) is None
    code = add(store, cfg, 'it-z', 'Meissen Zwiebelmuster Teller', 'Meissen')     # 无图案、无功能
    customer(gc['db'], 'GC000103', '赵女士', prefs=[pref('maker', 'Meissen'), pref('pattern', '蓝洋葱', polarity='dislike')])
    v = by_name(matching.view(store, gc, root))
    assert codes_of(v['赵女士']) == [scene['vase']] and code in codes_of(v['李先生'])


def test_form_detection_false_positives(vocab):
    def forms(title):
        return rules.match_facts({'brand': 'Meissen', 'segments': '{}'}, title, vocab)['forms']
    assert forms('Meissen Untertasse') == {'saucer'}            # 不是杯
    assert forms('Meissen Kaffeetasse') == {'cup'}
    assert forms('Meissen Bildplatte Watteau') == {'plaque'}    # 不是盘
    assert forms('Meissen Cupid Amor') == set()                 # cup 不是 Cupid 的一部分
    assert forms('Meissen Vasenpaar') == set()
    cupid = rules.match_facts({'brand': 'Meissen', 'segments': '{}'}, 'Meissen Cupid', vocab)
    assert rules.match_customer(cupid, [need('梅森的杯子', maker=['Meissen'])], [], vocab)['grade'] == 'need'  # 器型未知不排除
    saucer = rules.match_facts({'brand': 'Meissen', 'segments': '{}'}, 'Meissen Untertasse', vocab)
    assert rules.match_customer(saucer, [], [pref('maker', 'Meissen', source='purchase'), pref('form', '杯')], vocab
                                )['reasons'] == ['推算口味：Meissen（购买）']


def test_brand_scoped_pattern_group(vocab):
    meissen = rules.match_facts({'brand': 'Meissen', 'segments': '{}'}, 'Meissen Kurland Tasse', vocab)
    kpm = rules.match_facts({'brand': 'KPM Berlin', 'segments': '{}'}, 'KPM Kurland Tasse', vocab)
    assert meissen['pattern_groups'] == [] and kpm['pattern_groups']
    assert rules.match_customer(meissen, [], [pref('pattern', '库尔兰')], vocab) is None
    m = rules.match_customer(kpm, [], [pref('pattern', '库尔兰')], vocab)                 # 只属于 KPM 的图案说明了品牌
    assert m['grade'] == 'explicit' and m['reasons'] == ['明说喜欢：库尔兰']


def test_value_raw_only_like_and_dislike(vocab):
    f = rules.match_facts({'brand': 'Sèvres', 'segments': '{"function": "杯"}'}, 'Sèvres Tasse', vocab)
    raw = dict(pref('maker', ''), value_raw='塞弗尔 / Sevres')
    assert rules.match_customer(f, [], [raw], vocab)['reasons'] == ['明说喜欢：塞弗尔 / Sevres']
    bad = dict(pref('maker', None, polarity='dislike'), value_raw='塞弗尔')
    assert rules.match_customer(f, [], [pref('maker', 'Sèvres'), bad], vocab) is None


def test_grade_reason_and_bad_types(vocab):
    f = facts(vocab, 'Meissen', 'Meissen Tasse', function='杯碟', pattern='红玫瑰', pattern_id='MP-014')
    # 明说的只有器型，品牌来自购买 → 推算口味
    m = rules.match_customer(f, [], [pref('maker', 'Meissen', source='purchase'), pref('form', '杯碟')], vocab)
    assert m['grade'] == 'inferred'
    many = [pref('maker', v) for v in ('Meissen', '梅森', '老梅森', 'Meissen 古董')]
    assert rules.match_customer(f, [], many, vocab)['reasons'] == ['明说喜欢：Meissen、梅森、老梅森等']
    weird = [dict(pref('maker', 'Meissen'), dimension=['maker']), dict(pref('maker', 'Meissen'), dimension={'a': 1}),
             dict(pref('maker', 'Meissen'), source_type=['explicit']), dict(pref('maker', 'Meissen', source=None), source_type=3)]
    m = rules.match_customer(f, [], weird, vocab)
    assert m['grade'] == 'inferred' and m['reasons'] == ['推算口味：Meissen（未注明来源）']
    only_filters = {'status': 'open', 'object_filters': ['梅森花瓶']}
    vase = facts(vocab, 'Meissen', 'Meissen Vase', function='花瓶')
    assert rules.match_customer(vase, [only_filters], [], vocab)['reasons'] == ['明说想要：梅森花瓶']


def test_confirm_then_reject_and_undo(store, gc, root, scene):
    matching.decide(store, gc, 'GC000101', 'it-sevres', 'confirmed', 'rq-a', root=root)
    matching.decide(store, gc, 'GC000101', 'it-sevres', 'rejected', 'rq-b', root=root)
    v = matching.view(store, gc, root)
    assert '王姐' not in by_name(v) and v['count'] == 2
    with pytest.raises(ValueError, match='无需撤销'):
        matching.decide(store, gc, 'GC000102', 'it-vase', 'undo', 'rq-c', root=root)
    r = matching.decide(store, gc, 'GC000101', 'it-sevres', 'undo', 'rq-d', root=root)
    assert r['decision'] == 'undo'
    wang = by_name(matching.view(store, gc, root))['王姐']
    assert wang['items'][0]['decision'] is None and wang['pending'] == 1


def test_decide_refuses_pairs_that_do_not_match(store, gc, root, scene):
    with pytest.raises(ValueError, match='没有匹配'):
        matching.decide(store, gc, 'GC000104', 'it-herend', 'confirmed', 'rq-x', root=root)   # 只对上器型
    with pytest.raises(ValueError, match='没有匹配'):
        matching.decide(store, gc, 'GC000103', 'it-onion', 'rejected', 'rq-y', root=root)     # 不喜欢蓝洋葱
    with pytest.raises(ValueError, match='没有匹配'):
        matching.decide(store, gc, 'GC000999', 'it-vase', 'confirmed', 'rq-z', root=root)


def test_invalid_utf8_and_unreadable_files(store, gc, root, scene, caplog):
    d = Path(gc['db']) / 'customers' / 'GC000101' / '06_结构化结果'
    line = json.dumps(need('塞弗尔蓝色系列的杯、碟、壶', maker=['Sèvres'], patterns=['塞弗尔蓝色系列']), ensure_ascii=False)
    (d / 'needs.jsonl').write_bytes(b'{"note": "\xff\xfe"}\n' + line.encode() + b'\n')
    v = matching.view(store, gc, root)
    assert codes_of(by_name(v)['王姐']) == [scene['sevres']] and v['warnings'] == 0
    p = Path(gc['db']) / 'customers' / 'GC000102' / '06_结构化结果' / 'preferences.jsonl'
    p.unlink()
    p.mkdir()                                                    # 读不了的"文件"
    matching._cache.clear()
    with caplog.at_level(logging.WARNING, logger='curio.matching'):
        v = matching.view(store, gc, root)
    assert v['unreadable'] == 1 and v['warnings'] == 0 and '李先生' not in by_name(v)
    assert any('未能读取' in r.getMessage() for r in caplog.records)


def test_link_uses_server_url_without_token(store, gc, tmp_path, scene):
    deliver(store, scene['sevres'])
    only_url = tmp_path / 'u'
    only_url.mkdir()
    (only_url / '.env').write_text('CURIO_SERVER_URL=https://shop.example.com/\n')
    it = by_name(matching.view(store, gc, only_url))['王姐']['items'][0]
    assert it['link'] == SITE + '/item/' + scene['sevres']
    (only_url / '.env').write_text('CURIO_SERVER_URL=ftp://shop.example.com\n')
    v = matching.view(store, gc, only_url)
    assert v['site'] is False and '站点地址无效' in by_name(v)['王姐']['items'][0]['link_reason']


def test_matching_yaml_brand_checks(tmp_path):
    (tmp_path / 'config').mkdir()
    good = (ROOT / 'config' / 'matching.yaml').read_text()
    f = tmp_path / 'config' / 'matching.yaml'
    f.write_text(good.replace('brands: [KPM Berlin]', 'brands: [KPM]'))
    with pytest.raises(ValueError, match='不在 matching.brands'):
        match_rules(tmp_path)
    f.write_text(good.replace('  Herend: [赫伦, 海兰德]', '  Herend: [赫伦]\n  HEREND: [海兰德]'))
    with pytest.raises(ValueError, match='规范化后相同'):
        match_rules(tmp_path)
    f.write_text(re.sub(r'(?ms)^dimensions:.*?(?=^brands:)', '', good))
    with pytest.raises(ValueError, match='缺少 dimensions'):
        match_rules(tmp_path)
    assert '雅致' not in good


def test_api_match_bad_config_is_400(http, root):
    (root / 'config').mkdir(exist_ok=True)
    (root / 'config' / 'matching.yaml').write_text('grades: 1\n')
    status, r = http['get']('/api/match')
    assert status == 400 and r['error'].startswith('匹配词表有误：')
    (root / 'config' / 'matching.yaml').unlink()
    assert http['get']('/api/match')[0] == 200
