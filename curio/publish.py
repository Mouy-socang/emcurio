"""发布快照与即时隐藏（AD-5、AD-6、AD-13、AD-22；CAP-7、CAP-9）。

- 入选：拣选状态 kept、items.status='active'、非 Kleinanzeigen、欧元标价的货；商品池按墨墨留下时间新到旧。
- 榜：`publish_rank` 表里墨墨指定的恰好 10 件，且都须在入选集合内；不足 10 件拒绝生成（FR-16）。
- 白名单（AD-6）：快照只含 contract/schemas/snapshot.v1 里的字段；卖家原文先清洗（去平台名、商品号、卖家/店铺、链接）。
- 图片（AD-13）：用系统 `sips` 转成最长边 1600 与 600 的 JPEG，文件名 = 内容 sha256 前 16 位 + .jpg，
  放在 curio.db 同目录的 publish_images/；同步模块提交快照前先上传、经服务器确认（curio/sync）。
- 序号（AD-22）：发布编号与 hide_seq 共用 kv `publish_seq` 计数器，在生成快照的同一写事务内分配；
  快照的隐藏水位 = 本次发布编号（此前分配的 hide_seq 都已反映在快照内容里）。同一时刻只允许一个发布（kv 发布锁，
  处理每件货时续期，入队前再核对仍由本次发布持有）。
- 已送达与待送达：快照入队时只记为 pending（表 publishes / publish_items）；同步模块收到服务器确认
  （applied，或服务器当前编号 ≥ 本次）才 promote 为已送达并更新曾发布记录（墓碑来源）；
  `scripts/outbox.py drop` 丢掉快照时 discard_pending。表 published 是"可能在客人页上"的货
  （已送达快照 ∪ 待送达快照）及其隐藏状态。
- 隐藏（AD-5）：`reconcile(c, item_id)` 由拣选状态机与采集在状态变更的同一事务里调用：
  published 里的货离开在售（原平台下架、去掉、移出商品池、标记售出、不再是欧元标价）入发件箱 hide，回到在售入 unhide。
  隐藏原因：原平台下架或墨墨标记售出为 sold（客人侧"已售出"），其余为 removed（"已移出"）。
- 千人千面（故事 18，AD-11）：发布时由 curio/personalize 为符合条件的账号算商品池顺序，作为可选字段 `orders`
  随快照下发（账号编号 → 货号前缀）；不进内容指纹（预览按墨墨顺序，AD-28）。客户数记在 publishes.orders，
  03 发布状态显示"已为 N 位客人排好顺序"。超过 SNAPSHOT_MAX 字节时从最长的顺序开始去掉（记警告）；
  准备或排序出错只记警告、本次不带顺序，发布照常。
"""
import hashlib
import ipaddress
import json
import logging
import re
import socket
import sqlite3
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests

from . import sync, triage
from .config import ROOT

log = logging.getLogger(__name__)

RANK_SIZE = 10
TOMBSTONE_DAYS = 90
LOCK_SECONDS = 1800
SIZES = ((1600, 'full'), (600, 'thumb'))
JPEG_QUALITY = '82'
MAX_DOWNLOAD = 25_000_000
MAX_IMAGES = 16
SNAPSHOT_MAX = sync.MAX_PAYLOAD - 20_000   # 带个性化顺序的快照上限（字节，同 sync.wire 的口径；给信封留出余量）
CODE_RE = re.compile(r'CO-[0-9]{4,8}')
ACCOUNT_RE = re.compile(r'[0-9a-f]{32}')
# 价格区间（欧元，左闭右开）——墨墨 2026-11-03 定。
BANDS = (('lt200', 0, 200), ('200-500', 200, 500), ('500-1000', 500, 1000), ('gte1000', 1000, None))
BAND_LABELS = {'lt200': '€200 以下', '200-500': '€200–500', '500-1000': '€500–1000', 'gte1000': '€1000 以上'}

ELIGIBLE_SQL = """SELECT i.*, k.code, k.seq code_seq, t.updated_at kept_at
    FROM triage t JOIN items i ON i.item_id=t.item_id JOIN item_codes k ON k.item_id=i.item_id
    WHERE t.state='kept' AND i.status='active' AND i.platform!='kleinanzeigen'
      AND UPPER(COALESCE(NULLIF(i.currency,''),'EUR'))='EUR' AND i.listed_price IS NOT NULL AND i.listed_price>=0
    ORDER BY t.updated_at DESC, k.seq DESC"""


class PublishError(ValueError):
    """发布被拒绝（中文说明）。"""


class PublishBusy(PublishError):
    """另一个发布正在进行。"""


class PublishStale(PublishError):
    """预览之后榜或内容有变化（须重新预览）。"""


def now_z():
    return sync.utc_now()


def ensure(c):
    c.executescript('''
    CREATE TABLE IF NOT EXISTS publish_rank(pos INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE);
    CREATE TABLE IF NOT EXISTS publish_images(source TEXT NOT NULL, size INTEGER NOT NULL, name TEXT NOT NULL,
        width INTEGER NOT NULL, height INTEGER NOT NULL, made_at TEXT NOT NULL, PRIMARY KEY(source,size));
    -- 可能在客人页上的货（已送达快照 ∪ 待送达快照）：hidden / hide_seq 记录之后推送过的隐藏状态。
    CREATE TABLE IF NOT EXISTS published(code TEXT PRIMARY KEY, item_id TEXT NOT NULL, publish_no INTEGER NOT NULL,
        hidden INTEGER NOT NULL DEFAULT 0, hide_seq INTEGER, reason TEXT, changed_at TEXT);
    -- 每份快照（待送达 / 已送达）包含的货号。
    CREATE TABLE IF NOT EXISTS publish_items(publish_no INTEGER NOT NULL, code TEXT NOT NULL, item_id TEXT NOT NULL,
        PRIMARY KEY(publish_no, code));
    -- 曾发布过的货（墓碑来源）：left_at 为离开商品池的时间，回到商品池时清空。
    CREATE TABLE IF NOT EXISTS publish_history(code TEXT PRIMARY KEY, item_id TEXT NOT NULL, first_no INTEGER NOT NULL,
        last_no INTEGER NOT NULL, left_at TEXT, left_status TEXT);
    -- status：pending（已入队）、delivered（服务器已确认）、superseded（被更新的已送达快照取代）、dropped（发件箱里丢弃）。
    CREATE TABLE IF NOT EXISTS publishes(publish_no INTEGER PRIMARY KEY, event_id TEXT NOT NULL, created_at TEXT NOT NULL,
        rank TEXT NOT NULL, pool_size INTEGER NOT NULL, tombstones INTEGER NOT NULL, images INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending', tomb_json TEXT NOT NULL DEFAULT '[]');
    ''')
    # 故事 11：发布当时的内容指纹（status_fingerprint），用于"有未发布的修改"。
    # 故事 18：这份快照里有个性化顺序的账号数（orders）。
    cols = {r[1] for r in c.execute('PRAGMA table_info(publishes)')}
    for col, decl in (('status_fp', 'TEXT'), ('orders', 'INTEGER NOT NULL DEFAULT 0')):
        if col not in cols:
            try:
                c.execute(f'ALTER TABLE publishes ADD COLUMN {col} {decl}')
            except sqlite3.OperationalError as e:
                if 'duplicate column' not in str(e):
                    raise


# ---------- 入选与榜 ----------

def eligible(c):
    """可发布的货（商品池顺序）。"""
    return [dict(r) for r in c.execute(ELIGIBLE_SQL)]


def why_not(c, code):
    """货号不能上榜/发布的原因（中文）；可以则 None。"""
    row = c.execute('''SELECT i.*, t.state FROM item_codes k JOIN items i ON i.item_id=k.item_id
        LEFT JOIN triage t ON t.item_id=k.item_id WHERE k.code=?''', (code,)).fetchone()
    if row is None:
        return f'{code} 不存在'
    if row['platform'] == 'kleinanzeigen':
        return f'{code} 是 Kleinanzeigen 的货，不能上榜'
    if row['state'] != 'kept':
        return f'{code} 当前是「{triage.LABELS.get(row["state"], row["state"])}」，不在商品池'
    if row['status'] != 'active':
        return f'{code} 原平台已下架'
    if not _priced(row):
        return f'{code} 没有欧元标价，暂不能发布'
    return None


def _priced(row):
    """欧元标价且价格有效（与 ELIGIBLE_SQL 一致）。"""
    price = row['listed_price']
    return (row['currency'] or 'EUR').upper() == 'EUR' and price is not None and price >= 0


def rank(c):
    return [r[0] for r in c.execute('SELECT code FROM publish_rank ORDER BY pos')]


def set_rank(c, codes, keep_stale=False):
    """设置榜（按名次，至多 10 件，须都在商品池内）。不提交。
    keep_stale：已在榜上、之后下架或不再合格的货允许原样留在榜里（后台排序时不因它卡住；发布时仍会拒绝）。"""
    codes = [str(x).strip().upper() for x in codes]
    stale = set(rank(c)) if keep_stale else set()
    if len(codes) > RANK_SIZE:
        raise PublishError(f'榜最多 {RANK_SIZE} 件')
    for code in codes:
        if not CODE_RE.fullmatch(code):
            raise PublishError(f'货号格式不对：{code[:20]}')
    if len(set(codes)) != len(codes):
        raise PublishError('榜里有重复的货号')
    for code in codes:
        reason = None if code in stale else why_not(c, code)
        if reason:
            raise PublishError(reason)
    c.execute('DELETE FROM publish_rank')
    c.executemany('INSERT INTO publish_rank(pos,code) VALUES(?,?)', list(enumerate(codes, 1)))
    return codes


# ---------- 卖家原文清洗与筛选字段 ----------

_URL = re.compile(r'(?i)\b(?:https?://|www\.)\S+')
_EMAIL = re.compile(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+')
# 平台名连同所在的整个词一起去掉（含 eBayer、Kleinanzeigenmarkt、Etsyshop、ebay.de 等复合写法）。
_PLATFORM = re.compile(r'(?i)[\w.-]*(?:ebay[\s-]*kleinanzeigen|kleinanzeigen|ebay|etsy)[\w.-]*')
_ITEM_NO = re.compile(r'(?i)\b(?:artikel[\s-]*(?:nummer|nr\.?)|art\.?[\s-]*nr\.?|item[\s-]*(?:number|no\.?|#)|'
                      r'listing[\s-]*(?:id|#|number)|anzeigen?[\s-]*(?:id|nr\.?|nummer))\s*[:#.]?\s*[\w-]*\d[\w-]*')
_LONG_NUM = re.compile(r'\b\d{9,}\b')            # 平台商品号（eBay 12 位、Etsy 9–10 位、Kleinanzeigen 10 位）
_SELLER = re.compile(r'(?i)\b(?:verkäufer(?:in)?|seller|shop|store|händler(?:in)?|anbieter(?:in)?|'
                     r'shopname|shop[\s-]*name)\s*[:：]\s*@?[\w.\-]+')
_MY_SHOP = re.compile(r'(?i)\b(?:in\s+)?(?:meinem|meinen|unserem|unseren|my|our)\s+(?:shop|store|laden|etsy[\s-]*shop)\b')
_HANDLE = re.compile(r'(?<![\w.])@[\w.\-]{2,}')


def clean_seller_text(text, limit=5000):
    """AD-6：卖家原文进入快照前去掉平台名、商品号、卖家名/店铺与链接（其余原文照录，不翻译不摘要）。"""
    text = text or ''
    for pattern in (_URL, _EMAIL, _SELLER, _MY_SHOP, _ITEM_NO, _PLATFORM, _LONG_NUM, _HANDLE):
        text = pattern.sub(' ', text)
    lines = [re.sub(r'[ \t]{2,}', ' ', line).strip(' \t·|-–—:：,;') for line in text.splitlines()]
    text = '\n'.join(lines)
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    return text[:limit].rstrip()


def price_band(price):
    for key, low, high in BANDS:
        if price >= low and (high is None or price < high):
            return key
    return BANDS[0][0]


def _segments(std):
    try:
        return json.loads(std['segments'] or '{}') if std is not None else {}
    except (TypeError, ValueError):
        return {}


def _lang(text):
    from .store import guess_lang
    return guess_lang(text)


# ---------- 图片：sips 转码，内容哈希命名 ----------

def _sips_dims(path, sips):
    try:
        r = subprocess.run([sips, '-g', 'pixelWidth', '-g', 'pixelHeight', str(path)],
                           capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        raise PublishError('读取图片超时') from None
    w = re.search(r'pixelWidth:\s*(\d+)', r.stdout)
    h = re.search(r'pixelHeight:\s*(\d+)', r.stdout)
    if r.returncode != 0 or not w or not h:
        raise PublishError('图片无法读取')
    return int(w.group(1)), int(h.group(1))


def _segments_of(data):
    """JPEG 标记段遍历：产出 (marker, start, end)，到 SOS 为止（SOS 段 end 为文件末尾）。坏文件抛 PublishError。"""
    if not data.startswith(b'\xff\xd8'):
        raise PublishError('不是 JPEG')
    i = 2
    while True:
        if i + 4 > len(data) or data[i] != 0xFF:
            raise PublishError('JPEG 结构损坏')
        marker = data[i + 1]
        if marker == 0xFF:              # 填充字节
            i += 1
            continue
        if marker == 0xDA:              # SOS：其后是图像数据
            yield marker, i, len(data)
            return
        length = int.from_bytes(data[i + 2:i + 4], 'big')
        if length < 2 or i + 2 + length > len(data):
            raise PublishError('JPEG 结构损坏')
        yield marker, i, i + 2 + length
        i += 2 + length


def jpeg_orientation(data):
    """读源 JPEG 的 EXIF 方向（1–8）；没有或读不出时为 1。"""
    try:
        for marker, start, end in _segments_of(data):
            seg = data[start + 4:end]
            if marker == 0xE1 and seg.startswith(b'Exif\x00\x00'):
                t = seg[6:]
                order = 'little' if t[:2] == b'II' else 'big'
                off = int.from_bytes(t[4:8], order)
                for k in range(int.from_bytes(t[off:off + 2], order)):
                    e = off + 2 + 12 * k
                    if int.from_bytes(t[e:e + 2], order) == 0x0112:
                        v = int.from_bytes(t[e + 8:e + 10], order)
                        return v if 1 <= v <= 8 else 1
                return 1
    except (PublishError, ValueError, IndexError):
        pass
    return 1


STRIP_MARKERS = {0xE1, 0xED, 0xFE}     # APP1（EXIF/XMP：GPS、序列号、方向）、APP13（IPTC）、COM


def strip_metadata(data):
    """去掉 APP1 / APP13 / COM 段（保留 JFIF 与色彩配置），图像数据原样。"""
    out = bytearray(b'\xff\xd8')
    for marker, start, end in _segments_of(data):
        if marker not in STRIP_MARKERS:
            out += data[start:end]
    return bytes(out)


# EXIF 方向 → sips 依次执行的操作（-r 为顺时针旋转）。
_ORIENT = {2: [['-f', 'horizontal']], 3: [['-r', '180']], 4: [['-f', 'vertical']],
           5: [['-r', '90'], ['-f', 'horizontal']], 6: [['-r', '90']],
           7: [['-r', '270'], ['-f', 'horizontal']], 8: [['-r', '270']]}


def transcode(src, longest, out_dir, sips='sips'):
    """把 src 转成最长边不超过 longest 的 JPEG（不放大），按 EXIF 方向转正，去掉 EXIF 等元数据，
    按内容哈希命名存进 out_dir。返回 (name, w, h)，w/h 为转正后的显示尺寸。"""
    src = Path(src)
    w, h = _sips_dims(src, sips)
    orientation = jpeg_orientation(src.read_bytes()[:262144])
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=out_dir) as tmp:
        out = Path(tmp) / 'out.jpg'
        cmd = [sips, '-s', 'format', 'jpeg', '-s', 'formatOptions', JPEG_QUALITY]
        if max(w, h) > longest:
            cmd += ['-Z', str(longest)]
        try:
            r = subprocess.run(cmd + [str(src), '--out', str(out)], capture_output=True, timeout=120)
        except subprocess.TimeoutExpired:
            raise PublishError('图片转码超时') from None
        if r.returncode != 0 or not out.is_file():
            raise PublishError('图片转码失败')
        for op in _ORIENT.get(orientation, []):
            try:
                r = subprocess.run([sips] + op + [str(out)], capture_output=True, timeout=120)
            except subprocess.TimeoutExpired:
                raise PublishError('图片转正超时') from None
            if r.returncode != 0:
                raise PublishError('图片转正失败')
        data = strip_metadata(out.read_bytes())
        out.write_bytes(data)
        ow, oh = _sips_dims(out, sips)
        name = hashlib.sha256(data).hexdigest()[:16] + '.jpg'
        final = out_dir / name
        if not final.is_file():
            out.replace(final)
    return name, ow, oh


# 只从平台图片 CDN 下载（库里 eBay 货的图几乎都在 i.ebayimg.com；Etsy 为 i.etsystatic.com；其余是卖家自建站，不下载）。
IMAGE_HOSTS = ('i.ebayimg.com', 'i.etsystatic.com')
MAX_REDIRECTS = 3
_MAGIC = (b'\xff\xd8\xff', b'\x89PNG\r\n\x1a\n', b'GIF87a', b'GIF89a')


def _image_magic(data):
    return data.startswith(_MAGIC) or (data[:4] == b'RIFF' and data[8:12] == b'WEBP')


def _check_host(url):
    parts = urlsplit(url)
    if parts.scheme != 'https' or parts.hostname not in IMAGE_HOSTS or parts.username or parts.password:
        raise PublishError('图片地址不在允许的平台图片站')
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or 443, proto=socket.IPPROTO_TCP)
    except OSError:
        raise PublishError('图片站无法解析') from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise PublishError('图片站解析到内网地址，已拒绝')


def _download(url):
    """下载卖家图片：只 https、只允许平台图片 CDN（每次跳转都重新核对）、拒绝解析到内网/本机的地址，
    须 image/* 且内容是 JPEG/PNG/WebP/GIF；限大小与时间。返回 bytes。"""
    for _ in range(MAX_REDIRECTS + 1):
        _check_host(url)
        with requests.get(url, timeout=30, stream=True, allow_redirects=False,
                          headers={'User-Agent': 'Curiobject/1.0'}) as r:
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get('location'):
                url = urljoin(url, r.headers['location'])
                continue
            if r.status_code != 200:
                raise PublishError(f'图片下载失败（HTTP {r.status_code}）')
            if not (r.headers.get('content-type') or '').lower().startswith('image/'):
                raise PublishError('下载到的不是图片')
            chunks, size = [], 0
            for chunk in r.iter_content(65536):
                size += len(chunk)
                if size > MAX_DOWNLOAD:
                    raise PublishError('图片过大')
                chunks.append(chunk)
        data = b''.join(chunks)
        if not _image_magic(data):
            raise PublishError('下载到的不是图片')
        return data
    raise PublishError('图片地址跳转过多')


def _source_path(src, root):
    """本机已下载的图（/archive/images/<sha>.jpg）返回其路径；远程地址返回 None。"""
    if src.startswith('/archive/images/'):
        return Path(root) / 'data' / 'porcelain' / 'images' / src.rsplit('/', 1)[-1]
    return None


def _cached(c, source, out_dir):
    rows = {r['size']: r for r in c.execute('SELECT * FROM publish_images WHERE source=?', (source,))}
    if all(size in rows and (Path(out_dir) / rows[size]['name']).is_file() for size, _ in SIZES):
        return {label: rows[size] for size, label in SIZES}
    return None


def prepare_images(store, items, root=ROOT, fetch=None, sips='sips', heartbeat=None):
    """为这些货的全部图片准备两种尺寸（写锁外进行，已转过且文件还在的跳过）。返回 (尝试过的来源集合, 失败数)。
    heartbeat：每件货处理前调用（续期发布锁）。"""
    from .store import item_images
    out_dir = sync.image_dir(store)
    fetch = fetch or _download
    attempted, failures = set(), 0
    for item in items:
        if heartbeat:
            heartbeat()
        with store.connect() as c:
            sources = item_images(c, item['item_id'], item.get('image'), root)[:MAX_IMAGES]
            todo = [s for s in sources if _cached(c, s['key'], out_dir) is None]
        for s in sources:
            attempted.add(s['key'])
        for s in todo:
            try:
                made = _make(s, root, out_dir, fetch, sips)
            except Exception as e:      # 单张失败只跳过这张
                failures += 1
                log.warning('图片未能转码（%s）', type(e).__name__)
                continue
            with store.connect() as c:
                for size, label in SIZES:
                    name, w, h = made[label]
                    c.execute('INSERT OR REPLACE INTO publish_images(source,size,name,width,height,made_at) '
                              'VALUES(?,?,?,?,?,?)', (s['key'], size, name, w, h, now_z()))
    return attempted, failures


def _make(source, root, out_dir, fetch, sips):
    path = _source_path(source['src'], root)
    with tempfile.TemporaryDirectory() as tmp:
        if path is None:
            path = Path(tmp) / 'src'
            data = fetch(source['src'])
            if not _image_magic(data):
                raise PublishError('下载到的不是图片')
            path.write_bytes(data)
        if not path.is_file():
            raise PublishError('本机图片不存在')
        return {label: transcode(path, size, out_dir, sips) for size, label in SIZES}


# ---------- 快照 ----------

def _cut(value, limit):
    return value[:limit] if isinstance(value, str) else value


def _item_payload(c, item, root, out_dir, attempted, fees=None):
    from .store import item_images
    iid = item['item_id']
    std = c.execute('SELECT * FROM std_names WHERE item_id=?', (iid,)).fetchone()
    named = std is not None and std['status'] == 'named' and bool(std['std_name'])
    seg = _segments(std)
    price = float(item['listed_price'])
    std_name = _cut(std['std_name'], 200) if named else ''
    out = {'code': item['code'], 'std_name': std_name,
           'price_eur': int(price) if price.is_integer() else round(price, 2), 'price_band': price_band(price)}
    if named:
        out['title'] = std_name
    else:
        title = clean_seller_text(item['title'], 300) or item['code']
        out['title'] = title
        lang = _lang(title)
        if lang:
            out['title_lang'] = lang
    if std is not None and std['brand']:
        out['brand'] = _cut(std['brand'], 60)
    if std is not None and std['function_id'] and seg.get('function'):
        out['form_id'], out['form'] = _cut(std['function_id'], 60), _cut(seg['function'], 100)
    elif std is not None and std['form_id'] and seg.get('form'):
        out['form_id'], out['form'] = _cut(std['form_id'], 60), _cut(seg['form'], 100)
    if std is not None and std['pattern_id'] and seg.get('pattern'):
        out['pattern_id'], out['pattern'] = _cut(std['pattern_id'], 60), _cut(seg['pattern'], 100)
    sources = item_images(c, iid, item['image'], root)[:MAX_IMAGES]
    saved = dict(c.execute('SELECT image_key,text FROM alt_texts WHERE item_id=?', (iid,)).fetchall())
    images = []
    for s in sources:
        made = _cached(c, s['key'], out_dir)
        if made is None:
            if s['key'] not in attempted:
                raise PublishError('准备图片期间商品有变化，请重新发布')
            continue            # 这张转码失败：跳过
        images.append((s['key'], made))
    # 默认替代文字"标准名称 · 第 N 张 / 共 M 张"；没有标准名称时用标题。
    name = std_name or out['title']
    out['images'] = [{'name': m['full']['name'], 'w': m['full']['width'], 'h': m['full']['height'],
                      'thumb': m['thumb']['name'], 'tw': m['thumb']['width'], 'th': m['thumb']['height'],
                      'alt': _cut(saved.get(key) or f'{name} · 第 {n} 张 / 共 {len(images)} 张', 500)}
                     for n, (key, m) in enumerate(images, 1)]
    out['seller_text'] = clean_seller_text(item['description'])
    lang = _lang(out['seller_text'])
    if lang and out['seller_text']:
        out['seller_lang'] = lang
    comment = c.execute('SELECT text FROM comments WHERE item_id=?', (iid,)).fetchone()
    if comment and comment[0].strip():
        out['comment'] = comment[0][:5000]
    # CAP-10：预估费用档位、金额与档位名称（手动改档优先；请咨询时不带金额与名称）。推断理由不上线。
    fee = fee_info(c, item, fees)
    out['fee_tier'] = fee['tier']
    if fee['amount'] is not None:
        out['fee_amount'] = int(fee['amount'])
        out['fee_label'] = _cut(fee['label'], 60)
    return out


def fee_info(c, item, fees=None):
    from .store import fee_info as info
    return info(c, item, fees)


def _fees(root):
    from .config import fees_for
    return fees_for(root)


FEE_TIERS = ('A', 'B', 'C', 'D')


def check_fees(payload):
    """快照里费用与汇率字段的一致性（Schema 表达不了的部分，服务器 snapshot.check_snapshot 同样核对）：
    A–D 必带 0–10000 的整数金额；ask 不带金额与名称；有金额必有档位；汇率与日期同时出现、汇率 1–50。不合抛 PublishError。"""
    for it in payload['items']:
        tier, amount = it.get('fee_tier'), it.get('fee_amount')
        if tier is None and (amount is not None or 'fee_label' in it):
            raise PublishError(f'{it["code"]} 有费用金额却没有档位')
        if tier == 'ask' and (amount is not None or 'fee_label' in it):
            raise PublishError(f'{it["code"]} 请咨询的货不应带费用金额')
        if tier in FEE_TIERS and not (type(amount) is int and 0 <= amount <= 10000):
            raise PublishError(f'{it["code"]} 费用金额无效')
    rate, date = payload.get('fx_rate'), payload.get('fx_date')
    if (rate is None) != (date is None):
        raise PublishError('汇率与汇率日期须同时提供')
    if rate is not None and not (isinstance(rate, (int, float)) and 1 <= rate <= 50):
        raise PublishError('汇率超出合理范围')


def fx_fields(c):
    """AD-14：快照里的人民币参考汇率与日期（kv 里最近一次成功取到的）；从未取到时为空 dict。"""
    from . import fx
    got = fx.current(c)
    return {'fx_rate': got[0], 'fx_date': got[1]} if got else {}


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def _tombstones(c, pool_codes, now):
    """墓碑：曾送达、已离开商品池 90 天内的货号；加上此刻可能还在客人页上（published）却不在新商品池的货号。"""
    cutoff = _iso(now - TOMBSTONE_DAYS * 86400)
    out = {}
    for r in c.execute('SELECT * FROM publish_history WHERE left_at IS NOT NULL AND left_at>=?', (cutoff,)):
        if r['code'] not in pool_codes:
            out[r['code']] = {'code': r['code'], 'status': r['left_status'], 'at': r['left_at']}
    for r in c.execute('''SELECT p.*, i.status item_status, i.currency, i.listed_price FROM published p
            LEFT JOIN items i ON i.item_id=p.item_id'''):
        if r['code'] in pool_codes or r['code'] in out:
            continue
        hist = c.execute('SELECT left_at FROM publish_history WHERE code=?', (r['code'],)).fetchone()
        if hist is not None and hist[0] is not None:
            continue
        if r['hidden'] and r['reason'] in ('sold', 'removed'):
            status = r['reason']
        else:
            status = 'sold' if r['item_status'] != 'active' or triage.is_sold(c, r['item_id']) else 'removed'
        at = r['changed_at'] if r['hidden'] and r['changed_at'] else _iso(now)
        out[r['code']] = {'code': r['code'], 'status': status, 'at': at}
    return [out[k] for k in sorted(out)]


def check_rank(c):
    """榜须恰好 10 件且都能发布；否则抛 PublishError。返回榜（货号按名次）。"""
    ranked = rank(c)
    if len(ranked) < RANK_SIZE:
        raise PublishError(f'榜需满 {RANK_SIZE} 件才能发布（现在 {len(ranked)} 件）')
    for pos, code in enumerate(ranked, 1):
        reason = why_not(c, code)
        if reason:
            raise PublishError(f'榜里第 {pos} 名 {code} 已不能发布（{reason}），请换一件后再发布')
    return ranked


def content_fingerprint(rank_, pool, items):
    """候选快照的内容指纹（榜、商品池与每件货的全部快照字段；不含发布编号、水位、墓碑与时间）。"""
    text = json.dumps({'rank': rank_, 'pool': pool, 'items': items}, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'))
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def status_fingerprint(c, fees=None):
    """"有未发布的修改"用的轻量指纹（不转码图片）：榜、入选的货号（含顺序）、欧元价、点评与替代文字原文、
    生效的预估费用档位（故事 12：手动改档或推断变化都算修改）。汇率不算（每天会变，不该让榜显示有修改）。"""
    fees = fees or _fees(ROOT)
    rows = c.execute("""SELECT e.*,
        (SELECT text FROM comments m WHERE m.item_id=e.item_id) comment,
        (SELECT json_group_array(json_array(a.image_key, a.text)) FROM
            (SELECT image_key, text FROM alt_texts WHERE item_id=e.item_id ORDER BY image_key) a) alts
        FROM (""" + ELIGIBLE_SQL + """) e""").fetchall()
    pool = []
    for r in rows:
        fee = fee_info(c, r, fees)
        pool.append([r['code'], r['listed_price'], r['comment'], r['alts'], fee['tier'], fee['amount'], fee['label']])
    data = {'rank': rank(c), 'pool': pool}
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def check_orders(payload):
    """快照里个性化顺序的一致性（服务器 snapshot.check_snapshot 同样核对）：账号编号格式、货号是货号格式的文字、
    都在本快照商品池、不重复。不合抛 PublishError。"""
    orders = payload.get('orders')
    if orders is None:
        return
    if not isinstance(orders, dict):
        raise PublishError('个性化顺序格式不对')
    pool = set(payload['pool'])
    for account_id, codes in orders.items():
        if not isinstance(account_id, str) or not ACCOUNT_RE.fullmatch(account_id):
            raise PublishError('个性化顺序里的账号编号格式不对')
        if not isinstance(codes, list) or not all(isinstance(x, str) and CODE_RE.fullmatch(x) for x in codes):
            raise PublishError('个性化顺序里的货号格式不对')
        if len(set(codes)) != len(codes) or not set(codes) <= pool:
            raise PublishError('个性化顺序里有重复或不在商品池的货号')


def _size(payload):
    return len(json.dumps(payload, ensure_ascii=False).encode('utf-8'))      # 与 sync.wire 同一口径


def _with_orders(payload, orders, owner):
    """加上个性化顺序；超过 SNAPSHOT_MAX 字节时从最长的顺序开始去掉，直到放得下（去掉的账号看墨墨顺序）。
    owner：账号编号 → 客户编号。返回带上顺序的客户数（同一客户几个账号算一位）。"""
    if not orders:
        return 0
    payload['orders'] = dict(orders)
    size = _size(payload)
    if size > SNAPSHOT_MAX:
        dropped = 0
        for account_id in sorted(orders, key=lambda a: (-len(orders[a]), a)):
            entry = len(json.dumps({account_id: orders[account_id]}, ensure_ascii=False).encode('utf-8'))   # 约等于它在整份里占的字节
            del payload['orders'][account_id]
            dropped += 1
            size -= entry
            if size <= SNAPSHOT_MAX and _size(payload) <= SNAPSHOT_MAX:
                break
        log.warning('快照加上个性化顺序后超过上限，去掉了 %d 个账号（最长的先去）的顺序', dropped)
        if not payload['orders']:
            del payload['orders']
            return 0
    return len({owner.get(a, a) for a in payload['orders']})


def build_and_enqueue(c, root, out_dir, attempted, now=None, expect_fingerprint=None, fees=None, personal=None):
    """在调用方的写事务里生成完整快照、分配发布编号并入发件箱（记为待送达）。返回摘要。
    expect_fingerprint：预览时的 content_fingerprint；内容已不同则抛 PublishStale（不分配编号、不入队）。
    personal：personalize.prepare 的结果（None 时不带个性化顺序）。"""
    assert c.in_transaction, '快照必须在写事务内生成'
    now = time.time() if now is None else now
    items = eligible(c)
    codes = [it['code'] for it in items]
    ranked = check_rank(c)
    fees = fees or _fees(root)
    payload_items = [_item_payload(c, it, root, out_dir, attempted, fees) for it in items]
    if expect_fingerprint is not None and content_fingerprint(ranked, codes, payload_items) != expect_fingerprint:
        raise PublishStale('内容在预览后有变化，请重新预览')
    at = _iso(now)
    tombstones = _tombstones(c, set(codes), now)
    publish_no = sync.next_publish_seq(c)
    payload = {'schema_version': 1, 'publish_no': publish_no, 'hide_watermark': publish_no,
               'rank': ranked, 'pool': codes, 'items': payload_items, 'tombstones': tombstones, **fx_fields(c)}
    from . import personalize
    try:
        got, owner = personalize.orders(c, items, personal)
    except Exception:           # 千人千面"尽量做到"：出错只记警告，本次不带顺序
        log.warning('个性化顺序排序失败，本次发布不带顺序', exc_info=True)
        got, owner = {}, {}
    n_orders = _with_orders(payload, got, owner)
    check_fees(payload)
    check_orders(payload)
    event_id = sync.enqueue(c, 'snapshot', payload)
    # 待送达快照里的货也算"可能在客人页上"：之后的下架、去掉照样推隐藏。
    c.executemany('INSERT OR IGNORE INTO published(code,item_id,publish_no,changed_at) VALUES(?,?,?,?)',
                  [(it['code'], it['item_id'], publish_no, at) for it in items])
    c.executemany('INSERT INTO publish_items(publish_no,code,item_id) VALUES(?,?,?)',
                  [(publish_no, it['code'], it['item_id']) for it in items])
    images = sum(len(i['images']) for i in payload_items)
    no_images = [i['code'] for i in payload_items if not i['images']]
    c.execute('INSERT INTO publishes(publish_no,event_id,created_at,rank,pool_size,tombstones,images,status,tomb_json,'
              "status_fp,orders) VALUES(?,?,?,?,?,?,?,'pending',?,?,?)",
              (publish_no, event_id, at, json.dumps(ranked), len(codes), len(tombstones), images,
               json.dumps(tombstones, ensure_ascii=False), status_fingerprint(c, fees), n_orders))
    return dict(publish_no=publish_no, event_id=event_id, rank=len(ranked), pool=len(codes),
                tombstones=len(tombstones), images=images, no_images=no_images, published_at=at, orders=n_orders)


def _prune(c):
    """published 只留已送达或待送达快照里的货。"""
    c.execute('DELETE FROM published WHERE code NOT IN (SELECT code FROM publish_items)')


def promote(c, publish_no):
    """服务器已确认这份快照（或已有更新的）：记为已送达，更新曾发布记录，丢掉更早快照的货号表。不提交。"""
    row = c.execute('SELECT * FROM publishes WHERE publish_no=?', (publish_no,)).fetchone()
    if row is None or row['status'] in ('delivered', 'superseded'):
        return False
    c.execute("UPDATE publishes SET status='delivered' WHERE publish_no=?", (publish_no,))
    c.execute("UPDATE publishes SET status='superseded' WHERE publish_no<? AND status IN ('delivered','pending')",
              (publish_no,))
    for r in c.execute('SELECT code,item_id FROM publish_items WHERE publish_no=?', (publish_no,)).fetchall():
        c.execute('''INSERT INTO publish_history(code,item_id,first_no,last_no) VALUES(?,?,?,?)
            ON CONFLICT(code) DO UPDATE SET last_no=excluded.last_no,left_at=NULL,left_status=NULL''',
                  (r['code'], r['item_id'], publish_no, publish_no))
    for t in json.loads(row['tomb_json'] or '[]'):
        item = c.execute('SELECT item_id FROM published WHERE code=?', (t['code'],)).fetchone()
        c.execute('''INSERT INTO publish_history(code,item_id,first_no,last_no,left_at,left_status) VALUES(?,?,?,?,?,?)
            ON CONFLICT(code) DO UPDATE SET left_at=COALESCE(left_at,excluded.left_at),
            left_status=COALESCE(left_status,excluded.left_status)''',
                  (t['code'], item[0] if item else '', publish_no, publish_no, t['at'], t['status']))
    c.execute('DELETE FROM publish_items WHERE publish_no<?', (publish_no,))
    _prune(c)
    return True


def on_snapshot_ack(c, payload, ack):
    """同步模块送达快照后调用（同一连接）。applied，或服务器当前编号 ≥ 本次，即视为送达。"""
    no = payload.get('publish_no')
    cur = ack.get('current_publish_no') if isinstance(ack, dict) else None
    if (isinstance(ack, dict) and ack.get('applied') is True) or (type(cur) is int and type(no) is int and cur >= no):
        return promote(c, no)
    return False


def discard_pending(c, publish_no):
    """发件箱里丢掉了这份快照：不再当它会上线。不提交。"""
    c.execute("UPDATE publishes SET status='dropped' WHERE publish_no=? AND status='pending'", (publish_no,))
    c.execute('DELETE FROM publish_items WHERE publish_no=? AND publish_no NOT IN '
              "(SELECT publish_no FROM publishes WHERE status='delivered')", (publish_no,))
    _prune(c)


def candidate(store, root=ROOT, fetch=None, sips='sips'):
    """预览用的候选快照（AD-28）：与发布同样的入选、榜校验、字段白名单与图片（Mac 本地转码，必要时先转码），
    但不分配发布编号、不写发件箱、不改 published / publishes，也不取发布锁。
    返回 (payload, 转码失败张数)；payload 的结构同 snapshot.v1（publish_no、hide_watermark 为 0，无墓碑）。"""
    with store.connect() as c:
        check_rank(c)
        items = eligible(c)
    attempted, failures = prepare_images(store, items, root, fetch, sips)
    with store.connect() as c:
        ranked = check_rank(c)
        items = eligible(c)
        fees = _fees(root)
        payload_items = [_item_payload(c, it, root, sync.image_dir(store), attempted, fees) for it in items]
        fx = fx_fields(c)
    payload = {'schema_version': 1, 'publish_no': 0, 'hide_watermark': 0, 'rank': ranked,
               'pool': [it['code'] for it in items], 'items': payload_items, 'tombstones': [], **fx}
    check_fees(payload)
    return payload, failures


def payload_fingerprint(payload):
    """候选快照（candidate 的 payload）的内容指纹，与发布时 build_and_enqueue 的比较口径相同。"""
    return content_fingerprint(payload['rank'], payload['pool'], payload['items'])


def last_publish(c):
    """最近一次入队（待送达或已送达）的发布；从未发布或都被丢弃时 None。"""
    row = c.execute("SELECT * FROM publishes WHERE status!='dropped' ORDER BY publish_no DESC LIMIT 1").fetchone()
    return dict(row) if row else None


# ---------- 发布锁 ----------

def _held(raw):
    """kv 里的发布锁 → (owner, until)；格式坏掉时视为无锁。"""
    try:
        value = json.loads(raw)
        return str(value['owner']), float(value['until'])
    except (TypeError, ValueError, KeyError, IndexError):
        return None, 0.0


def _lock(store, owner, clock):
    with store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute("SELECT v FROM kv WHERE k='publish_lock'").fetchone()
        if row and _held(row[0])[1] > clock():
            raise PublishBusy('另一个发布正在进行，请稍后再试')
        c.execute("INSERT OR REPLACE INTO kv VALUES('publish_lock',?)",
                  (json.dumps({'owner': owner, 'until': clock() + LOCK_SECONDS}),))


def _check_owner(c, owner):
    row = c.execute("SELECT v FROM kv WHERE k='publish_lock'").fetchone()
    if row is None or _held(row[0])[0] != owner:
        raise PublishBusy('发布锁已被另一个发布接手，本次未发布，请重新发布')


def _refresh(store, owner, clock):
    with store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        _check_owner(c, owner)
        c.execute("UPDATE kv SET v=? WHERE k='publish_lock'",
                  (json.dumps({'owner': owner, 'until': clock() + LOCK_SECONDS}),))


def _unlock(store, owner):
    with store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute("SELECT v FROM kv WHERE k='publish_lock'").fetchone()
        if row is not None and _held(row[0])[0] == owner:
            c.execute("DELETE FROM kv WHERE k='publish_lock'")


REPLY_KEYS = ('publish_no', 'published_at', 'pool', 'image_failures', 'orders')


def request_payload(expect_rank):
    return json.dumps(['publish', list(expect_rank or [])], ensure_ascii=False)


def publish(store, root=ROOT, fetch=None, sips='sips', clock=time.time, expect_rank=None, expect_fingerprint=None,
            request_id=None, gc=None):
    """生成并入队一份完整快照（持发布锁）。返回摘要（no_images：没有可用图片的货号）；
    榜不足、榜里有不能发布的货、另一发布进行中时抛 PublishError。
    后台从预览发布时：expect_rank / expect_fingerprint 为预览所见的榜与内容指纹，入队前（同一写事务）已不同则抛
    PublishStale；request_id 的结果（REPLY_KEYS）与快照在同一事务里记入 triage_requests，重放返回该结果（replayed=True）。
    gc：GoodCustomer 资料库路径（千人千面读需求与喜好；None 时读 config/settings.yaml，没有已关联账号时不读）。"""
    owner = uuid.uuid4().hex
    _lock(store, owner, clock)
    try:
        with store.connect() as c:
            ranked = rank(c)
            if len(ranked) < RANK_SIZE:
                raise PublishError(f'榜需满 {RANK_SIZE} 件才能发布（现在 {len(ranked)} 件）')
            items = eligible(c)
        attempted, failures = prepare_images(store, items, root, fetch, sips,
                                             heartbeat=lambda: _refresh(store, owner, clock))
        from . import personalize
        personal = personalize.prepare(store, gc, root)
        with store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            _check_owner(c, owner)
            if request_id is not None:
                prev = c.execute('SELECT * FROM triage_requests WHERE request_id=?', (request_id,)).fetchone()
                if prev:
                    if prev['payload'] != request_payload(expect_rank):
                        raise PublishError('重复请求内容不同')
                    return dict(json.loads(prev['result']), replayed=True)
            if expect_rank is not None and rank(c) != list(expect_rank):
                raise PublishStale('排序在预览之后有变化，请重新预览后再发布')
            result = build_and_enqueue(c, root, sync.image_dir(store), attempted, clock(), expect_fingerprint,
                                       personal=personal)
            result['image_failures'] = failures
            if request_id is not None:
                c.execute('INSERT INTO triage_requests VALUES(?,?,?,?)',
                          (request_id, request_payload(expect_rank),
                           json.dumps({k: result[k] for k in REPLY_KEYS}, ensure_ascii=False), now_z()))
        return result
    finally:
        _unlock(store, owner)


# ---------- 即时隐藏 ----------

def _has_table(c, name):
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def online_platforms(c):
    """当前有可能在线的货（published 中未隐藏）的平台集合；心跳据此判断哪些平台需要已发布货检查（AD-10）。"""
    if not _has_table(c, 'published'):
        return set()
    return {r[0] for r in c.execute('''SELECT DISTINCT i.platform FROM published p JOIN items i ON i.item_id=p.item_id
        WHERE p.hidden=0 AND i.status='active' ''')}


def reconcile(c, iid):
    """可能在客人页上的货（published）：在售状态变化时（同一事务）入发件箱 hide / unhide。
    返回入队的 action 或 None。不提交。"""
    if not _has_table(c, 'published'):
        return None            # 只建了部分表的连接（测试或迁移）：没有发布过的货
    row = c.execute('''SELECT p.*, t.state, i.status, i.currency, i.listed_price FROM published p
        JOIN item_codes k ON k.code=p.code LEFT JOIN triage t ON t.item_id=p.item_id
        LEFT JOIN items i ON i.item_id=p.item_id WHERE k.item_id=?''', (iid,)).fetchone()
    if row is None:
        return None
    show = row['state'] == 'kept' and row['status'] == 'active' and _priced(row)
    if show != bool(row['hidden']):
        return None
    action = 'unhide' if show else 'hide'
    sold = row['status'] != 'active' or triage.is_sold(c, row['item_id'])
    reason = None if show else ('sold' if sold else 'removed')
    seq = sync.next_publish_seq(c)
    at = now_z()
    payload = {'schema_version': 1, 'code': row['code'], 'hide_seq': seq, 'action': action, 'at': at}
    if reason:
        payload['reason'] = reason
    sync.enqueue(c, 'hide', payload)
    c.execute('UPDATE published SET hidden=?,hide_seq=?,reason=?,changed_at=? WHERE code=?',
              (0 if show else 1, seq, reason, at, row['code']))
    return action
