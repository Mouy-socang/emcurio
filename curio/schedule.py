"""采集调度与每日接口调用预算（AD-27、NFR-2）：间隔计算、是否到期、预算计数与分配的单一归口。

launchd 每 5 分钟唤醒一次 `curio.curio_monitor`；本模块按 kv 里的状态决定本轮是否做新货抓取、
是否做在售检查，并给每个外部 HTTP 请求按平台计入当天（柏林日期）的预算。
"""
import json
import math
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo('Europe/Berlin')
PLATFORMS = ('ebay', 'etsy', 'kleinanzeigen')
# Etsy 账号额度 5000/天、5 次/秒；预算留出余量。
DEFAULT_BUDGET = {'ebay': 4500, 'etsy': 4500, 'kleinanzeigen': 1500}

MIN_INTERVAL = 5.0          # 分钟
MAX_INTERVAL = 60.0
DEFAULT_INTERVAL = 30.0     # 首次运行的基础间隔，沿用原先的 30 分钟节拍
NIGHT_HOURS = range(1, 7)   # 柏林时间 01:00–06:59
STATUS_CHECK_MINUTES = 30   # 在售检查每 30 分钟最多一轮
PUBLISHED_CHECK_MINUTES = 15  # 已发布货下架检查每 15 分钟最多一轮（AD-27），独立于新货抓取与普通在售检查
WAKE_SECONDS = 300          # launchd 设计唤醒间隔（scripts/install_hourly.py StartInterval）；读不到本机 plist 时按它算
LAUNCHD_PLIST = '~/Library/LaunchAgents/com.curiobject.collect.plist'   # 测试里改成临时路径
# launchd 的唤醒时刻会有几秒抖动；容差避免 5 分钟间隔因早到几秒而被拖成 10 分钟。
JITTER_SECONDS = 30

FETCH_SHARE = 0.6           # 新货抓取最多用每日预算的 60%，在售检查保底 40%

KV_BASE = 'schedule_fetch_interval_min'
KV_LAST_FETCH = 'schedule_last_fetch_at'
KV_LAST_CHECK = 'schedule_last_status_check_at'
KV_LAST_PUBLISHED = 'schedule_last_published_check_at'
KV_PUBLISHED_OK = 'published_check_ok_at:'   # + 平台：该平台已发布货下架检查最近一次全部判定完成的时间（UTC，带 Z）
KV_USAGE = 'api_usage'


def berlin(ts):
    return datetime.fromtimestamp(ts, timezone.utc).astimezone(BERLIN)


def berlin_date(ts):
    return berlin(ts).date().isoformat()


def is_night(ts):
    return berlin(ts).hour in NIGHT_HOURS


def clamp(minutes):
    return min(MAX_INTERVAL, max(MIN_INTERVAL, float(minutes)))


def next_base_interval(base, new_count):
    """上一轮三平台合计新货 ≥10 件减半，0 件 ×1.5，其余不变；夹在 5–60 分钟。"""
    base = clamp(base)
    if new_count >= 10:
        base /= 2
    elif new_count == 0:
        base *= 1.5
    return clamp(base)


# 分时段抓取（墨墨 2026-11-04，依据 Kleinanzeigen 真实发帖分布：10–19 点每小时 12–16 件，凌晨约 1 件，周日最多）：
# 每个时段给出间隔的下限与上限（分钟），自适应间隔夹在其中。周日 08:00–21:59 按高峰处理。
FETCH_WINDOWS = (
    (range(10, 20), 5.0, 5.0),            # 高峰：每 5 分钟
    (range(7, 10), 5.0, 15.0),            # 早间：5–15 分钟
    (range(20, 23), 5.0, 15.0),           # 晚间：5–15 分钟
    (tuple(range(23, 24)) + tuple(range(0, 7)), 30.0, 60.0),   # 夜间：30–60 分钟
)
SUNDAY_PEAK_HOURS = range(8, 22)


def window(ts):
    """该时刻所在时段的 (下限, 上限) 分钟。"""
    t = berlin(ts)
    if t.weekday() == 6 and t.hour in SUNDAY_PEAK_HOURS:
        return 5.0, 5.0
    for hours, lo, hi in FETCH_WINDOWS:
        if t.hour in hours:
            return lo, hi
    return MIN_INTERVAL, MAX_INTERVAL


def effective_interval(base, ts):
    """按上新数量自适应的基础间隔，再夹进当前时段的上下限（FETCH_WINDOWS）。"""
    lo, hi = window(ts)
    return min(hi, max(lo, clamp(base)))


def check_rounds_left(ts):
    """到柏林午夜（预算归零）前还剩几轮在售检查，至少 1。"""
    now = berlin(ts)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, math.ceil((midnight - now).total_seconds() / (STATUS_CHECK_MINUTES * 60)))


def launchd_interval(path=None):
    """本机已安装的 launchd 唤醒间隔（秒）；没装、读不出或内容不对时 None。"""
    import os
    import plistlib
    try:
        with open(os.path.expanduser(path or LAUNCHD_PLIST), 'rb') as f:
            data = plistlib.load(f)
        value = data.get('StartInterval') if isinstance(data, dict) else None
    except Exception:
        return None
    return value if type(value) is int and value > 0 else None


def published_check_interval(wake_seconds):
    """已发布货检查的实际间隔（分钟）：每次唤醒才判断是否到期，所以是 15 分钟向上取整到唤醒间隔的倍数。
    唤醒间隔未知时按设计值 WAKE_SECONDS 算。"""
    wake_seconds = wake_seconds or WAKE_SECONDS
    target = PUBLISHED_CHECK_MINUTES * 60 - JITTER_SECONDS
    return math.ceil(target / wake_seconds) * wake_seconds / 60


def published_capacity(budget, counts, interval_min):
    """已发布货检查的每日预计调用数 =（每轮请求数）×（每天轮数）；超过该平台在售检查保底份额（预算 − 新货份额）时
    返回中文警告列表。counts：{平台: 可能在线的件数}；Etsy 每 100 件一次请求。"""
    from .etsy import BATCH
    rounds = 1440 / max(interval_min, PUBLISHED_CHECK_MINUTES)
    out = []
    for platform, n in counts.items():
        per_round = math.ceil(n / BATCH) if platform == 'etsy' else n
        need = math.ceil(per_round * rounds)
        share = budget.limit(platform) - budget.fetch_cap(platform)
        if n and need > share:
            out.append(f'{platform} 已发布 {n} 件，按每 {interval_min:g} 分钟一轮每天约需 {need} 次请求，'
                       f'超过在售检查份额 {share} 次：部分轮次会因预算不足而不算成功（客人页可能提示状态未更新）')
    return out


def iso_z(ts):
    """UTC，ISO 8601 带 Z，精确到毫秒（跨机时间约定）。"""
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def _due(last, minutes, ts):
    # 记录时间晚于现在（系统时钟被调回）时视为到期，避免抓取停摆。
    return last is None or last > ts or ts - last >= minutes * 60 - JITTER_SECONDS


class Scheduler:
    """调度状态存 kv：基础间隔、上次新货抓取时间、上次在售检查时间。"""

    def __init__(self, store, clock=time.time):
        self.store = store
        self.clock = clock

    def _float(self, key):
        value = self.store.get(key)
        try:
            return float(value) if value is not None else None
        except ValueError:
            return None

    def base_interval(self):
        value = self._float(KV_BASE)
        return clamp(value if value is not None else DEFAULT_INTERVAL)

    def current_interval(self, ts=None):
        return effective_interval(self.base_interval(), self.clock() if ts is None else ts)

    def fetch_due(self, ts=None):
        ts = self.clock() if ts is None else ts
        return _due(self._float(KV_LAST_FETCH), self.current_interval(ts), ts)

    def check_due(self, ts=None):
        ts = self.clock() if ts is None else ts
        return _due(self._float(KV_LAST_CHECK), STATUS_CHECK_MINUTES, ts)

    def published_check_due(self, ts=None):
        ts = self.clock() if ts is None else ts
        return _due(self._float(KV_LAST_PUBLISHED), PUBLISHED_CHECK_MINUTES, ts)

    def mark_published_check_started(self, ts=None):
        self.store.set(KV_LAST_PUBLISHED, self.clock() if ts is None else ts)

    def record_published_ok(self, platform, ts=None):
        """该平台本轮已发布货全部判定完成：记最近成功时间（心跳，AD-10）。"""
        self.store.set(KV_PUBLISHED_OK + platform, iso_z(self.clock() if ts is None else ts))

    def published_ok(self, platform):
        return self.store.get(KV_PUBLISHED_OK + platform)

    def mark_fetch_started(self, ts=None):
        self.store.set(KV_LAST_FETCH, self.clock() if ts is None else ts)

    def mark_check_started(self, ts=None):
        self.store.set(KV_LAST_CHECK, self.clock() if ts is None else ts)

    def record_fetch(self, new_count):
        """一轮新货抓取结束后按合计新货数调整基础间隔，返回新的基础间隔（分钟）。"""
        base = next_base_interval(self.base_interval(), new_count)
        self.store.set(KV_BASE, base)
        return base

    def summary(self, ts=None):
        ts = self.clock() if ts is None else ts
        last = self._float(KV_LAST_FETCH)
        interval = self.current_interval(ts)
        out = {'fetch_base_interval_min': round(self.base_interval(), 2),
               'fetch_interval_min': round(interval, 2),
               'night_mode': is_night(ts)}
        wake = launchd_interval()
        out['published_check_interval_min'] = published_check_interval(wake)
        if wake:
            out['launchd_interval_min'] = round(wake / 60, 2)
        if last is not None:
            out['next_fetch_at'] = datetime.fromtimestamp(last + interval * 60, timezone.utc).isoformat(timespec='seconds')
        return out


class BudgetExhausted(Exception):
    """该平台当天（柏林日期）该用途的接口调用预算已用完。"""

    def __init__(self, platform, purpose):
        super().__init__(f'{platform} 当日{"新货抓取" if purpose == "fetch" else "在售检查"}调用预算已用完')
        self.platform = platform
        self.purpose = purpose


class Budget:
    """每平台每日调用预算。计数每个外部 HTTP 请求（含 OAuth、重试），按柏林日期归零。

    新货抓取（fetch）最多用当日预算的 60%；在售检查（check）可用到预算上限，因此至少保有 40%。
    """

    PURPOSES = ('fetch', 'check')

    def __init__(self, store, limits, clock=time.time):
        self.store = store
        self.limits = dict(limits)
        self.clock = clock

    def _load(self):
        today = berlin_date(self.clock())
        try:
            state = json.loads(self.store.get(KV_USAGE) or '{}')
        except ValueError:
            state = {}
        if not isinstance(state, dict) or state.get('date') != today:
            state = {'date': today}
        for p in PLATFORMS:
            counts = state.get(p) if isinstance(state.get(p), dict) else {}
            try:
                state[p] = {k: int(counts.get(k, 0)) for k in self.PURPOSES}
            except (TypeError, ValueError):
                state[p] = {k: 0 for k in self.PURPOSES}   # 计数损坏：该平台归零，不让每轮崩溃
        return state

    def limit(self, platform):
        return int(self.limits.get(platform, 0))

    def fetch_cap(self, platform):
        return math.floor(self.limit(platform) * FETCH_SHARE)

    @staticmethod
    def _used(counts):
        return counts['fetch'] + counts['check']

    def _allowed(self, state, platform, purpose):
        counts = state[platform]
        if self._used(counts) >= self.limit(platform):
            return False
        return purpose != 'fetch' or counts['fetch'] < self.fetch_cap(platform)

    def check_remaining(self, platform):
        """在售检查当天还可用的请求数（可用到预算上限）。"""
        return max(0, self.limit(platform) - self._used(self._load()[platform]))

    def available(self, platform, purpose):
        return self._allowed(self._load(), platform, purpose)

    def spend(self, platform, purpose):
        """在发出请求之前调用；预算不足时抛 BudgetExhausted，请求不得发出。"""
        if platform not in PLATFORMS or purpose not in self.PURPOSES:
            raise ValueError('未知的平台或用途')
        state = self._load()
        if not self._allowed(state, platform, purpose):
            raise BudgetExhausted(platform, purpose)
        state[platform][purpose] += 1
        self.store.set(KV_USAGE, json.dumps(state, sort_keys=True))

    def snapshot(self):
        state = self._load()
        return {'date': state['date'], **{
            p: {'used': self._used(state[p]), 'fetch': state[p]['fetch'], 'check': state[p]['check'],
                'limit': self.limit(p), 'fetch_cap': self.fetch_cap(p)} for p in PLATFORMS}}
