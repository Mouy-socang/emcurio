"""故事 16（CAP-5）、故事 29：后台 04 匹配——规则（judge.match_customer）、按客人汇总、确认 / 不合适、单件页链接可达（AD-29）、
坏数据与资料库不可用、后台接口。

GoodCustomer 一律用临时资料库（tmp_path），从不读写真实 ~/Desktop/goodcustomerDB；站点地址只写进临时 .env，
任何非 127.0.0.1 的请求直接失败。界面交互（展开、复制、390px 布局）见故事 Verification 的人工检查。
"""
import datetime
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

def customer(db, cid, name, needs=(), prefs=(), root='customers', status='active', raw_lines=(), evidence=None):
    d = Path(db) / root / cid
    (d / '06_结构化结果').mkdir(parents=True, exist_ok=True)
    (d / 'customer.json').write_text(json.dumps({'customer_id': cid, 'display_name': name, 'aliases': [],
                                                 'status': status, 'platform_accounts': []}, ensure_ascii=False))
    lines = [json.dumps(n, ensure_ascii=False) for n in needs] + list(raw_lines)
    (d / '06_结构化结果' / 'needs.jsonl').write_text('\n'.join(lines) + '\n' if lines else '')
    (d / '06_结构化结果' / 'preferences.jsonl').write_text(
        ''.join(json.dumps(p, ensure_ascii=False) + '\n' for p in prefs))
    if evidence is not None:
        (d / '98_证据索引').mkdir(exist_ok=True)
        (d / '98_证据索引' / 'evidence.jsonl').write_text(
            ''.join(json.dumps(e, ensure_ascii=False) + '\n' for e in evidence))


RECENT = (datetime.date.today() - datetime.timedelta(days=30)).isoformat() + 'T10:00:00+02:00'


def need(desc, maker=(), patterns=(), status='open', filters=(), seen=RECENT, refs=()):
    out = {'need_type': 'active_search', 'status': status, 'additional_description': desc}
    if maker:
        out['maker'] = list(maker)
    if patterns:
        out['pattern_names_raw'] = list(patterns)
    if filters:
        out['object_filters'] = list(filters)
    if seen:
        out['last_seen_at'] = seen
    if refs:
        out['evidence_refs'] = list(refs)
    return out


def evd(eid, text, at, role='customer', name='王姐', status='active'):
    return {'evidence_id': eid, 'evidence_type': 'message', 'author_role': role, 'author_name': name,
            'content_excerpt': text, 'occurred_at': at, 'status': status}


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
    return loose(tmp_path)


def loose(root):
    """这些用例测的是匹配机制本身（品牌、否定、分级、排序），沿用"只对上品牌也算"的宽松口径；
    收紧（detail_required）另有用例按仓库里的 matching.yaml 测。"""
    text = (ROOT / 'config' / 'matching.yaml').read_text().replace(
        'detail_required: [need, explicit, inferred]', 'detail_required: []')
    (Path(root) / 'config').mkdir(parents=True, exist_ok=True)
    (Path(root) / 'config' / 'matching.yaml').write_text(text)
    return root


def loose_vocab():
    return dict(match_rules_for(ROOT), detail_required=frozenset())


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
    """王姐想要塞弗尔蓝色杯碟；李先生想要梅森的东西（宽松口径下只说品牌也算）；
    赵女士想要梅森但不喜欢蓝洋葱；钱先生只喜欢杯（没写品牌）；孙女士只有喜欢梅森的喜好（喜好不进 04）。"""
    codes = dict(
        sevres=add(store, cfg, 'it-sevres', 'Sèvres Tasse bleu céleste', 'Sèvres', function='杯', score=50),
        vase=add(store, cfg, 'it-vase', 'Meissen Vase Blumen', 'Meissen', function='花瓶', score=70),
        onion=add(store, cfg, 'it-onion', 'Meissen Zwiebelmuster Teller', 'Meissen', function='盘',
                  pattern='蓝洋葱', pattern_id='MP-001', score=60),
        herend=add(store, cfg, 'it-herend', 'Herend Tasse', 'Herend', function='杯', score=40))
    db = gc['db']
    customer(db, 'GC000101', '王姐', needs=[need('塞弗尔蓝色系列的杯、碟、壶', maker=['Sèvres'], patterns=['塞弗尔蓝色系列'])])
    customer(db, 'GC000102', '李先生', needs=[need('梅森的东西', maker=['Meissen'])])
    customer(db, 'GC000103', '赵女士', needs=[need('梅森', maker=['梅森'])],
             prefs=[pref('pattern', '蓝洋葱', polarity='dislike')])
    customer(db, 'GC000104', '钱先生', prefs=[pref('object_type', '杯碟')])
    customer(db, 'GC000108', '孙女士', prefs=[pref('maker', 'Meissen'), pref('form', '花瓶')])
    return codes


def by_name(view):
    return {c['name']: c for c in view['customers']}


def codes_of(row):
    return [it['code'] for it in row['items']]


# ---------- I/O 矩阵 ----------

def test_needs_only_rows_grouped_by_customer(store, gc, root, scene):
    v = matching.view(store, gc, root)
    assert v['available'] is True
    names = [c['name'] for c in v['customers']]
    assert names[0] == '李先生'                                 # 待处理件数多的在前
    rows = by_name(v)
    wang = rows['王姐']
    assert codes_of(wang) == [scene['sevres']] and wang['total'] == 1
    it = wang['items'][0]
    assert it['dims'] == ['品牌', '图案', '器型'] and it['consistency'] == 3
    assert it['need_text'] == '塞弗尔蓝色系列的杯、碟、壶' and it['quotes'] == []
    assert not {'grade', 'grade_label', 'weight', 'reasons'} & set(it) and 'has_need' not in wang
    li = rows['李先生']
    assert all(x['dims'] == ['品牌'] for x in li['items'])
    assert codes_of(li) == [scene['vase'], scene['onion']]      # 同一致性按分数
    assert '孙女士' not in rows                                  # 只有喜好：不进 04
    assert v['count'] == 3                                      # 有未处理匹配的客人数
    text = json.dumps(v, ensure_ascii=False)
    assert not any(w in text for w in ('明说想要', '明说喜欢', '推算口味'))


def test_dislike_pattern_excludes_and_form_only_never_matches(store, gc, root, scene):
    rows = by_name(matching.view(store, gc, root))
    zhao = rows['赵女士']
    assert codes_of(zhao) == [scene['vase']]                    # 蓝洋葱的货不匹配
    assert zhao['items'][0]['need_text'] == '梅森'
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
    customer(db, 'GC000105', '已合并', needs=[need('梅森', maker=['Meissen'])], status='merged')
    customer(db, 'GC000106', '已归档', needs=[need('梅森', maker=['Meissen'])], status='archived')
    customer(db, 'GC000107', '潜在', needs=[need('梅森', maker=['梅森'])], root='潜在客户')
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
    return loose_vocab()


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
    assert status == 200 and v['count'] == 3 and v['customers'][0]['name'] == '李先生'
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

def test_customer_order_pending_then_total(store, gc, root, scene):
    db = gc['db']
    customer(db, 'GC000201', 'A客人', needs=[need('Sèvres 蓝色系列', maker=['Sèvres'])])                      # 1 件
    customer(db, 'GC000202', 'B客人', needs=[need('梅森', maker=['Meissen']), need('塞弗尔', maker=['Sèvres'])])  # 3 件
    order = [c['name'] for c in matching.view(store, gc, root)['customers']]
    assert order == ['B客人', '李先生', 'A客人', '王姐', '赵女士']        # 件数顺序与名字顺序不一致
    for iid in ('it-sevres', 'it-vase', 'it-onion'):
        matching.decide(store, gc, 'GC000202', iid, 'confirmed', 'rq-o-' + iid, root=root)
    order = [c['name'] for c in matching.view(store, gc, root)['customers']]
    assert order == ['李先生', 'A客人', '王姐', '赵女士', 'B客人']        # 处理完的不压在有待处理的前面


def test_title_only_detection_and_dislike(store, gc, root, cfg, vocab, scene):
    f = rules.match_facts({'brand': 'Meissen', 'segments': '{}'}, 'Meissen Zwiebelmuster Teller', vocab)
    assert f['forms'] == {'plate'} and f['pattern_groups'] == [0]
    likes = [pref('maker', 'Meissen')]
    assert rules.match_customer(f, [], likes, vocab)['grade'] == 'explicit'
    assert rules.match_customer(f, [], likes + [pref('pattern', '蓝洋葱', polarity='dislike')], vocab) is None
    code = add(store, cfg, 'it-z', 'Meissen Zwiebelmuster Teller', 'Meissen')     # 无图案、无功能
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
    assert v['unreadable'] == 1 and v['warnings'] == 0 and '李先生' in by_name(v)    # 需求照常
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


# ---------- 收紧：品牌只是门槛（墨墨 2026-11-04） ----------

def test_brand_alone_never_matches_with_repo_rules():
    strict = match_rules_for(ROOT)
    assert strict['detail_required'] == {'need', 'explicit', 'inferred'}
    cup = facts(strict, 'Meissen', 'Meissen Tasse', function='杯')
    # 只说了品牌：需求、明说喜欢、推算口味都不算
    assert rules.match_customer(cup, [{'status': 'open', 'maker': ['梅森'], 'additional_description': '梅森的东西都可以'}],
                                [], strict) is None
    assert rules.match_customer(cup, [], [pref('maker', '梅森')], strict) is None
    assert rules.match_customer(cup, [], [pref('maker', 'Meissen', source='purchase')], strict) is None
    # 品牌 + 器型对上
    m = rules.match_customer(cup, [], [pref('maker', 'Meissen', source='purchase'), pref('form', '杯碟', source='purchase')], strict)
    assert m['grade'] == 'inferred'
    assert rules.match_customer(cup, [], [pref('maker', '梅森'), pref('form', '杯')], strict)['grade'] == 'explicit'
    assert rules.match_customer(cup, [{'status': 'open', 'maker': ['梅森'], 'additional_description': '梅森咖啡杯'}],
                                [], strict)['grade'] == 'need'
    # 器型对上但品牌没对上：仍不算
    assert rules.match_customer(cup, [], [pref('maker', 'KPM'), pref('form', '杯')], strict) is None


def test_pattern_satisfies_detail_and_scoped_pattern_names_brand():
    strict = match_rules_for(ROOT)
    plate = facts(strict, 'Meissen', 'Meissen Zwiebelmuster Teller', function='盘', pattern='蓝洋葱', pattern_id='MP-001')
    assert rules.match_customer(plate, [], [pref('maker', '梅森'), pref('pattern', '蓝洋葱', source='purchase')],
                                strict)['grade'] == 'explicit'
    need = {'status': 'open', 'maker': ['Meissen'], 'pattern_names_raw': ['蓝洋葱']}
    assert rules.match_customer(plate, [need], [], strict)['grade'] == 'need'


def test_ashtray_is_not_a_cup():
    strict = match_rules_for(ROOT)
    tray = facts(strict, 'Meissen', 'Meissen Aschenbecher rote Rose')
    assert 'cup' not in tray['forms'] and 'ashtray' in tray['forms']


def test_detail_required_validated(tmp_path):
    (tmp_path / 'config').mkdir()
    good = (ROOT / 'config' / 'matching.yaml').read_text()
    (tmp_path / 'config' / 'matching.yaml').write_text(good.replace('detail_required: [need, explicit, inferred]',
                                                                   'detail_required: [brand]'))
    with pytest.raises(ValueError, match='detail_required'):
        match_rules(tmp_path)


# ---------- 故事 29：近一年的需求、一致性排序、客人原话 ----------

TODAY = datetime.date(2026, 11, 4)


@pytest.fixture
def strict_root(tmp_path):
    """仓库里的 matching.yaml 原样（品牌只是门槛，need_recent_days: 365）。"""
    r = tmp_path / 'strict'
    (r / 'config').mkdir(parents=True)
    (r / 'config' / 'matching.yaml').write_text((ROOT / 'config' / 'matching.yaml').read_text())
    (r / '.env').write_text(f'CURIO_SERVER_URL={SITE}\n')
    return r


@pytest.fixture
def roses(store, cfg, gc):
    return dict(
        tray=add(store, cfg, 'it-tray', 'Meissen rote Rose Teller', 'Meissen', function='盘', pattern='红玫瑰',
                 pattern_id='MP-014', score=40),
        cup=add(store, cfg, 'it-cup', 'Meissen Kaffeetasse', 'Meissen', function='杯', score=90),
        rcup=add(store, cfg, 'it-rcup', 'Meissen rote Rose Tasse', 'Meissen', function='杯', pattern='红玫瑰',
                 pattern_id='MP-014', score=10))


def test_recent_need_matches_old_need_does_not(store, gc, strict_root, roses):
    customer(gc['db'], 'GC000301', '周女士', needs=[
        need('红玫瑰托盘', maker=['梅森'], patterns=['红玫瑰'], seen='2026-03-10T09:00:00+01:00')])
    customer(gc['db'], 'GC000302', '吴先生', needs=[
        need('红玫瑰托盘', maker=['梅森'], patterns=['红玫瑰'], seen='2022-10-01T09:00:00+02:00')])
    rows = by_name(matching.view(store, gc, strict_root, today=TODAY))
    assert '吴先生' not in rows                                     # 旧需求不出现
    zhou = rows['周女士']
    assert codes_of(zhou) == [roses['tray']]
    assert zhou['items'][0]['dims'] == ['品牌', '图案', '器型']
    with pytest.raises(ValueError, match='没有匹配'):                # 旧需求也不能确认
        matching.decide(store, gc, 'GC000302', 'it-tray', 'confirmed', 'rq-old', root=strict_root, today=TODAY)
    assert matching.decide(store, gc, 'GC000301', 'it-tray', 'confirmed', 'rq-new', root=strict_root,
                           today=TODAY)['decision'] == 'confirmed'


def test_consistency_order_full_matches_first(store, gc, strict_root, roses):
    customer(gc['db'], 'GC000303', '郑女士', needs=[
        need('梅森杯子', maker=['梅森'], seen='2026-09-01T09:00:00+02:00'),
        need('红玫瑰杯', maker=['梅森'], patterns=['红玫瑰'], seen='2026-08-01T09:00:00+02:00')])
    items = by_name(matching.view(store, gc, strict_root, today=TODAY))['郑女士']['items']
    # 红玫瑰杯 3 项全对（分数最低也在前）；梅森杯子只说品牌 + 器型（2 项，全部对上）在后
    assert [x['code'] for x in items] == [roses['rcup'], roses['cup']]
    assert items[0]['dims'] == ['品牌', '图案', '器型'] and items[0]['need_text'] == '红玫瑰杯'
    assert items[1]['dims'] == ['品牌', '器型'] and items[1]['consistency'] == 2


def test_same_consistency_full_before_partial(vocab):
    plain = facts(vocab, 'Meissen', 'Meissen rote Rose', pattern='红玫瑰', pattern_id='MP-014')   # 器型未知
    full = rules.need_match(need('梅森红玫瑰', maker=['梅森'], patterns=['红玫瑰']), plain, vocab, datetime.date.today())
    part = rules.need_match(need('梅森红玫瑰盘子', maker=['梅森'], patterns=['红玫瑰']), plain, vocab, datetime.date.today())
    assert full['dims'] == part['dims'] == ['品牌', '图案']
    assert (full['named'], part['named']) == (2, 3)
    assert rules.consistency_key(full) < rules.consistency_key(part)
    best = rules.best_need_match(plain, [need('梅森红玫瑰盘子', maker=['梅森'], patterns=['红玫瑰']),
                                         need('梅森红玫瑰', maker=['梅森'], patterns=['红玫瑰'])], [], vocab,
                                 datetime.date.today())
    assert best['text'] == '梅森红玫瑰'


def test_recent_dates(vocab):
    f = facts(vocab, 'Meissen', 'Meissen Tasse', function='杯')
    base = need('梅森杯', maker=['梅森'], seen=None)

    def hit(**dates):
        return rules.need_match(dict(base, **dates), f, vocab, TODAY) is not None
    assert hit(last_seen_at='2025-11-04T00:00:00+01:00')                     # 恰好 365 天前
    assert not hit(last_seen_at='2025-11-03T23:00:00+01:00')
    assert hit(last_seen_at='2026-01-02T08:00:00Z')
    assert hit(first_seen_at='2026-05-01')                                   # 缺 last_seen 用 first_seen
    assert hit(last_seen_at='坏日期', first_seen_at='2026-05-01T00:00:00+02:00')
    assert not hit(last_seen_at='坏日期', first_seen_at=12)                   # 都解析不了：不在近一年
    assert not hit()
    assert rules.need_match(dict(base, last_seen_at='2026-05-01', status='fulfilled'), f, vocab, TODAY) is None


def test_quotes_customer_first_by_time_with_dates(store, gc, strict_root, roses):
    ev = [evd('EVD-1', '还想要一个红玫瑰的托盘', '2026-03-10T09:05:00+01:00'),
          evd('EVD-2', '好的我留意', '2026-03-10T09:01:00+01:00', role='merchant', name='墨墨'),
          evd('EVD-3', '有红玫瑰的盘子吗', '2026-03-10T09:00:00+01:00'),
          evd('EVD-4', '大一点的', '2026-03-11T10:00:00+01:00')]
    customer(gc['db'], 'GC000304', '王姐', evidence=ev, needs=[
        need('红玫瑰托盘', maker=['梅森'], patterns=['红玫瑰'], seen='2026-03-11T10:00:00+01:00',
             refs=['EVD-2', 'EVD-1', 'EVD-4', 'EVD-3', 'EVT-9'])])
    q = by_name(matching.view(store, gc, strict_root, today=TODAY))['王姐']['items'][0]['quotes']
    assert [x['text'] for x in q] == ['有红玫瑰的盘子吗', '还想要一个红玫瑰的托盘', '大一点的', '好的我留意']
    assert [x['date'] for x in q] == ['2026-03-10', '2026-03-10', '2026-03-11', '2026-03-10']
    assert [x['customer'] for x in q] == [True, True, True, False] and q[3]['who'] == '墨墨'


def test_quotes_at_most_five(store, gc, strict_root, roses):
    ev = [evd('EVD-%d' % n, '第 %d 句' % n, '2026-03-1%dT09:00:00+01:00' % n) for n in range(7)]
    customer(gc['db'], 'GC000305', '冯女士', evidence=ev, needs=[
        need('红玫瑰托盘', maker=['梅森'], patterns=['红玫瑰'], seen='2026-03-11T10:00:00+01:00',
             refs=['EVD-%d' % n for n in range(7)])])
    q = by_name(matching.view(store, gc, strict_root, today=TODAY))['冯女士']['items'][0]['quotes']
    assert [x['text'] for x in q] == ['第 %d 句' % n for n in range(2, 7)]     # 留最近的 5 条，从早到晚显示


def test_quotes_mixed_offsets_undated_duplicates_retracted(store, gc, strict_root, roses):
    ev = [evd('EVD-a', '柏林早上', '2026-03-10T09:00:00+01:00'),        # 08:00 UTC
          evd('EVD-b', '北京下午', '2026-03-10T15:30:00+08:00'),        # 07:30 UTC：更早
          evd('EVD-c', '没写时间', None),
          evd('EVD-d', '撤回的话', '2026-03-09T09:00:00+01:00', status='retracted'),
          evd('EVD-e', '墨墨回复', '2026-03-01T09:00:00+01:00', role='merchant', name='墨墨')]
    customer(gc['db'], 'GC000308', '蒋女士', evidence=ev, needs=[
        need('红玫瑰托盘', maker=['梅森'], patterns=['红玫瑰'], seen='2026-03-11T10:00:00+01:00',
             refs=['EVD-c', 'EVD-a', 'EVD-a', 'EVD-d', 'EVD-e', 'EVD-b', 'EVD-b'])])
    q = by_name(matching.view(store, gc, strict_root, today=TODAY))['蒋女士']['items'][0]['quotes']
    assert [x['text'] for x in q] == ['北京下午', '柏林早上', '没写时间', '墨墨回复']
    assert q[2]['date'] == ''


def test_quotes_over_five_prefers_recent_customer_quotes(store, gc, strict_root, roses):
    ev = [evd('EVD-%d' % n, '客人 %d' % n, '2026-03-1%dT09:00:00+01:00' % n) for n in range(6)]
    ev.append(evd('EVD-m', '墨墨最新', '2026-03-19T09:00:00+01:00', role='merchant', name='墨墨'))
    customer(gc['db'], 'GC000309', '韩女士', evidence=ev, needs=[
        need('红玫瑰托盘', maker=['梅森'], patterns=['红玫瑰'], seen='2026-03-11T10:00:00+01:00',
             refs=['EVD-m'] + ['EVD-%d' % n for n in range(6)])])
    q = by_name(matching.view(store, gc, strict_root, today=TODAY))['韩女士']['items'][0]['quotes']
    assert [x['text'] for x in q] == ['客人 %d' % n for n in range(1, 6)]


def test_missing_evidence_still_matches_and_warns_once(store, gc, strict_root, roses, caplog):
    customer(gc['db'], 'GC000306', '陈女士', needs=[      # 没有 evidence.jsonl
        need('红玫瑰托盘', maker=['梅森'], patterns=['红玫瑰'], seen=RECENT, refs=['EVD-1'])])
    customer(gc['db'], 'GC000307', '褚先生', evidence=[evd('EVD-8', '别的', '2026-03-10T09:00:00+01:00')], needs=[
        need('红玫瑰杯', maker=['梅森'], patterns=['红玫瑰'], seen=RECENT, refs=['EVD-1'])])
    customer(gc['db'], 'GC000310', '卫女士', needs=[        # 旧需求、没有引用的需求：不计入警告
        need('红玫瑰托盘', maker=['梅森'], patterns=['红玫瑰'], seen='2022-03-11T10:00:00+01:00', refs=['EVD-1']),
        need('红玫瑰杯', maker=['梅森'], patterns=['红玫瑰'])])
    with caplog.at_level(logging.WARNING, logger='curio.matching'):
        rows = by_name(matching.view(store, gc, strict_root))
    assert rows['陈女士']['items'][0]['quotes'] == [] and rows['褚先生']['items'][0]['quotes'] == []
    warns = [r for r in caplog.records if '找不到原话' in r.getMessage()]
    assert len(warns) == 1 and '2 条' in warns[0].getMessage()


def test_need_recent_days_validated(tmp_path):
    (tmp_path / 'config').mkdir()
    good = (ROOT / 'config' / 'matching.yaml').read_text()
    f = tmp_path / 'config' / 'matching.yaml'
    assert 'need_recent_days: 365' in good
    assert match_rules(ROOT)['need_recent_days'] == 365
    for bad in ('0', '-3', '1.5', 'yes', '"365"'):
        f.write_text(good.replace('need_recent_days: 365', 'need_recent_days: ' + bad))
        with pytest.raises(ValueError, match='need_recent_days'):
            match_rules(tmp_path)


def test_page_has_quotes_and_no_grade_labels(http):
    js = urllib.request.urlopen(http['base'] + '/triage.js').read().decode()
    assert "'对上：'" in js and '看原话' in js and '没有找到原话' in js
    assert not any(w in js for w in ('明说想要', '明说喜欢', '推算口味'))
