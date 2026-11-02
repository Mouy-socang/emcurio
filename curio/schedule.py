"""采集调度与每日接口调用预算（AD-27、NFR-2）：间隔计算、是否到期、预算计数与分配的单一归口。

launchd 每 5 分钟唤醒一次 `curio.curio_monitor`；本模块按 kv 里的状态决定本轮是否做新货抓取、
是否做在售检查，并给每个外部 HTTP 请求按平台计入当天（柏林日期）的预算。
"""
import json
import math
import time
from datetime import datetime, timezone
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
# launchd 的唤醒时刻会有几秒抖动；容差避免 5 分钟间隔因早到几秒而被拖成 10 分钟。
JITTER_SECONDS = 30

FETCH_SHARE = 0.6           # 新货抓取最多用每日预算的 60%，在售检查保底 40%

KV_BASE = 'schedule_fetch_interval_min'
KV_LAST_FETCH = 'schedule_last_fetch_at'
KV_LAST_CHECK = 'schedule_last_status_check_at'
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


def effective_interval(base, ts):
    """夜间（柏林 01:00–06:59）间隔 ×2，仍不超过 60 分钟。"""
    base = clamp(base)
    return min(MAX_INTERVAL, base * 2) if is_night(ts) else base


def _due(last, minutes, ts):
    return last is None or ts - last >= minutes * 60 - JITTER_SECONDS


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
            state[p] = {k: int(counts.get(k, 0)) for k in self.PURPOSES}
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
