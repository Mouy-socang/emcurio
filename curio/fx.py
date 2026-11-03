"""人民币参考汇率（AD-14；CAP-11）：Mac 每天从 ECB EXR 取一次 CNY/EUR 日参考汇率，写进 kv，发布时随快照上线。

- 来源：ECB Data API `EXR/D.CNY.EUR.SP00.A`（1 欧元兑多少人民币，日参考汇率），只取最近一个观测值。
- 每天（柏林日期）最多成功取一次；当天已成功就不再请求——除非取到的还是前一天的观测值：工作日柏林 16:30 之后
  （ECB 约 16:00 CET 发布）再取一次当天的值。失败时沿用最近一次成功的汇率与它原来的日期，
  记一次警告，1 小时后才重试（不每 5 分钟打一次 ECB）。
- kv：`fx_cny_eur` = {"rate": 7.8123, "date": "2026-11-02", "fetched_on": 柏林日期, "fetched_at": "<UTC>"}；
  `fx_attempt` = 最近一次失败的 Unix 秒（成功后删除）。
- 客人页只用快照里的汇率（服务器不取汇率）；汇率日期距今超过 7 天时客人页只显示欧元（server/guest.py）。
- HTTP 只用 requests，带超时；测试注入 get 函数（tests/conftest.py 默认拦截，测试从不连接 ECB）。
"""
import csv
import io
import json
import logging
import math
import re
import time

import requests

from .schedule import berlin, berlin_date

log = logging.getLogger(__name__)

ECB_URL = 'https://data-api.ecb.europa.eu/service/data/EXR/D.CNY.EUR.SP00.A'
ECB_PARAMS = {'lastNObservations': '1', 'format': 'csvdata'}
TIMEOUT = (10, 20)
RETRY_SECONDS = 3600
PUBLISH_AFTER = (16, 30)            # ECB 约 16:00 CET 发布；之后可再取一次当天的值
KV_RATE = 'fx_cny_eur'
KV_ATTEMPT = 'fx_attempt'
_DATE = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}')


class FxError(RuntimeError):
    """汇率取不到或格式不对（中文）。"""


def _get(url, params, timeout):
    """真实 HTTP 请求（测试里被替换）。"""
    return requests.get(url, params=params, timeout=timeout, headers={'Accept': 'text/csv'})


def parse(text):
    """ECB csvdata → (rate, date)。只认 TIME_PERIOD 与 OBS_VALUE 两列；取日期最新的一行。"""
    rows = list(csv.DictReader(io.StringIO(text)))
    best = None
    for r in rows:
        date, value = (r.get('TIME_PERIOD') or '').strip(), (r.get('OBS_VALUE') or '').strip()
        if not _DATE.fullmatch(date):
            continue
        try:
            rate = float(value)
        except ValueError:
            continue
        if not math.isfinite(rate) or not 1 <= rate <= 50:      # 人民币兑欧元的合理范围
            continue
        if best is None or date > best[1]:
            best = (rate, date)
    if best is None:
        raise FxError('ECB 返回里没有可用的 CNY/EUR 汇率')
    return best


def current(c):
    """kv 里最近一次成功的汇率：(rate, date)；从未成功时 None。c 为 sqlite3 连接。"""
    row = c.execute('SELECT v FROM kv WHERE k=?', (KV_RATE,)).fetchone()
    if not row:
        return None
    try:
        data = json.loads(row[0])
        rate, date = float(data['rate']), str(data['date'])
    except (TypeError, ValueError, KeyError):
        return None
    if not math.isfinite(rate) or rate <= 0 or not _DATE.fullmatch(date):
        return None
    return rate, date


def _after_publish(now):
    """柏林时间工作日 16:30 之后（ECB 约 16:00 CET 发布当天的参考汇率）。"""
    t = berlin(now)
    return t.weekday() < 5 and (t.hour, t.minute) >= PUBLISH_AFTER


def _late_due(saved, have, now):
    """今天已取过、但取到的还是前一天的观测值：工作日 16:30 之后再取一次（每天只多这一次）。"""
    today = berlin_date(now)
    return have[1] < today and _after_publish(now) and saved.get('late_on') != today


def refresh(store, clock=time.time, get=None):
    """需要时取一次汇率。返回 dict(fx_status, fx_rate, fx_date)：
    fx_status：ok（本次取到）/ fresh（今天已取过）/ wait（失败后 1 小时内不重试）/ error（本次失败，沿用旧值）。"""
    get = get or _get
    now = clock()
    with store.connect() as c:
        row = c.execute('SELECT v FROM kv WHERE k=?', (KV_RATE,)).fetchone()
        try:
            saved = json.loads(row[0]) if row else None
        except ValueError:
            saved = None
        saved = saved if isinstance(saved, dict) else None
        attempt = c.execute('SELECT v FROM kv WHERE k=?', (KV_ATTEMPT,)).fetchone()
        have = current(c)

    def result(status):
        out = {'fx_status': status}
        if have:
            out.update(fx_rate=have[0], fx_date=have[1])
        return out
    today = berlin_date(now)
    if saved and saved.get('fetched_on') == today and have and not _late_due(saved, have, now):
        return result('fresh')
    try:
        failed = float(attempt[0]) if attempt else None
    except ValueError:
        failed = None
    if failed is not None and 0 <= now - failed < RETRY_SECONDS:
        return result('wait')
    try:
        r = get(ECB_URL, ECB_PARAMS, TIMEOUT)
        if getattr(r, 'status_code', 200) != 200:
            raise FxError(f'ECB 返回 HTTP {r.status_code}')
        rate, date = parse(r.text)
    except Exception as e:                  # 网络、超时、格式：沿用旧值与其原日期
        store.set(KV_ATTEMPT, now)
        log.warning('汇率未能更新（%s），沿用%s', type(e).__name__, f' {have[1]} 的汇率' if have else '：暂无汇率')
        return result('error')
    with store.connect() as c:
        late = today if _after_publish(now) else (saved or {}).get('late_on') if (saved or {}).get('fetched_on') == today else None
        c.execute('INSERT OR REPLACE INTO kv VALUES(?,?)', (KV_RATE, json.dumps({
            'rate': rate, 'date': date, 'fetched_on': today, 'late_on': late,
            'fetched_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now))})))
        c.execute('DELETE FROM kv WHERE k=?', (KV_ATTEMPT,))
    have = (rate, date)
    return result('ok')


def run(store, clock=time.time):
    """采集轮里调用：任何意外都只写进结果，不中断采集。"""
    try:
        return refresh(store, clock)
    except Exception:
        log.exception('汇率更新出错')
        return {'fx_status': 'error'}
