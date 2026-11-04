"""发布快照、隐藏集合与图片（AD-5、AD-6、AD-13、AD-22）——服务器只读地提供 Mac 推上来的内容。

- 快照：整份原子替换（同一事务里写入新快照、删掉 hide_seq ≤ 新水位的隐藏项）；publish_no ≤ 当前编号的不替换
  （确认里 applied 为 false、带 current_publish_no，不算错误）；引用的图片服务器上缺任何一张即拒收（MissingImages）。
- 隐藏：每个货号只留 hide_seq 最大的一条（hide / unhide）；hide_seq ≤ 当前快照水位的忽略。
- 图片：PUT 上来的 JPEG 核对 sha256 前 16 位与文件名一致后落盘，文件不可变；
  只保留最近 3 份快照（当前 + 之前两次）引用的图与 1 天内新传的图，其余在新快照生效后清理。
- 客人页读取：current_view() 给出当前快照减去隐藏集合后的榜、商品池与筛选数据（解析结果按 publish_no 缓存）。
- 千人千面（AD-11，故事 18）：快照可选的 orders（账号编号 → 货号顺序）由 Mac 算好；服务器只照顺序排，
  View.for_account() 给出该账号的商品池（集合不变 = 商品池 − 隐藏集合；顺序里不在集合的货号忽略，
  集合里不在顺序中的按墨墨顺序排在末尾）。榜、单件页与筛选项不变。
- 心跳（AD-10）：只留 sent_at 最新的一份；heartbeat_stale() 判断已发布货下架检查是否超过 2 小时未成功
  （从未发布过快照时不提示；发布过但从未收到心跳、或某平台没有成功时间时提示）。
- 拍卖榜（故事 30，snapshot.v2）：rank_auction 为拍卖榜货号，这些货在 items 里、不在 pool 里、带 ends；
  View.rank_auction 去掉隐藏的与结束时间已过的（按读取时的服务器时间即时判断，不等下一次发布）。v1 快照没有拍卖榜。
"""
import copy
import datetime
import hashlib
import json
import math
import os
import re
import tempfile
import threading
import time
from pathlib import Path

CODE_RE = re.compile(r'CO-[0-9]{4,8}')
ACCOUNT_RE = re.compile(r'[0-9a-f]{32}')
IMAGE_RE = re.compile(r'[0-9a-f]{16}\.jpg')
TS_RE = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z')
MAX_SEQ = 2 ** 63 - 1
KEEP_SNAPSHOTS = 3
FRESH_IMAGE_SECONDS = 86400
MAX_IMAGE = 8_000_000
BANDS = (('lt200', '€200 以下'), ('200-500', '€200–500'), ('500-1000', '€500–1000'), ('gte1000', '€1000 以上'))
BRAND_ORDER = ('Meissen', 'KPM Berlin')
FEE_TIERS = ('A', 'B', 'C', 'D')
MAX_FEE = 10000
TOMBSTONE_DAYS = 90                 # AD-29：隐藏后单件页保留 90 天
STALE_SECONDS = 2 * 3600            # AD-10：已发布货下架检查超过 2 小时未成功 → 客人页提示


class BadSnapshot(ValueError):
    """快照或隐藏载荷语义不对（中文）。"""


class MissingImages(BadSnapshot):
    """快照引用的图片服务器上没有。"""


def ensure(c):
    c.executescript('''
    CREATE TABLE IF NOT EXISTS snapshots(publish_no INTEGER PRIMARY KEY, watermark INTEGER NOT NULL,
        payload TEXT NOT NULL, received_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS snapshot_images(publish_no INTEGER NOT NULL, name TEXT NOT NULL,
        PRIMARY KEY(publish_no, name));
    CREATE TABLE IF NOT EXISTS hides(code TEXT PRIMARY KEY, hide_seq INTEGER NOT NULL, hidden INTEGER NOT NULL,
        reason TEXT, at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS heartbeat(id INTEGER PRIMARY KEY CHECK(id=1), sent_at TEXT NOT NULL,
        payload TEXT NOT NULL, received_at TEXT NOT NULL);
    ''')


def image_dir(db_path):
    return Path(db_path).parent / 'images'


# ---------- 校验（契约之外的语义检查） ----------

def _seq(value, label, low=1):
    if type(value) is not int or not low <= value <= MAX_SEQ:
        raise BadSnapshot(f'{label} 超出范围')


def _codes(values, label):
    for v in values:
        if not CODE_RE.fullmatch(v):
            raise BadSnapshot(f'{label} 里的货号格式不对')
    if len(set(values)) != len(values):
        raise BadSnapshot(f'{label} 里有重复的货号')


def snapshot_images(payload):
    names = []
    for item in payload['items']:
        for img in item['images']:
            names += [img['name'], img['thumb']]
    return sorted(set(names))


def check_snapshot(payload):
    _seq(payload['publish_no'], 'publish_no')
    _seq(payload['hide_watermark'], 'hide_watermark', 0)
    codes = [it['code'] for it in payload['items']]
    _codes(codes, 'items')
    _codes(payload['pool'], 'pool')
    _codes(payload['rank'], 'rank')
    # 故事 30（v2）：items = pool ∪ rank_auction，两者不相交；拍卖榜的货都带 ends，别的货不带。v1 没有拍卖榜。
    auction = payload.get('rank_auction') or []
    _codes(auction, 'rank_auction')
    if set(auction) & set(payload['pool']):
        raise BadSnapshot('拍卖榜里有在商品池的货')
    if sorted(codes) != sorted(payload['pool'] + auction):
        raise BadSnapshot('pool（与拍卖榜）与 items 不是同一组货')
    if not set(payload['rank']) <= set(payload['pool']):
        raise BadSnapshot('榜里有不在商品池的货')
    for item in payload['items']:
        if (item['code'] in auction) != ('ends' in item):
            raise BadSnapshot('拍卖榜的货须带结束时间，别的货不带')
        if 'ends' in item:
            if not TS_RE.fullmatch(item['ends']):
                raise BadSnapshot('结束时间格式不对')
            try:
                _epoch(item['ends'])
            except ValueError:
                raise BadSnapshot('结束时间无效') from None
    tomb = [t['code'] for t in payload['tombstones']]
    _codes(tomb, 'tombstones')
    if set(tomb) & set(codes):
        raise BadSnapshot('墓碑与商品池重叠')
    for t in payload['tombstones']:
        if not TS_RE.fullmatch(t['at']):
            raise BadSnapshot('墓碑时间格式不对')
    # 故事 18：按账号的顺序——账号编号格式、货号都在本快照商品池、不重复；否则整份拒收。
    orders = payload.get('orders')
    if orders is not None:
        if not isinstance(orders, dict):
            raise BadSnapshot('orders 格式不对')
        pool = set(payload['pool'])
        for account_id, order in orders.items():
            if not ACCOUNT_RE.fullmatch(account_id):
                raise BadSnapshot('orders 里的账号编号格式不对')
            _codes(order, 'orders')
            if not set(order) <= pool:
                raise BadSnapshot('orders 里有不在商品池的货号')
    # 汇率与汇率日期同时出现（AD-14）。
    rate, date = payload.get('fx_rate'), payload.get('fx_date')
    if (rate is None) != (date is None):
        raise BadSnapshot('汇率与汇率日期须同时提供')
    if rate is not None:
        if not (isinstance(rate, (int, float)) and math.isfinite(rate) and 1 <= rate <= 50):
            raise BadSnapshot('汇率无效')
        try:
            datetime.date.fromisoformat(date)
        except ValueError:
            raise BadSnapshot('汇率日期无效') from None
    for item in payload['items']:
        price = item['price_eur']
        if not (isinstance(price, (int, float)) and math.isfinite(price) and price >= 0):
            raise BadSnapshot('价格无效')
        # 故事 12：A–D 档必带欧元整数金额，请咨询不带金额；旧快照可以没有档位。
        tier, amount = item.get('fee_tier'), item.get('fee_amount')
        if tier is None and amount is not None:
            raise BadSnapshot('有费用金额却没有档位')
        if tier is None and 'fee_label' in item:
            raise BadSnapshot('有费用金额却没有档位')
        if tier == 'ask' and (amount is not None or 'fee_label' in item):
            raise BadSnapshot('请咨询的货不应带费用金额')
        if tier in FEE_TIERS and not (type(amount) is int and 0 <= amount <= MAX_FEE):
            raise BadSnapshot('费用金额无效')
        for img in item['images']:
            if not (IMAGE_RE.fullmatch(img['name']) and IMAGE_RE.fullmatch(img['thumb'])):
                raise BadSnapshot('图片名格式不对')
            for key in ('w', 'h', 'tw', 'th'):
                if not 1 <= img[key] <= 20000:
                    raise BadSnapshot('图片尺寸无效')


def check_hide(payload):
    _seq(payload['hide_seq'], 'hide_seq')
    if not CODE_RE.fullmatch(payload['code']) or not TS_RE.fullmatch(payload['at']):
        raise BadSnapshot('隐藏载荷格式不对')


# ---------- 写入（调用方已开 BEGIN IMMEDIATE） ----------

def current(c):
    """(publish_no, watermark)；从未发布时 (0, 0)。"""
    row = c.execute('SELECT publish_no, watermark FROM snapshots ORDER BY publish_no DESC LIMIT 1').fetchone()
    return (row[0], row[1]) if row else (0, 0)


def apply_snapshot(c, payload, img_dir, received_at, now):
    """原子替换。返回 (ack 附加字段, 待删除的图片文件列表)。缺图抛 MissingImages（调用方回滚、不记事件）。"""
    cur_no, _ = current(c)
    if payload['publish_no'] <= cur_no:
        return {'applied': False, 'current_publish_no': cur_no}, []
    names = snapshot_images(payload)
    missing = [n for n in names if not (Path(img_dir) / n).is_file()]
    if missing:
        raise MissingImages(f'快照引用的 {len(missing)} 张图片尚未上传（如 {missing[0]}），已拒收')
    no, mark = payload['publish_no'], payload['hide_watermark']
    c.execute('INSERT INTO snapshots(publish_no,watermark,payload,received_at) VALUES(?,?,?,?)',
              (no, mark, json.dumps(payload, ensure_ascii=False, separators=(',', ':')), received_at))
    c.executemany('INSERT OR IGNORE INTO snapshot_images(publish_no,name) VALUES(?,?)', [(no, n) for n in names])
    # AD-22：保留 hide_seq 大于新快照水位的隐藏项。
    c.execute('DELETE FROM hides WHERE hide_seq<=?', (mark,))
    keep = [r[0] for r in c.execute('SELECT publish_no FROM snapshots ORDER BY publish_no DESC LIMIT ?',
                                    (KEEP_SNAPSHOTS,))]
    marks = ','.join('?' * len(keep))
    c.execute(f'DELETE FROM snapshots WHERE publish_no NOT IN ({marks})', keep)
    c.execute(f'DELETE FROM snapshot_images WHERE publish_no NOT IN ({marks})', keep)
    referenced = {r[0] for r in c.execute('SELECT name FROM snapshot_images')}
    stale = []
    if Path(img_dir).is_dir():
        for path in Path(img_dir).iterdir():
            if IMAGE_RE.fullmatch(path.name) and path.name not in referenced:
                try:
                    if now - path.stat().st_mtime > FRESH_IMAGE_SECONDS:
                        stale.append(path)
                except OSError:
                    pass
    return {'applied': True, 'current_publish_no': no}, stale


def apply_hide(c, payload):
    """返回是否生效。"""
    _, mark = current(c)
    seq = payload['hide_seq']
    if seq <= mark:
        return False
    row = c.execute('SELECT hide_seq FROM hides WHERE code=?', (payload['code'],)).fetchone()
    if row and row[0] >= seq:
        return False
    c.execute('INSERT OR REPLACE INTO hides(code,hide_seq,hidden,reason,at) VALUES(?,?,?,?,?)',
              (payload['code'], seq, 1 if payload['action'] == 'hide' else 0, payload.get('reason'), payload['at']))
    return True


# ---------- 心跳（AD-10） ----------

def _epoch(text):
    return datetime.datetime.fromisoformat(text.replace('Z', '+00:00')).timestamp()


HEARTBEAT_PLATFORMS = {'ebay', 'etsy'}


def check_heartbeat(payload):
    names = [p['platform'] for p in payload['platforms']]
    if len(set(names)) != len(names) or set(names) != HEARTBEAT_PLATFORMS:
        raise BadSnapshot('心跳须恰好包含 ebay 与 etsy 各一项')
    stamps = [payload['sent_at']] + [p['last_success_at'] for p in payload['platforms'] if 'last_success_at' in p]
    for t in stamps:
        if not TS_RE.fullmatch(t):
            raise BadSnapshot('心跳时间格式不对')
        try:
            _epoch(t)
        except ValueError:
            raise BadSnapshot('心跳时间无效') from None
    sent = _epoch(payload['sent_at'])
    if any(_epoch(t) > sent for t in stamps[1:]):
        raise BadSnapshot('成功时间晚于发送时间')


def apply_heartbeat(c, payload, received_at, now):
    """只保留 sent_at 最新的一份（晚到的旧心跳不覆盖）。例外：已存的 sent_at 比服务器现在还晚（Mac 时钟曾经快了、
    已被校正），则以后到的为准，免得卡死在一个"未来"的心跳上。返回是否生效。"""
    row = c.execute('SELECT sent_at FROM heartbeat WHERE id=1').fetchone()
    if row and _epoch(row[0]) > _epoch(payload['sent_at']) and _epoch(row[0]) <= now:
        return False
    c.execute('INSERT OR REPLACE INTO heartbeat(id,sent_at,payload,received_at) VALUES(1,?,?,?)',
              (payload['sent_at'], json.dumps(payload, ensure_ascii=False, separators=(',', ':')), received_at))
    return True


def heartbeat_age(c, now):
    """最早一个平台的最近成功距今多少秒；从未收到心跳或某平台没有成功时间时 None。
    距今 = (Mac 发送时间 − 最早成功时间，两者同一时钟) + (服务器现在 − 服务器收到时间)，不依赖两端时钟一致。"""
    row = c.execute('SELECT sent_at, payload, received_at FROM heartbeat WHERE id=1').fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row[1])
        stamps = [p.get('last_success_at') for p in payload['platforms']]
        if not stamps or None in stamps:
            return None
        earliest = min(_epoch(t) for t in stamps)
        return (_epoch(row[0]) - earliest) + max(0.0, now - _epoch(row[2]))
    except (ValueError, KeyError, TypeError):
        return None


def heartbeat_stale(c, now):
    """客人页是否显示"部分货品状态可能未更新"：发布过快照，且心跳缺失或已发布货检查超过 2 小时未成功。"""
    if current(c)[0] == 0:
        return False
    age = heartbeat_age(c, now)
    return age is None or age > STALE_SECONDS


# ---------- 图片 ----------

def store_image(img_dir, name, data):
    """核对后落盘（已存在则不动）。返回确认（image_ack.v1）。不合格抛 BadSnapshot。"""
    if not IMAGE_RE.fullmatch(name):
        raise BadSnapshot('图片名格式不对')
    if not data.startswith(b'\xff\xd8\xff'):
        raise BadSnapshot('图片不是 JPEG')
    if hashlib.sha256(data).hexdigest()[:16] != name[:16]:
        raise BadSnapshot('图片内容与文件名不符')
    d = Path(img_dir)
    d.mkdir(parents=True, exist_ok=True)
    final = d / name
    if not final.is_file():
        fd, tmp = tempfile.mkstemp(dir=d, suffix='.part')
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, final)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    else:
        os.utime(final)          # 刚被确认过：清理时算新传的
    return {'schema_version': 1, 'name': name, 'bytes': len(data)}


def missing_images(img_dir, names):
    return [n for n in names if not (IMAGE_RE.fullmatch(n) and (Path(img_dir) / n).is_file())]


# ---------- 客人页读取 ----------

_cache = {}
_lock = threading.Lock()


class View:
    """当前快照减去隐藏集合。"""

    def __init__(self, publish_no, received_at, payload, hidden, hidden_at=None, now=None):
        self.publish_no = publish_no
        self.received_at = received_at
        self.items = {it['code']: it for it in payload['items']} if payload else {}
        self.hidden = hidden                         # code -> reason
        self.hidden_at = hidden_at or {}             # code -> 隐藏时间（UTC 文字）
        self.tombstones = {t['code']: t for t in payload['tombstones']} if payload else {}
        self.rank = [self.items[c] for c in (payload['rank'] if payload else []) if c not in hidden]
        self.pool = [self.items[c] for c in (payload['pool'] if payload else []) if c not in hidden]
        # 故事 30：拍卖中（v2）——去掉隐藏的与结束时间已过的（now：Unix 秒，None 为现在）；v1 快照为空。
        now = time.time() if now is None else now
        self.rank_auction = [self.items[c] for c in ((payload.get('rank_auction') or []) if payload else [])
                             if c not in hidden and _live(self.items[c].get('ends'), now)]
        self.fx_rate = payload.get('fx_rate') if payload else None
        self.fx_date = payload.get('fx_date') if payload else None
        self.orders = (payload.get('orders') or {}) if payload else {}

    def for_account(self, account_id):
        """该账号看到的视图：商品池按 Mac 下发的顺序排（AD-11）；没有该账号的顺序时就是本视图（墨墨顺序）。"""
        order = self.orders.get(account_id) if account_id else None
        if not order:
            return self
        pos = {code: i for i, code in enumerate(order)}
        end = len(order)
        view = copy.copy(self)
        # 稳定排序：顺序里没有的货保持墨墨顺序排在末尾；隐藏的货早已不在 self.pool 里。
        view.pool = sorted(self.pool, key=lambda it: pos.get(it['code'], end))
        return view

    def item_state(self, code, now=None):
        """'show' / 'sold' / 'removed' / None（不在快照里，或隐藏、离开已超过 90 天）。now：Unix 秒，None 时不判保留期。"""
        if code in self.items:
            # 故事 30：拍卖货结束时间已过即不在展出（与不在快照里一样，不等下一次发布）。
            ends = self.items[code].get('ends')
            if ends is not None and not _live(ends, time.time() if now is None else now):
                return None
            if code not in self.hidden:
                return 'show'
            if _expired(self.hidden_at.get(code), now):
                return None
            return self.hidden[code] or 'removed'
        if code in self.tombstones:
            t = self.tombstones[code]
            return None if _expired(t['at'], now) else t['status']
        return None


def _live(ends, now):
    """拍卖还没结束（结束时间读不出的不显示）。"""
    try:
        return _epoch(ends) > now
    except (AttributeError, TypeError, ValueError):
        return False


def _expired(at, now):
    if now is None or not at:
        return False
    try:
        t = datetime.datetime.fromisoformat(at.replace('Z', '+00:00')).timestamp()
    except (AttributeError, ValueError):
        return False
    return now - t > TOMBSTONE_DAYS * 86400


def current_view(c, db_key='', now=None):
    """db_key：库的标识（路径），解析缓存按 (库, 编号, 接收时间) 区分。now：Unix 秒（拍卖是否已结束；None 为现在）。"""
    row = c.execute('SELECT publish_no, payload, received_at FROM snapshots ORDER BY publish_no DESC LIMIT 1'
                    ).fetchone()
    hides = c.execute('SELECT code, reason, at FROM hides WHERE hidden=1').fetchall()
    hidden = {r[0]: r[1] for r in hides}
    hidden_at = {r[0]: r[2] for r in hides}
    if row is None:
        return View(0, None, None, hidden, hidden_at, now)
    no = row[0]
    key = (str(db_key), no, row[2])
    with _lock:
        payload = _cache.get(key)
        if payload is None:
            payload = json.loads(row[1])
            if len(_cache) > 8:
                _cache.clear()
            _cache[key] = payload
    return View(no, row[2], payload, hidden, hidden_at, now)


def filter_options(pool):
    """品牌（按出现）、器型、图案（只列商品池里有的）、价格区间（四档），各带件数。"""
    def count(key, label_key=None):
        out = {}
        for it in pool:
            k = it.get(key)
            if k:
                label = it.get(label_key) if label_key else k
                n, _ = out.get(k, (0, label))
                out[k] = (n + 1, label)
        return out
    brands = count('brand')
    order = {b: i for i, b in enumerate(BRAND_ORDER)}
    brand_list = sorted(brands.items(), key=lambda kv: (order.get(kv[0], len(order)), kv[0]))
    forms = sorted(count('form_id', 'form').items(), key=lambda kv: (-kv[1][0], kv[1][1]))
    patterns = sorted(count('pattern_id', 'pattern').items(), key=lambda kv: (-kv[1][0], kv[1][1]))
    bands = {k: 0 for k, _ in BANDS}
    for it in pool:
        bands[it['price_band']] = bands.get(it['price_band'], 0) + 1
    return {
        'brand': [(k, label, n) for k, (n, label) in brand_list],
        'form': [(k, label, n) for k, (n, label) in forms],
        'band': [(k, label, bands[k]) for k, label in BANDS],
        'pattern': [(k, label, n) for k, (n, label) in patterns],
    }


FILTER_KEYS = {'brand': 'brand', 'form': 'form_id', 'band': 'price_band', 'pattern': 'pattern_id'}


def apply_filters(pool, chosen):
    return [it for it in pool if all(it.get(FILTER_KEYS[k]) == v for k, v in chosen.items())]
