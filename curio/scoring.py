"""综合评分（故事 27，方案 C；AD-15）：每件货本地算四个成分（墨墨口味、性价比、客群趋势、客人喜好，各 0–100 百分位），
合成综合分与一句中文理由，存 `composite_scores`（每件一行，每轮全量重写）。

- 只读 curio.db（items、triage、std_names、scores、owner_events 的 seen）与 GoodCustomer 的 `orders/order_items.jsonl`
  （路径取 config/settings.yaml 的 goodcustomer.db）；知识库中文名取 judge.kb_vocab 的图案 / 器型词表，品牌别名取
  config/matching.yaml。不调用 AI、不改拣选状态、不移出也不上榜（AD-26）。
- 计算只在规则归口（curio/judge.composite_scores，经 curio/rules.py）；参数在 config/scoring.yaml。
- 读与算都在写事务之外（compute 只要一个连接，scripts/score_compare.py 用只读连接调用它）；
  写入是一次事务：清空后整表写入（refresh）。
- 采集每轮在封存预测之前调用 refresh_for_run；失败只记结果字段，不影响采集。
- GoodCustomer 读不了（路径不存在、文件坏）时趋势全为 50，理由注明"客群数据缺"，记警告。
"""
import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import judge, rules
from .config import ROOT, scoring_rules_for

log = logging.getLogger(__name__)

KEPT_STATES = ('kept', 'out_of_pool')
ALGO = 1            # 算法版本（成分或合成的算法变了加 1）；与参数一起进 version 指纹


def _settings(root):
    try:
        from .config import load
        return load(root)
    except Exception:
        log.warning('读取 config/settings.yaml 失败，综合评分用默认知识库路径、不读客群数据', exc_info=True)
        return None


def _gc_db(root):
    """GoodCustomer 资料库路径（settings.yaml 的 goodcustomer.db）；读不了设置时 None。测试里替换成不存在的路径。"""
    s = _settings(root)
    return s['goodcustomer']['db'] if s else None


def read_orders(gc_db):
    """GoodCustomer 订单行列表；路径不存在或读不了时 None（坏行跳过）。"""
    if not gc_db:
        return None
    path = Path(gc_db) / 'orders' / 'order_items.jsonl'
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    out = []
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _matching_brands(root):
    path = Path(root) / 'config/matching.yaml'
    if not path.is_file():
        path = ROOT / 'config/matching.yaml'
    raw = yaml.safe_load(path.read_text()) or {}
    brands = raw.get('brands') or {}
    return brands if isinstance(brands, dict) else {}


def _cjk(text):
    return any('一' <= ch <= '鿿' for ch in text or '')


def vocab_names(root, kb_path=None):
    """理由与客群识别用的中文名：(names=dict(brand, pattern, function), aliases, patterns, functions)。
    aliases：[(小写别名, 品牌)]（长的在前，同长按 matching.yaml 的顺序；品牌名本身也算）；patterns / functions：[(中文名, ID)]，长的在前。"""
    vocab = judge.kb_vocab(kb_path)[0]
    pat_zh, fun_zh, pat_keys, fun_keys = {}, {}, {}, {}
    for e in vocab.get('patterns') or []:
        pl = e[4]
        if pl and pl[0] != 'catalog' and pl[1]:
            pat_zh.setdefault(pl[0], pl[1])
            pat_keys.setdefault(pl[1], pl[0])
    for payload in (vocab.get('codes') or {}).values():
        if payload and payload[0] != 'catalog' and payload[1]:
            pat_zh.setdefault(payload[0], payload[1])
    for e in vocab.get('functions') or []:
        pl = e[4]
        if pl and pl[1]:
            fun_zh.setdefault(pl[0], pl[1])
            fun_keys.setdefault(pl[1], pl[0])
    brand_zh, aliases = dict(judge.BRAND_ZH), []
    for brand, al in _matching_brands(root).items():
        al = [a for a in (al or []) if isinstance(a, str) and a.strip()]
        for a in [brand] + al:
            aliases.append((str(a).lower(), brand))
        zh = next((a for a in al if _cjk(a)), None)
        if zh:
            brand_zh.setdefault(brand, zh)
    order = lambda d: sorted(d.items(), key=lambda kv: (-len(kv[0]), kv[0]))
    aliases.sort(key=lambda a: -len(a[0]))      # 长的别名先比（稳定排序，同长保持 matching.yaml 顺序）
    return dict(brand=brand_zh, pattern=pat_zh, function=fun_zh), aliases, order(pat_keys), order(fun_keys)


ITEMS_SQL = '''SELECT i.item_id, i.listed_price, i.currency, t.state, s.brand, s.pattern_id, s.function_id,
    sc.score old_score, sc.source old_source FROM items i LEFT JOIN triage t ON t.item_id=i.item_id
    LEFT JOIN std_names s ON s.item_id=i.item_id LEFT JOIN scores sc ON sc.item_id=i.item_id ORDER BY i.item_id'''


def load_items(c):
    """全部货（含 backlog、自动移出、已处理）→ judge.composite_scores 的输入。
    标签：留下（kept / out_of_pool）= 1；去掉（removed）或「看过没选」（新货且有 seen 记录）= 0；其余 None。"""
    seen = {r[0] for r in c.execute("SELECT DISTINCT item_id FROM owner_events WHERE type='seen'")}
    out = []
    for r in c.execute(ITEMS_SQL):
        state = r['state']
        label = 1 if state in KEPT_STATES else 0 if state == 'removed' or (state == 'new' and r['item_id'] in seen) else None
        out.append(dict(item_id=r['item_id'], listed_price=r['listed_price'], currency=r['currency'], brand=r['brand'],
                        pattern_id=r['pattern_id'], function_id=r['function_id'], label=label,
                        ai_score=r['old_score'] if r['old_source'] == 'ai' else None))
    return out


def version(params):
    # 故事 28：曝光衰减参数只影响排序键、不影响综合分，不进综合分的版本指纹。
    params = {k: v for k, v in params.items() if k != 'exposure'}
    return 'C:' + hashlib.sha1(json.dumps(dict(algo=ALGO, params=params), sort_keys=True).encode()).hexdigest()[:8]


def compute(c, root=ROOT, params=None, gc_db=None, today=None, settings=None):
    """只读：用连接 c 读货，算全部货的综合分（不写库）。gc_db 缺省读 settings.yaml 的 goodcustomer.db。
    返回 dict(scores={item_id: …}, items=[…], demand_ok, version, params)。"""
    params = params or scoring_rules_for(root)
    settings = settings if settings is not None else _settings(root)
    kb = (settings or {}).get('em_kb_path')
    if gc_db is None:
        gc_db = _gc_db(root)
    if today is None:
        from .schedule import BERLIN
        today = datetime.now(timezone.utc).astimezone(BERLIN).date()
    names, aliases, patterns, functions = vocab_names(root, kb)
    orders = read_orders(gc_db)
    if orders is not None and not any(not o.get('refunded') for o in orders):
        orders = None               # 读得到但没有可用订单（空、全是坏行或退款）：同读不了
    if orders is None:
        log.warning('GoodCustomer 订单读不了或没有可用订单（%s），客群趋势全为 50', gc_db)
        demand = None
    else:
        demand = rules.gc_demand(orders, aliases, patterns, functions, today, params)
    items = load_items(c)
    return dict(scores=rules.composite_scores(items, demand, names, params), items=items,
                demand_ok=demand is not None, version=version(params), params=params)


def write(store, data):
    """一次事务：清空 composite_scores 后写入本次全部结果。"""
    from .store import now
    at, ver = now(), data['version']
    rows = [(iid, round(r['score'], 3), round(r['momo'], 3), round(r['value'], 3), round(r['trend'], 3),
             round(r['customer'], 3), r['reason'], ver, at) for iid, r in data['scores'].items()]
    with store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute('DELETE FROM composite_scores')
        c.executemany('INSERT INTO composite_scores(item_id,score,momo,value,trend,customer,reason,version,scored_at) '
                      'VALUES(?,?,?,?,?,?,?,?,?)', rows)
    return len(rows)


def refresh(store, root=ROOT, gc_db=None, today=None):
    """重算全部货并写入（读在事务外、写一次）。返回结果字段。"""
    t0 = time.time()
    with store.connect() as c:
        data = compute(c, root, gc_db=gc_db, today=today)
    n = write(store, data)
    return dict(composite_status='ok', composite_count=n, composite_trend='ok' if data['demand_ok'] else 'missing',
                composite_version=data['version'], composite_seconds=round(time.time() - t0, 2))


def refresh_for_run(store, root, ts=None):
    """采集调度调用（封存之前）：失败只返回结果字段，不影响采集。"""
    try:
        today = None
        if ts is not None:
            from .schedule import BERLIN
            today = datetime.fromtimestamp(ts, timezone.utc).astimezone(BERLIN).date()
        return refresh(store, root, today=today)
    except Exception as e:      # noqa: BLE001 — 重算失败不影响采集
        log.warning('综合评分重算失败', exc_info=True)
        return dict(composite_status='error', composite_error=str(e)[:200])
