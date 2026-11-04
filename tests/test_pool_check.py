"""故事 26：商品池优先在售检查与商品池存档。

外部平台全部模拟（假 eBay、打桩的 Kleinanzeigen curl、打桩的图片下载与汇率），只用临时库，不读写 data/curio.db。
"""
import json
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import pytest
import requests

from curio import curio_monitor, sync, triage, vision
from curio.config import load
from curio.errors import CollectionError
from curio.schedule import KV_LAST_CHECK, KV_LAST_FETCH, KV_LAST_PUBLISHED, Budget
from curio.store import Store, item_images

ROOT = Path(__file__).resolve().parents[1]
_real_request = requests.sessions.Session.request
NOON = datetime(2026, 11, 4, 12, 0, tzinfo=ZoneInfo('Europe/Berlin')).timestamp()   # 到柏林午夜还剩 24 轮检查


@pytest.fixture(autouse=True)
def only_localhost():
    def guard(self, method, url, *a, **kw):
        assert urlsplit(url).hostname == '127.0.0.1', '测试不得连接外部主机'
        return _real_request(self, method, url, *a, **kw)
    fake_price = lambda amount, currency, root, archive_date=None: {'original_amount': amount, 'status': 'pending_rate'}
    with patch('requests.sessions.Session.request', guard), patch('curio.sync._notify'), \
         patch('curio.archive_price.archive_price', fake_price):
        yield


class Clock:
    def __init__(self, ts=None):
        self.ts = time.time() if ts is None else ts

    def __call__(self):
        return self.ts


@pytest.fixture
def root(tmp_path):
    shutil.copytree(ROOT / 'config', tmp_path / 'config')
    (tmp_path / '.env').write_text('EBAY_CLIENT_ID=dummy\nEBAY_CLIENT_SECRET=dummy\n')
    return tmp_path


@pytest.fixture
def cfg(root):
    return load(root)


@pytest.fixture
def db(root):
    return Store(root / 'data' / 'curio.db')


def add(db, cfg, n, platform='ebay', checked_hours_ago=None, published=False, image='https://i.ebayimg.com/x.jpg'):
    """一件留下（商品池）的在售货；checked_hours_ago 给出时补一条那时的检查记录。"""
    iid = f'ka:{n}' if platform == 'kleinanzeigen' else f'etsy:{n}' if platform == 'etsy' else f'it{n:03d}'
    url = f'https://www.kleinanzeigen.de/s-anzeige/x/{n}' if platform == 'kleinanzeigen' else f'https://www.ebay.de/itm/{n}'
    db.ingest(dict(item_id=iid, platform=platform, title=f'Meissen Tasse Nr {n}', price=100 + n, currency='EUR',
                   buying='FIXED_PRICE', url=url, image=image), cfg)
    with db.connect() as c:
        triage.keep(c, iid)
        if published:
            code = triage.code(c, iid)
            c.execute('INSERT INTO published(code,item_id,publish_no,changed_at) VALUES(?,?,?,?)',
                      (code, iid, 1, sync.utc_now()))
    if checked_hours_ago is not None:
        checked(db, iid, platform, checked_hours_ago)
    return iid


def checked(db, iid, platform, hours_ago):
    curio_monitor._ensure_check_events(db)
    at = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat(timespec='seconds')
    with db.connect() as c:
        c.execute('INSERT INTO status_check_events(item_id,platform,checked_at,outcome,evidence) VALUES(?,?,?,?,?)',
                  (iid, platform, at, 'active', 'test'))


def events(db, iid):
    with db.connect() as c:
        return c.execute('SELECT count(*) FROM status_check_events WHERE item_id=?', (iid,)).fetchone()[0]


def status(db, iid):
    with db.connect() as c:
        return c.execute('SELECT status FROM items WHERE item_id=?', (iid,)).fetchone()[0]


class FakeEbay:
    def __init__(self, db, budget=None, gone=(), fail=(), images=()):
        self.db, self.budget, self.gone, self.fail, self.images = db, budget, set(gone), set(fail), list(images)
        self.purpose = 'check'
        self.calls = []

    def detail(self, iid):
        if self.budget is not None:
            self.budget.spend('ebay', self.purpose)
        self.calls.append(iid)
        if iid in self.fail:
            raise CollectionError('eBay 网络请求失败')
        if iid in self.gone:
            return None
        with self.db.connect() as c:
            row = c.execute('SELECT * FROM items WHERE item_id=?', (iid,)).fetchone()
        out = {'itemId': iid, 'title': row['title'], 'price': {'value': str(row['listed_price']), 'currency': 'EUR'}}
        if row['image']:
            out['image'] = {'imageUrl': row['image']}      # 真实详情带主图；在售刷新不会把图清掉
        if self.images:
            out['image'] = {'imageUrl': self.images[0]}
            out['additionalImages'] = [{'imageUrl': u} for u in self.images[1:]]
        return out


def fake_download(fail=()):
    """假的图片下载：写一个按网址命名的本地文件，与 vision.download 同样的返回形状。"""
    import hashlib

    def dl(url, folder):
        if url in fail:
            raise vision.Failure('图片网络失败')
        name = hashlib.sha256(url.encode()).hexdigest() + '.jpg'
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(b'img')
        return dict(file='images/' + name, url=url, mime='image/jpeg')
    return dl


def idle_schedule(db, clock):
    """新货抓取、普通在售检查、已发布货检查都刚做过。"""
    db.set(KV_LAST_FETCH, clock.ts - 60)
    db.set('schedule_fetch_interval_min', 60)
    db.set(KV_LAST_CHECK, clock.ts - 60)
    db.set(KV_LAST_PUBLISHED, clock.ts - 60)


def run(root, api, **extra):
    with patch('curio.curio_monitor.EbayAPI', return_value=api), \
         patch('curio.curio_monitor.collect_ebay', Mock(return_value=0)) as fetch, \
         patch('curio.curio_monitor.collect_ka', side_effect=lambda store, config, budget=None: store.set('ka_status', 'ok') or 0), \
         patch('curio.curio_monitor.check_existing', Mock(return_value={'attempted': 0, 'errors': 0})) as check, \
         patch('curio.vision.download', fake_download()):
        return curio_monitor.run(root, extra.get('clock', Clock())), fetch, check


# ---------- 验收：只查到期的，下架落库 ----------

def test_acceptance_only_due_items_checked_and_gone_marked(db, cfg, root):
    clock = Clock()
    due = [add(db, cfg, n, checked_hours_ago=7) for n in (1, 2, 3)]
    fresh = [add(db, cfg, n, checked_hours_ago=0.5) for n in range(4, 11)]
    idle_schedule(db, clock)
    api = FakeEbay(db, gone={due[1]})
    result, fetch, check = run(root, api, clock=clock)
    assert result['pool_check_ran'] and not result['fetch_ran'] and not result['status_check_ran']
    assert not fetch.called and not check.called          # 空闲轮也查了到期的池货
    pc = result['pool_check']
    assert pc == dict(attempted=3, active=2, gone=1, errors=0, budget_skipped=0, deferred=0, due=3)
    assert status(db, due[1]) == 'gone' and status(db, due[0]) == status(db, due[2]) == 'active'
    assert all(events(db, i) == 2 for i in due) and all(events(db, i) == 1 for i in fresh)
    # 02 商品池随之显示已下架；拣选状态不变（仍为留下）
    from curio import ranking
    rows = {r['item_id']: r for r in ranking.pool(db, root)['items']}
    assert rows[due[1]]['on_sale'] is False and len(rows) == 10
    with db.connect() as c:
        assert triage.state(c, due[1]) == 'kept'
        assert c.execute('SELECT status FROM collection_runs').fetchone()[0] == 'ok'


def test_not_due_round_stays_idle(db, cfg, root):
    clock = Clock()
    add(db, cfg, 1, checked_hours_ago=2)
    idle_schedule(db, clock)
    api = FakeEbay(db)
    result, *_ = run(root, api, clock=clock)
    assert result['status'] == 'idle' and api.calls == []


# ---------- 到期、排序、已发布排除 ----------

def test_pool_rows_due_order_and_exclusions(db, cfg, root):
    never = add(db, cfg, 1)
    old = add(db, cfg, 2, checked_hours_ago=30)
    mid = add(db, cfg, 3, checked_hours_ago=7)
    add(db, cfg, 4, checked_hours_ago=2)                    # 未到期
    add(db, cfg, 5, checked_hours_ago=9, published=True)    # 已在已发布货检查里
    gone = add(db, cfg, 6, checked_hours_ago=9)
    db.mark_gone(gone)                                      # 已下架不再查
    db.ingest(dict(item_id='it099', platform='ebay', title='Meissen Teller', price=5, currency='EUR',
                   buying='FIXED_PRICE', url='https://www.ebay.de/itm/99', image=''), cfg)   # 不在商品池
    ka = add(db, cfg, 7, platform='kleinanzeigen', checked_hours_ago=8)
    ids = [r['item_id'] for r in curio_monitor.pool_rows(db, 6)]
    assert ids == [never, old, ka, mid]
    assert 'pool_last_check' not in curio_monitor.pool_rows(db, 6)[0]
    assert 'it004' in [r['item_id'] for r in curio_monitor.pool_rows(db, 1)]      # 间隔 1 小时时 2 小时前查过的也到期


def test_published_item_not_rechecked_by_pool(db, cfg, root):
    pub = add(db, cfg, 1, checked_hours_ago=9, published=True)
    api = FakeEbay(db)
    summary = curio_monitor.check_pool(db, api, cfg)
    assert summary['due'] == 0 and api.calls == [] and events(db, pub) == 1


# ---------- 预算与份额 ----------

def test_budget_tight_checks_oldest_within_share(db, cfg):
    ids = [add(db, cfg, n, checked_hours_ago=30 - n) for n in range(1, 21)]
    budget = Budget(db, {**cfg['api_budget'], 'ebay': 3}, Clock(NOON))
    api = FakeEbay(db, budget)
    summary = curio_monitor.check_pool(db, api, cfg, budget)
    assert api.calls == ids[:3]                              # 最久没查的先查
    assert summary['attempted'] == 3 and summary['budget_skipped'] == 2 and summary['errors'] == 0
    assert summary['due'] == 20
    assert all(events(db, i) == 1 for i in ids[3:])          # 没查的不写记录


def test_share_is_half_of_per_round_budget(db, cfg):
    ids = [add(db, cfg, n, checked_hours_ago=8) for n in range(1, 16)]
    # 480 ÷ 24 轮 = 20，一半 = 10
    budget = Budget(db, {**cfg['api_budget'], 'ebay': 480}, Clock(NOON))
    api = FakeEbay(db, budget)
    summary = curio_monitor.check_pool(db, api, cfg, budget)
    assert summary['attempted'] == 10 and summary['budget_skipped'] == 0 and len(api.calls) == 10
    # 份额至少 5 件
    assert curio_monitor._pool_share(Budget(db, {**cfg['api_budget'], 'ebay': 30}, Clock(NOON)), 'ebay') == 5


def test_request_failure_counts_error_and_others_judged(db, cfg):
    ids = [add(db, cfg, n, checked_hours_ago=8 - n * 0.1) for n in (1, 2, 3)]
    api = FakeEbay(db, gone={ids[2]}, fail={ids[1]})
    summary = curio_monitor.check_pool(db, api, cfg)
    assert summary == dict(attempted=3, active=1, gone=1, errors=1, budget_skipped=0, deferred=0, due=3)
    assert status(db, ids[1]) == 'active' and status(db, ids[2]) == 'gone'


def test_kleinanzeigen_pool_items_checked(db, cfg):
    ka = add(db, cfg, 1, platform='kleinanzeigen', checked_hours_ago=8, image='')
    with patch('curio.curio_monitor.ka_curl', return_value=(404, '', 'https://www.kleinanzeigen.de/s-anzeige/x/1', None)):
        summary = curio_monitor.check_pool(db, None, cfg)
    assert summary['gone'] == 1 and status(db, ka) == 'gone'


# ---------- 存档 ----------

def test_archive_pool_snapshots_twelve_local_images(db, cfg, root):
    iid = add(db, cfg, 1, checked_hours_ago=1)
    urls = [f'https://i.ebayimg.com/{n}.jpg' for n in range(15)]
    api = FakeEbay(db, images=urls)
    with patch('curio.vision.download', fake_download()):
        summary = curio_monitor.archive_pool(db, root, api, cfg)
    assert summary['archived'] == 1 and summary['errors'] == 0
    with db.connect() as c:
        data = json.loads(c.execute('SELECT data FROM product_snapshots WHERE item_id=?', (iid,)).fetchone()[0])
        imgs = item_images(c, iid, '', root)
        assert vision.pool_archived(c, iid, root)
    assert len(data['images']) == 12 and data['pool_archive'] == {'image_limit': 12, 'detail_ok': True, 'download_failures': 0}
    assert 'price_archive' in data and len(imgs) == 12 and all(x['src'].startswith('/archive/images/') for x in imgs)
    # 已完成的不再做
    with patch('curio.vision.download', fake_download()):
        assert curio_monitor.archive_pool(db, root, api, cfg)['pending'] == 0
    assert api.calls == [iid]


def test_archive_failure_retried_next_round(db, cfg, root):
    iid = add(db, cfg, 1, checked_hours_ago=1)
    api = FakeEbay(db, images=['https://i.ebayimg.com/a.jpg'])
    bad = {'https://i.ebayimg.com/a.jpg', 'https://i.ebayimg.com/x.jpg'}
    with patch('curio.vision.download', fake_download(fail=bad)):
        first = curio_monitor.archive_pool(db, root, api, cfg)
    assert first['errors'] == 1 and first['archived'] == 0
    with db.connect() as c:
        row = c.execute('SELECT attempts,done,last_error FROM pool_archive WHERE item_id=?', (iid,)).fetchone()
    assert row['attempts'] == 1 and row['done'] == 0 and '图片网络失败' in row['last_error']
    with patch('curio.vision.download', fake_download()):
        second = curio_monitor.archive_pool(db, root, api, cfg)
    assert second['archived'] == 1


def test_archive_includes_gone_pool_items_and_respects_budget(db, cfg, root):
    gone = add(db, cfg, 1)
    db.mark_gone(gone)
    api = FakeEbay(db, gone={gone})
    budget = Budget(db, {**cfg['api_budget'], 'ebay': 0}, Clock(NOON))
    with patch('curio.vision.download', fake_download()):
        assert curio_monitor.archive_pool(db, root, api, cfg, budget)['attempted'] == 0   # 预算用完：不做、不计尝试
        summary = curio_monitor.archive_pool(db, root, api, cfg)
    assert summary['archived'] == 1          # 已下架的货：详情已不可用，仍存下搜索结果里的图
    with db.connect() as c:
        assert vision.has_local_snapshot(c, gone, root)


def test_gone_in_pool_check_archives_immediately(db, cfg, root):
    iid = add(db, cfg, 1, checked_hours_ago=8)
    api = FakeEbay(db, gone={iid})
    with patch('curio.vision.download', fake_download()):
        summary = curio_monitor.check_pool(db, api, cfg, root=root)
    assert summary['gone'] == 1
    with db.connect() as c:
        assert vision.has_local_snapshot(c, iid, root)
        data = json.loads(c.execute('SELECT data FROM product_snapshots WHERE item_id=?', (iid,)).fetchone()[0])
    assert data['status'] == 'gone'


def test_gone_with_existing_local_snapshot_not_redone(db, cfg, root):
    iid = add(db, cfg, 1, checked_hours_ago=8)
    with patch('curio.vision.download', fake_download()):
        curio_monitor.archive_pool(db, root, FakeEbay(db), cfg)      # 进池时已存档（含本机图片）
    api = FakeEbay(db, gone={iid})
    with patch('curio.vision.download', fake_download()):
        curio_monitor.check_pool(db, api, cfg, root=root)
    assert api.calls == [iid]                # 只有判定那一次，没有再取详情做快照
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM product_snapshots WHERE item_id=?', (iid,)).fetchone()[0] == 1


def test_gone_keeps_previous_images_when_platform_deleted_them(db, cfg, root):
    iid = add(db, cfg, 1, checked_hours_ago=8)
    vision.init(db)
    with patch('curio.vision.download', fake_download()):
        vision.snapshot(db, dict(item_id=iid, platform='ebay', image='https://i.ebayimg.com/x.jpg', listed_price=1,
                                 currency='EUR'), root, api=FakeEbay(db))     # 早先的普通快照（4 张上限）
    with db.connect() as c:
        before = json.loads(c.execute('SELECT data FROM product_snapshots WHERE item_id=?', (iid,)).fetchone()[0])['images']
    api = FakeEbay(db, gone={iid})
    with patch('curio.vision.download', fake_download(fail={'https://i.ebayimg.com/x.jpg'})):
        archived = curio_monitor._archive_one(db, dict(item_id=iid, platform='ebay', image='https://i.ebayimg.com/x.jpg',
                                                       listed_price=1, currency='EUR'), root, api, None)
    with db.connect() as c:
        last = json.loads(c.execute('SELECT data FROM product_snapshots WHERE item_id=? ORDER BY id DESC', (iid,)).fetchone()[0])
    # 已有照片保留；新图下载失败，所以不算完成，留待下轮（尝试次数上限内）重试
    assert archived is False and last['images'] == before and '使用此前已保存的照片' in last['acquisition_errors']


def test_run_archives_newly_kept_item(db, cfg, root):
    clock = Clock()
    iid = add(db, cfg, 1)                    # 刚留下、从没查过
    idle_schedule(db, clock)
    result, *_ = run(root, FakeEbay(db), clock=clock)
    assert result['pool_check']['attempted'] == 1, result
    assert result['pool_archive']['archived'] == 1
    with db.connect() as c:
        assert item_images(c, iid, '', root)[0]['src'].startswith('/archive/images/')


# ---------- 配置 ----------

def test_pool_check_hours_config(root):
    assert load(root)['pool_check_hours'] == 6
    path = root / 'config' / 'settings.yaml'
    text = path.read_text()
    for bad in ('0', '49', '2.5', 'true'):
        path.write_text(text.replace('pool_check_hours: 6', 'pool_check_hours: ' + bad))
        with pytest.raises(ValueError, match='pool_check_hours'):
            load(root)
    path.write_text(text.replace('pool_check_hours: 6', 'pool_check_hours: 12'))
    assert load(root)['pool_check_hours'] == 12


# ---------- 评审补充 ----------

class FakeEtsy:
    """假的 Etsy 客户端：/listings/batch 计入 etsy 在售检查预算；gone 里的返回 state=removed。"""

    def __init__(self, budget=None, gone=()):
        self.budget, self.gone = budget, set(gone)
        self.purpose = 'check'
        self.calls = 0

    def get(self, path, params):
        if self.budget is not None:
            self.budget.spend('etsy', self.purpose)
        self.calls += 1
        ids = params['listing_ids'].split(',')
        return {'results': [{'listing_id': int(i), 'title': 'Meissen', 'state': 'removed' if 'etsy:' + i in self.gone else 'active',
                             'quantity': 1, 'price': {'amount': 1000, 'divisor': 100, 'currency_code': 'EUR'}} for i in ids]}


def test_run_with_failing_pool_item_is_partial(db, cfg, root):
    clock = Clock()
    iid = add(db, cfg, 1, checked_hours_ago=8)
    idle_schedule(db, clock)
    result, *_ = run(root, FakeEbay(db, fail={iid}), clock=clock)
    assert result['pool_check']['errors'] == 1
    with db.connect() as c:
        assert c.execute('SELECT status FROM collection_runs').fetchone()[0] == 'partial'


def test_error_outcome_is_due_again_next_round(db, cfg):
    iid = add(db, cfg, 1, checked_hours_ago=8)
    curio_monitor.check_pool(db, FakeEbay(db, fail={iid}), cfg)
    assert events(db, iid) == 2
    assert [r['item_id'] for r in curio_monitor.pool_rows(db, 6)] == [iid]      # 失败记录不算查过
    curio_monitor.check_pool(db, FakeEbay(db), cfg)
    assert curio_monitor.pool_rows(db, 6) == []


def test_archive_gives_up_after_attempt_cap(db, cfg, root):
    iid = add(db, cfg, 1, checked_hours_ago=1)
    api = FakeEbay(db)
    with patch('curio.vision.download', fake_download(fail={'https://i.ebayimg.com/x.jpg'})):
        for _ in range(curio_monitor.ARCHIVE_ATTEMPTS + 1):
            curio_monitor.archive_pool(db, root, api, cfg)
    assert len(api.calls) == curio_monitor.ARCHIVE_ATTEMPTS
    with db.connect() as c:
        row = c.execute('SELECT attempts,done FROM pool_archive WHERE item_id=?', (iid,)).fetchone()
    assert row['attempts'] == curio_monitor.ARCHIVE_ATTEMPTS and row['done'] == 0


def test_kleinanzeigen_archive_charges_budget_once(db, cfg, root):
    iid = add(db, cfg, 1, platform='kleinanzeigen', checked_hours_ago=1, image='')
    page = '<div id="viewad-description-text">Schöne Tasse</div><img src="https://img.kleinanzeigen.de/api/v1/a.jpg">'
    budget = Budget(db, cfg['api_budget'], Clock(NOON))
    with patch('curio.curio_monitor.ka_curl', return_value=(200, page, 'https://www.kleinanzeigen.de/s-anzeige/x/1', None)) as curl, \
         patch('curio.vision.download', fake_download()):
        summary = curio_monitor.archive_pool(db, root, None, cfg, budget)
    assert summary['archived'] == 1 and curl.call_count == 1
    assert budget.snapshot()['kleinanzeigen']['check'] == 1
    with db.connect() as c:
        data = json.loads(c.execute('SELECT data FROM product_snapshots WHERE item_id=?', (iid,)).fetchone()[0])
    assert data['pool_archive']['detail_ok'] is True and data['description'] == 'Schöne Tasse'


def test_paused_kleinanzeigen_archive_skipped_without_attempt(db, cfg, root):
    iid = add(db, cfg, 1, platform='kleinanzeigen', checked_hours_ago=1, image='')
    db.set('ka_pause_until', time.time() + 3600)
    with patch('curio.curio_monitor.ka_curl') as curl:
        summary = curio_monitor.archive_pool(db, root, None, cfg)
    assert summary['attempted'] == 0 and not curl.called
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM pool_archive WHERE item_id=?', (iid,)).fetchone()[0] == 0


def test_kleinanzeigen_block_during_archive_pauses(db, cfg, root):
    add(db, cfg, 1, platform='kleinanzeigen', checked_hours_ago=1, image='')
    with patch('curio.curio_monitor.ka_curl', return_value=(429, '', '', None)):
        curio_monitor.archive_pool(db, root, None, cfg)
    assert float(db.get('ka_pause_until')) > time.time()


def test_archive_budget_exhausted_does_not_count_attempt(db, cfg, root):
    iid = add(db, cfg, 1, checked_hours_ago=1)
    budget = Budget(db, {**cfg['api_budget'], 'ebay': 1}, Clock(NOON))
    budget.spend('ebay', 'check')
    with patch.object(Budget, 'available', return_value=True), patch('curio.vision.download', fake_download()):
        summary = curio_monitor.archive_pool(db, root, FakeEbay(db, budget), cfg, budget)
    assert summary['attempted'] == 0
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM pool_archive WHERE item_id=?', (iid,)).fetchone()[0] == 0


@pytest.mark.parametrize('blocked', ['paused', 'disabled'])
def test_due_items_on_unusable_platform_keep_round_idle(db, cfg, root, blocked):
    clock = Clock()
    add(db, cfg, 1, platform='kleinanzeigen', checked_hours_ago=8, image='')
    if blocked == 'paused':
        db.set('ka_pause_until', time.time() + 3600)
    else:
        path = root / 'config' / 'settings.yaml'
        path.write_text(path.read_text().replace('kleinanzeigen_enabled: true', 'kleinanzeigen_enabled: false'))
    idle_schedule(db, clock)
    with patch('curio.curio_monitor.ka_curl') as curl:
        result, *_ = run(root, FakeEbay(db), clock=clock)
    assert result['status'] == 'idle' and not curl.called


def test_due_ebay_items_with_exhausted_budget_keep_round_idle(db, cfg, root):
    clock = Clock()
    add(db, cfg, 1, checked_hours_ago=8)
    path = root / 'config' / 'settings.yaml'
    path.write_text(path.read_text().replace('ebay: 4500', 'ebay: 0'))
    idle_schedule(db, clock)
    api = FakeEbay(db)
    result, *_ = run(root, api, clock=clock)
    assert result['status'] == 'idle' and api.calls == []


def test_etsy_pool_items_checked_in_batches(db, cfg):
    a, b = add(db, cfg, 1, platform='etsy', checked_hours_ago=8), add(db, cfg, 2, platform='etsy', checked_hours_ago=7)
    etsy = FakeEtsy(gone={b})
    summary = curio_monitor.check_pool(db, None, cfg, etsy=etsy)
    assert summary['attempted'] == 2 and summary['active'] == 1 and summary['gone'] == 1 and etsy.calls == 1
    assert status(db, a) == 'active' and status(db, b) == 'gone'
    with db.connect() as c:
        assert c.execute("SELECT count(*) FROM status_check_events WHERE platform='etsy' AND outcome IN ('active','gone')").fetchone()[0] == 4   # 2 条旧记录 + 本轮 2 条


def test_etsy_budget_exhausted_counts_budget_skipped(db, cfg):
    add(db, cfg, 1, platform='etsy', checked_hours_ago=8)
    add(db, cfg, 2, platform='etsy', checked_hours_ago=7)
    budget = Budget(db, {**cfg['api_budget'], 'etsy': 0}, Clock(NOON))
    etsy = FakeEtsy(budget)
    # 预检放行、发请求时才发现预算用完（BudgetExhausted）：这批两件都计 budget_skipped，不写记录、不算错误
    with patch.object(Budget, 'available', return_value=True):
        summary = curio_monitor.check_pool(db, None, cfg, budget, etsy)
    assert summary['budget_skipped'] == 2 and summary['attempted'] == 0 and summary['errors'] == 0 and etsy.calls == 0


def test_etsy_partial_verdicts_count_error(db, cfg):
    a = add(db, cfg, 1, platform='etsy', checked_hours_ago=8)
    with patch('curio.etsy.judge_listings', return_value=({}, None)):
        summary = curio_monitor.check_pool(db, None, cfg, etsy=FakeEtsy())
    assert summary['errors'] == 1 and summary['attempted'] == 0 and events(db, a) == 1


def test_timeout_counts_deferred(db, cfg):
    ids = [add(db, cfg, n, checked_hours_ago=8) for n in (1, 2, 3)]
    ticks = iter([0, 0, 0, 1000, 1000, 1000])
    summary = curio_monitor.check_pool(db, FakeEbay(db), cfg, monotonic=lambda: next(ticks))
    assert summary['attempted'] == 2 and summary['deferred'] == 1
