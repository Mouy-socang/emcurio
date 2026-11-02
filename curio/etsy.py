"""Etsy 货源（Etsy Open API v3），由 legacy/desktop_curio/etsy_source.py 移植。

与旧版的区别（legacy README 列出的 4 个坑）：
- key 只从项目根目录 .env 读取（`config.etsy_credentials`），不读进程环境变量；
- 批量取详情中途失败时，只对拿到详情的编号判定，其余保持原状态；
- 库存 `quantity` 区分 None（未知，按在售）与 0（售罄，判下架）；
- 运行在 uv 管理的 Python 3.13，不再兼容系统 Python 3.9。
每个外部请求（含换 key 写法重试、429 重试）都先经 `schedule.Budget` 计数，且相邻请求间隔不少于 0.2 秒（Etsy 限 5 次/秒）。
错误信息不含响应正文、请求头或 key。
"""
import time
from datetime import datetime, timezone

import requests

from .errors import CollectionError
from .schedule import BudgetExhausted

API = 'https://openapi.etsy.com/v3/application'
MAX_PER_KEYWORD = 300   # 每个关键词每轮最多翻看多少条最新上架
BATCH = 100             # /listings/batch 每批最多 100 个编号
MIN_GAP = 0.2           # Etsy 限 5 次/秒：任意两次请求之间至少 0.2 秒
AUTH_BACKOFF_KEY = 'etsy_auth_backoff_until'
AUTH_BACKOFF_SECONDS = 3600
AUTH_BACKOFF_MESSAGE = 'Etsy 鉴权失败，1 小时后重试'


class EtsyAuthError(CollectionError):
    """两种 key 写法都被 401/403 拒绝。"""


def auth_backoff_active(store):
    try:
        return float(store.get(AUTH_BACKOFF_KEY) or 0) > time.time()
    except ValueError:
        return False


class EtsyAPI:
    def __init__(self, key, secret='', budget=None, session=None, store=None):
        self.key = key.strip()
        self.secret = (secret or '').strip()
        self.budget = budget
        self.purpose = 'fetch'
        self.session = session or requests.Session()
        self._header = None
        self._last = None
        self.store = store   # 有 store 时记录/清除鉴权退避（kv etsy_auth_backoff_until）

    def _throttle(self):
        if self._last is not None:
            wait = MIN_GAP - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
        self._last = time.monotonic()

    def _candidates(self):
        # 新版接口要求 x-api-key 写成 keystring:secret；没有 secret 时只试 keystring。
        if self._header:
            return [self._header]
        out = [f'{self.key}:{self.secret}'] if self.secret else []
        # HTTP 头只能是可见 ASCII；.env 里误填的占位文字（中文、空格）直接跳过，不发请求。
        return [h for h in out + [self.key] if h and h.isascii() and h.isprintable() and ' ' not in h]

    def get(self, path, params):
        auth_failed = False
        candidates = self._candidates()
        if not candidates:
            raise CollectionError('Etsy key 格式无效：检查 .env 的 ETSY_API_KEY / ETSY_SHARED_SECRET')
        for header in candidates:
            for attempt in range(3):
                if self.budget is not None:
                    self.budget.spend('etsy', self.purpose)
                self._throttle()
                try:
                    r = self.session.get(API + path, headers={'x-api-key': header}, params=params, timeout=30)
                except requests.RequestException:
                    raise CollectionError('Etsy 网络请求失败') from None
                if r.status_code == 429 and attempt < 2:
                    time.sleep(5 * (attempt + 1))
                    continue
                break
            if r.status_code in (401, 403):
                auth_failed = True
                continue
            if r.status_code != 200:
                raise CollectionError('Etsy 请求失败，HTTP ' + str(r.status_code))
            try:
                data = r.json()
            except ValueError:
                raise CollectionError('Etsy 响应无效') from None
            if not isinstance(data, dict):
                raise CollectionError('Etsy 响应无效')
            self._header = header
            if self.store is not None and self.store.get(AUTH_BACKOFF_KEY) is not None:
                with self.store.connect() as c:
                    c.execute('DELETE FROM kv WHERE k=?', (AUTH_BACKOFF_KEY,))
            return data
        if auth_failed:
            if self.store is not None:
                self.store.set(AUTH_BACKOFF_KEY, time.time() + AUTH_BACKOFF_SECONDS)
            raise EtsyAuthError('Etsy 鉴权失败：检查 .env 的 ETSY_API_KEY / ETSY_SHARED_SECRET，以及该 key 是否已启用')
        raise CollectionError('Etsy 请求失败')


def _price(p):
    p = p or {}
    try:
        amount = p.get('amount')
        if amount is None:
            return None, p.get('currency_code') or 'EUR'
        return float(amount) / float(p.get('divisor') or 1), p.get('currency_code') or 'EUR'
    except (TypeError, ValueError):
        return None, p.get('currency_code') or 'EUR'


def _ts(v):
    try:
        return datetime.fromtimestamp(int(v), tz=timezone.utc).isoformat(timespec='seconds')
    except (TypeError, ValueError, OverflowError, OSError):
        return ''


def _image(listing):
    imgs = sorted(listing.get('images') or [], key=lambda i: i.get('rank', 99))
    if not imgs:
        return ''
    i = imgs[0]
    return i.get('url_fullxfull') or i.get('url_570xN') or i.get('url_170x135') or ''


def _quantity(listing):
    q = listing.get('quantity')
    if q is None:
        return None
    try:
        return int(q)
    except (TypeError, ValueError):
        return None


def parse(listing, keyword):
    price, cur = _price(listing.get('price'))
    lid = listing['listing_id']
    return {
        'item_id': f'etsy:{lid}',
        'search': keyword,
        'title': listing.get('title', '') or '',
        'description': listing.get('description', '') or '',
        'price': price, 'currency': cur, 'buying': 'FIXED_PRICE',
        'url': listing.get('url', '') or f'https://www.etsy.com/listing/{lid}',
        'image': _image(listing),
        'created': _ts(listing.get('original_creation_timestamp') or listing.get('creation_timestamp')),
        'ends': '', 'condition': '', 'location': '',
        'price_text': f'{price:.2f} {cur}' if price is not None and cur != 'EUR' else '',
        'platform': 'etsy', 'country': '',
    }


def fetch_batch(api, ids):
    """按 listing_id 批量取详情（含图片和状态），每批最多 100。

    返回 ({listing_id: listing}, error, done)：某一批失败时停止，error 为该异常，
    done 是请求成功的批次里的编号——调用方只能对 done 里的编号下结论。
    """
    out, done = {}, set()
    ids = [str(i) for i in ids]
    for i in range(0, len(ids), BATCH):
        chunk = ids[i:i + BATCH]
        try:
            data = api.get('/listings/batch', {'listing_ids': ','.join(chunk), 'includes': 'Images'})
        except (CollectionError, BudgetExhausted) as error:
            return out, error, done
        done.update(chunk)
        for listing in data.get('results') or []:
            if isinstance(listing, dict):
                out[str(listing.get('listing_id'))] = listing
    return out, None, done


def collect_etsy(store, api, config, max_per_keyword=MAX_PER_KEYWORD):
    """按最新上架翻页；一页全是已知货即停（续上架的旧货不会挡住后面的新货）。只为新货补取图片。"""
    known = store.known()
    added, failures = 0, []
    for keyword in config['keywords']:
        # 一个关键词失败不挡后面的关键词；预算用完则整轮停。
        try:
            added += _collect_keyword(store, api, config, keyword, known, max_per_keyword)
        except EtsyAuthError:
            raise   # 鉴权失败对所有关键词都一样，不再白花请求
        except CollectionError as error:
            failures.append(f'{keyword}: {error}')
    if failures:
        raise CollectionError('Etsy 部分关键词未完成：' + '；'.join(failures))
    return added


def _collect_keyword(store, api, config, keyword, known, max_per_keyword):
    added = 0
    fresh, offset = [], 0
    while offset < max_per_keyword:
        limit = min(100, max_per_keyword - offset)
        data = api.get('/listings/active', {'keywords': keyword, 'limit': limit, 'offset': offset,
                                            'sort_on': 'created', 'sort_order': 'desc'})
        batch = data.get('results') or []
        seen = {str(l['listing_id']) for l in fresh}
        unseen = [l for l in batch if f"etsy:{l['listing_id']}" not in known and str(l['listing_id']) not in seen]
        fresh.extend(unseen)
        if not unseen or len(batch) < limit:
            break
        offset += len(batch)
    if not fresh:
        return 0
    # 搜索结果不带图片 → 批量补；补图失败的货本轮不入库，下一轮仍是新货会重试。
    detail, error, _ = fetch_batch(api, [l['listing_id'] for l in fresh])
    for listing in fresh:
        d = detail.get(str(listing['listing_id']))
        if d is None:
            continue
        listing['images'] = d.get('images') or []
        row = parse(listing, keyword)
        if store.ingest(row, config) == 'new':
            added += 1
        known.add(row['item_id'])
    if error is not None:
        raise error
    return added


def judge_listings(api, item_ids):
    """在售检查：批量查 Etsy 详情。返回 ({item_id: (outcome, evidence, listing)}, error)。

    outcome：active / gone / unknown（请求成功但响应里没有该货或没有 state，保持原状态）/
    skipped（所在批次没请求成功或没轮到，未检查，不应写检查记录）。绝不对没拿到详情的货判下架。
    """
    ids = [iid.split(':', 1)[1] for iid in item_ids]
    detail, error, done = fetch_batch(api, ids)
    out = {}
    for iid, lid in zip(item_ids, ids):
        listing = detail.get(lid)
        if lid not in done:
            out[iid] = ('skipped', 'batch not fetched', None)
        elif listing is None:
            out[iid] = ('unknown', 'Etsy detail not returned; state retained', None)
        elif not listing.get('state'):
            out[iid] = ('unknown', 'Etsy state missing; state retained', None)
        elif listing.get('state') != 'active':
            out[iid] = ('gone', f"Etsy state={listing.get('state')}; sale unknown", listing)
        elif (q := _quantity(listing)) is not None and q <= 0:
            out[iid] = ('gone', 'Etsy quantity 0; unavailable, sale unknown', listing)
        else:
            out[iid] = ('active', 'Etsy listing active', listing)
    return out, error
