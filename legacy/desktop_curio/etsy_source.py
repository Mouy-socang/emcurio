"""
Curio · Etsy 货源（Etsy Open API v3）

.env 里需要：
  ETSY_API_KEY=...        # Keystring
  ETSY_SHARED_SECRET=...  # Shared Secret（新版接口要求 x-api-key 写成 keystring:secret；没有也会先试只用 keystring）

只用标准库 + requests，兼容 macOS 自带 Python 3.9。
"""
import os, time
from datetime import datetime, timezone

import requests

API = "https://openapi.etsy.com/v3/application"
ETSY_KEYWORDS = ["Meissen", "KPM Berlin"]
ETSY_MAX_PER_KEYWORD = 300      # 每个关键词每次最多取多少条（按最新上架）
ETSY_CHECK_PER_RUN = 300        # 每次最多检查多少条"这次没出现"的商品是否下架

_header_cache = {}


def enabled():
    return bool(os.environ.get("ETSY_API_KEY"))


def _candidates():
    key = os.environ.get("ETSY_API_KEY", "").strip()
    sec = os.environ.get("ETSY_SHARED_SECRET", "").strip()
    out = []
    if key and sec:
        out.append(f"{key}:{sec}")
    if key:
        out.append(key)
    return out


def _get(path, params, log):
    """带鉴权的 GET；自动选能用的 x-api-key 写法，处理 429。"""
    keys = [_header_cache["key"]] if "key" in _header_cache else _candidates()
    last = None
    for k in keys:
        for attempt in range(3):
            try:
                r = requests.get(API + path, headers={"x-api-key": k}, params=params, timeout=30)
            except requests.RequestException as e:
                log(f"  Etsy 网络错误：{e}"); return None
            if r.status_code == 429:
                time.sleep(5 * (attempt + 1)); continue
            break
        if r.status_code in (401, 403):
            last = r; continue
        if r.status_code != 200:
            log(f"  Etsy 出错 ({r.status_code}): {r.text[:300]}"); return None
        _header_cache["key"] = k
        return r.json()
    if last is not None:
        log(f"  Etsy 鉴权失败 ({last.status_code}): {last.text[:200]} → 检查 .env 里的 ETSY_API_KEY / ETSY_SHARED_SECRET，"
            "以及 Etsy 后台这把 key 是否已是 Active")
    return None


def _price(p):
    p = p or {}
    try:
        return float(p.get("amount", 0)) / float(p.get("divisor", 1) or 1), p.get("currency_code", "EUR")
    except (TypeError, ValueError):
        return 0.0, p.get("currency_code", "EUR")


def _ts(v):
    try:
        return datetime.fromtimestamp(int(v), tz=timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return ""


def _image(listing):
    imgs = listing.get("images") or []
    if not imgs:
        return ""
    imgs = sorted(imgs, key=lambda i: i.get("rank", 99))
    i = imgs[0]
    return i.get("url_fullxfull") or i.get("url_570xN") or i.get("url_170x135") or ""


def parse(listing, keyword):
    price, cur = _price(listing.get("price"))
    return {
        "item_id": f"etsy:{listing['listing_id']}",
        "search": keyword,
        "title": listing.get("title", "") or "",
        "price": price, "currency": cur, "buying": "FIXED_PRICE",
        "url": listing.get("url", "") or f"https://www.etsy.com/listing/{listing['listing_id']}",
        "image": _image(listing),
        "created": _ts(listing.get("original_creation_timestamp") or listing.get("creation_timestamp")),
        "ends": "", "condition": "", "location": "",
        "price_text": f"{price:.2f} {cur}" if cur != "EUR" else "",
        "platform": "etsy", "country": "",
    }


def fetch_batch(ids, log):
    """按 listing_id 批量取详情（含图片和状态），每批最多 100。返回 {id: listing}。"""
    out = {}
    ids = [str(i) for i in ids]
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        data = _get("/listings/batch", {"listing_ids": ",".join(chunk), "includes": "Images"}, log)
        if data is None:
            break
        for l in data.get("results", []) or []:
            out[str(l.get("listing_id"))] = l
        time.sleep(0.2)
    return out


def search(keyword, log, max_items=ETSY_MAX_PER_KEYWORD):
    """按最新上架搜索在售商品，并补上图片。"""
    results, offset = [], 0
    while len(results) < max_items:
        limit = min(100, max_items - len(results))
        data = _get("/listings/active", {"keywords": keyword, "limit": limit, "offset": offset,
                                          "sort_on": "created", "sort_order": "desc"}, log)
        if data is None:
            break
        batch = data.get("results", []) or []
        results.extend(batch)
        if len(batch) < limit:
            break
        offset += limit
        time.sleep(0.2)
    # 搜索结果不带图片 → 批量补
    detail = fetch_batch([l["listing_id"] for l in results], log)
    for l in results:
        d = detail.get(str(l["listing_id"]))
        if d:
            l["images"] = d.get("images") or []
    return [parse(l, keyword) for l in results]


def check_gone(db, now, mark_gone, log):
    """这次没出现在搜索里的 Etsy 商品：批量查状态，不是 active 或查不到 → 下架。"""
    rows = db.execute("""SELECT item_id FROM items WHERE status='active' AND item_id LIKE 'etsy:%'
                         AND last_seen<? ORDER BY COALESCE(checked_at,'') LIMIT ?""",
                      (now, ETSY_CHECK_PER_RUN)).fetchall()
    if not rows:
        return 0
    ids = [r[0].split(":", 1)[1] for r in rows]
    detail = fetch_batch(ids, log)
    if not detail and ids:
        return 0  # 接口出错时不要误判下架
    n = 0
    for lid in ids:
        l = detail.get(lid)
        iid = f"etsy:{lid}"
        if l and l.get("state") == "active" and (l.get("quantity") or 1) > 0:
            db.execute("UPDATE items SET checked_at=? WHERE item_id=?", (now, iid))
        else:
            mark_gone(db, iid, now); n += 1
    return n
