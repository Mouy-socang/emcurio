"""每日封存预测与符合度（故事 23）。只在本机：不上线、不进 GoodCustomer；不改打分、不改 01 排序与拣选。

- 封存（`daily_predictions`）：每个柏林日期每种类型一条、写入后不可改（只用 INSERT OR IGNORE；触发器禁止 UPDATE / DELETE）。
  pool = 01 候选（口径同 triage.feed：新货、非 backlog、在售、未被「看过即不要」排除）按当前 `scores` 分数高到低的完整排序，
  前 100 件为"系统进池"；rank = 商品池（口径同 publish.eligible / 02）按分数高到低的完整排序，前 12 件为"系统 MyR12"。
  同分按 first_seen 新到旧、再按 item_id；无分的排最后。两种触发都只在柏林 05:00 之后：采集调度当天第一轮
  （有工作的轮次在抓取入库与打分之后），或墨墨先打开 01 时的首个（不带游标的）feed 请求——后者带本次打开的
  session，本次打开里刚看到的货不被「看过即不要」排除。
- 进池符合度：只算封存时刻（sealed_at）之后的事件。K = 当天、封存之后的有效人工留下（triage.HUMAN_DECISION、
  new_state='kept'，按服务器时间 at）且在封存 pool 里的货；V = 当天、封存之后的 `owner_events.seen`（按 eff_at）且在
  封存 pool 里的货，再并上 K（留下的货必然看过，seen 上报丢失时基线不超过 100%）；n = |K|；系统选 = 封存排序限制在
  V 内的前 n 件。符合度 = |K ∩ 系统选| ÷ n；随机基线 = n ÷ |V|；提升倍数 = 符合度 ÷ 基线。n = 0 记"无数据"。
  当天封存之前已有人工决定时 late_seal=True，并给出这些决定的次数（不计入）。
- 榜符合度：当天最后一次发布（publishes，非 dropped）的 12 件；当天没发布但榜改过（triage_requests 里当天的保存榜，
  或结果带 left_rank 的请求）时取当天结束时的榜（今天 = 当前 publish_rank，往日 = 当天最后一次改动后的榜）；
  与封存 rank 前 12 的重合数 ÷ 12。都没有记"无数据"。
- 计算在请求时进行；今天与昨天实时算（昨天的 seen 可能晚到），前天及更早的日期首次计算后写入 `daily_agreement`
  不再变。最早一次封存之前的日期一律"无数据"、不写库。
"""
import json
from datetime import date, datetime, timedelta, timezone

from . import publish, triage
from .config import ROOT
from .schedule import BERLIN

POOL_TOP = 100
RANK_TOP = publish.RANK_SIZE
METHOD = 1                  # 预测口径版本（排序规则变了再加 1）
SEAL_HOUR = 5               # 柏林 05:00 起才封存（采集与 feed 两种触发相同）
FREEZE_AFTER = 2            # 今天 − 2 天及更早的结果写入 daily_agreement
HISTORY_MAX = 90


def ensure(c):
    c.executescript('''
    CREATE TABLE IF NOT EXISTS daily_predictions(day TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('pool','rank')),
        items TEXT NOT NULL, scores TEXT NOT NULL, source TEXT NOT NULL, version TEXT NOT NULL,
        sealed_at TEXT NOT NULL, trigger TEXT, PRIMARY KEY(day,kind));
    CREATE TRIGGER IF NOT EXISTS daily_predictions_frozen BEFORE UPDATE ON daily_predictions
        BEGIN SELECT RAISE(ABORT, '封存的预测不可修改'); END;
    CREATE TRIGGER IF NOT EXISTS daily_predictions_nodelete BEFORE DELETE ON daily_predictions
        BEGIN SELECT RAISE(ABORT, '封存的预测不可修改'); END;
    CREATE TABLE IF NOT EXISTS daily_agreement(day TEXT PRIMARY KEY, result TEXT NOT NULL, computed_at TEXT NOT NULL);
    CREATE TRIGGER IF NOT EXISTS daily_agreement_frozen BEFORE UPDATE ON daily_agreement
        BEGIN SELECT RAISE(ABORT, '历史符合度不可修改'); END;
    CREATE TRIGGER IF NOT EXISTS daily_agreement_nodelete BEFORE DELETE ON daily_agreement
        BEGIN SELECT RAISE(ABORT, '历史符合度不可修改'); END;
    ''')


def _utcnow():
    return datetime.now(timezone.utc)


def _now():
    return _utcnow().isoformat(timespec='seconds')


def today():
    return _utcnow().astimezone(BERLIN).date()


def parse_day(value):
    if isinstance(value, date):
        return value
    try:
        d = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError('日期无效') from None
    if not 2000 <= d.year <= 9000:
        raise ValueError('日期无效')
    return d


def _start(day):
    return datetime(day.year, day.month, day.day, tzinfo=BERLIN)


def bounds(day):
    """柏林日期 → [lo, hi) 的 UTC 时间字符串（与 store.now() 同一格式）。跨夏令时也是次日零点。"""
    f = lambda t: t.astimezone(timezone.utc).isoformat(timespec='seconds')
    return f(_start(day)), f(_start(day + timedelta(days=1)))


def bounds_z(day):
    """同 bounds，但为发布时间的格式（UTC、毫秒、带 Z，见 sync.utc_now）。"""
    f = lambda t: t.astimezone(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    return f(_start(day)), f(_start(day + timedelta(days=1)))


# ---------- 封存 ----------

ORDER = "ORDER BY s.score IS NULL, s.score DESC, COALESCE(x.first_seen,'') DESC, x.item_id"
POOL_SQL = ('''SELECT x.item_id, x.rule_version, s.score, s.source FROM (SELECT i.* FROM triage t JOIN items i ON i.item_id=t.item_id
    WHERE ''' + triage.FEED_BASE + ' AND ' + triage.SEEN_ELSEWHERE + ''' IS NULL) x
    LEFT JOIN scores s ON s.item_id=x.item_id ''' + ORDER)
RANK_SQL = ('SELECT x.item_id, x.rule_version, s.score, s.source FROM (' + publish.ELIGIBLE_SQL + ''') x
    LEFT JOIN scores s ON s.item_id=x.item_id ''' + ORDER)


def prediction_rows(c, kind, session=None):
    """当前分数下的完整预测排序（不写库）：[(item_id, rule_version, score, source)]。
    session：由 feed 触发时为本次打开的 session（本次里看到的不排除）；否则任何一次打开里看过即排除。"""
    if kind == 'pool':
        return c.execute(POOL_SQL, (session or '',)).fetchall()
    return c.execute(RANK_SQL).fetchall()


def seal(c, day=None, trigger=None, session=None):
    """封存 day（默认柏林今天）的 pool 与 rank 预测；已有的不覆盖。返回本次新写入的类型列表。调用方负责事务。"""
    day = parse_day(day or today()).isoformat()
    have = {r[0] for r in c.execute('SELECT kind FROM daily_predictions WHERE day=?', (day,))}
    wrote = []
    for kind in ('pool', 'rank'):
        if kind in have:
            continue
        rows = prediction_rows(c, kind, session)
        sources = {}
        for r in rows:
            k = r['source'] or 'none'
            sources[k] = sources.get(k, 0) + 1
        versions = sorted({r['rule_version'] for r in rows if r['rule_version'] is not None})
        cur = c.execute('INSERT OR IGNORE INTO daily_predictions(day,kind,items,scores,source,version,sealed_at,trigger) '
                        'VALUES(?,?,?,?,?,?,?,?)',
                        (day, kind, json.dumps([r['item_id'] for r in rows], ensure_ascii=False),
                         json.dumps([r['score'] for r in rows]), json.dumps(dict(table='scores', counts=sources), sort_keys=True),
                         json.dumps(dict(method=METHOD, rule_versions=versions)), _now(), trigger))
        if cur.rowcount:
            wrote.append(kind)
    return wrote


def sealed(c, day):
    day = parse_day(day).isoformat()
    return c.execute('SELECT 1 FROM daily_predictions WHERE day=? GROUP BY day HAVING count(*)=2', (day,)).fetchone() is not None


def seal_store(store, day=None, trigger=None, session=None):
    """在独立写事务里封存（已封存时只读一次就返回）。返回 dict(day, sealed=[…])。"""
    day = parse_day(day or today())
    with store.connect() as c:
        if sealed(c, day):
            return dict(day=day.isoformat(), sealed=[])
        c.execute('BEGIN IMMEDIATE')
        return dict(day=day.isoformat(), sealed=seal(c, day, trigger, session))


def _seal_due(t):
    return t.astimezone(BERLIN).hour >= SEAL_HOUR


def seal_for_run(store, ts):
    """采集调度调用：柏林 05:00 之后才封存当天。失败只返回结果字段，不影响采集。"""
    try:
        t = datetime.fromtimestamp(ts, timezone.utc).astimezone(BERLIN)
        if not _seal_due(t):
            return dict(prediction_status='before_seal_hour')
        out = seal_store(store, t.date(), 'collect')
    except Exception as e:      # noqa: BLE001 — 封存失败不影响采集
        return dict(prediction_status='error', prediction_error=str(e)[:200])
    return dict(prediction_status='sealed' if out['sealed'] else 'already_sealed', prediction_day=out['day'])


def seal_for_feed(store, session=None):
    """01 首个（不带游标的）feed 请求：同样只在柏林 05:00 之后封存；带本次打开的 session。返回是否新写入。"""
    t = _utcnow().astimezone(BERLIN)
    if not _seal_due(t):
        return False
    return bool(seal_store(store, t.date(), 'feed', session)['sealed'])


def _prediction(c, day, kind):
    r = c.execute('SELECT * FROM daily_predictions WHERE day=? AND kind=?', (day.isoformat(), kind)).fetchone()
    if r is None:
        return None
    return dict(items=json.loads(r['items']), scores=json.loads(r['scores']), sealed_at=r['sealed_at'],
                source=json.loads(r['source']), version=json.loads(r['version']), trigger=r['trigger'])


def first_sealed_day(c):
    r = c.execute('SELECT min(day) FROM daily_predictions').fetchone()
    return date.fromisoformat(r[0]) if r and r[0] else None


# ---------- 符合度 ----------

def _kept(c, lo, hi):
    return {r[0] for r in c.execute('SELECT DISTINCT item_id FROM triage_events WHERE ' + triage.HUMAN_DECISION +
                                    " AND new_state='kept' AND at>=? AND at<?", (lo, hi))}


def _seen(c, lo, hi):
    return {r[0] for r in c.execute("SELECT DISTINCT item_id FROM owner_events WHERE type='seen' AND eff_at>=? AND eff_at<?",
                                    (lo, hi))}


def _early(c, lo, sealed_at):
    """当天封存之前的有效人工决定：(次数, 最早时间)。"""
    r = c.execute('SELECT count(*), min(at) FROM triage_events WHERE ' + triage.HUMAN_DECISION + ' AND at>=? AND at<?',
                  (lo, sealed_at)).fetchone()
    return r[0], r[1]


def _row(c, iid, pos, score, root):
    from .store import card     # 延迟导入：store 依赖本模块
    item = c.execute('SELECT * FROM items WHERE item_id=?', (iid,)).fetchone()
    out = card(c, item, root) if item else dict(item_id=iid, name=iid, price='', image=None, platform='', code=None, tags=[])
    keep = ('item_id', 'name', 'name_lang', 'price', 'image', 'platform', 'code', 'tags')
    out = {k: out.get(k) for k in keep}
    out.update(system_pos=pos, system_score=score)
    return out


def pool_agreement(c, day, root=ROOT, cards=True):
    pred = _prediction(c, day, 'pool')
    if pred is None:
        return dict(status='no_prediction')
    order = pred['items']
    pos = {iid: n for n, iid in enumerate(order)}
    lo, hi = bounds(day)
    start = max(lo, pred['sealed_at'])
    early, first_at = _early(c, lo, pred['sealed_at'])
    kept = {i for i in _kept(c, start, hi) if i in pos}
    seen = {i for i in _seen(c, start, hi) if i in pos} | kept
    n = len(kept)
    base = dict(sealed_at=pred['sealed_at'], sealed_size=len(order), system_top=min(POOL_TOP, len(order)),
                n=n, seen=len(seen), kept_in_top=sum(1 for i in kept if pos[i] < POOL_TOP),
                late_seal=early > 0, late_decisions=early, first_decision_at=first_at)
    if n == 0:
        return dict(base, status='no_data')
    picked = [i for i in order if i in seen][:n]
    hits = len(kept & set(picked))
    agreement = hits / n
    baseline = n / len(seen)
    out = dict(base, status='ok', hits=hits, agreement=agreement, baseline=baseline,
               lift=agreement / baseline if baseline else None)
    if cards:
        row = lambda i: _row(c, i, pos[i] + 1, pred['scores'][pos[i]], root)
        out.update(you_only=[row(i) for i in sorted(kept - set(picked), key=pos.get)],
                   system_only=[row(i) for i in sorted(set(picked) - kept, key=pos.get)])
    return out


def _codes_to_ids(c, codes):
    out = []
    for code in codes:
        r = c.execute('SELECT item_id FROM item_codes WHERE code=?', (code,)).fetchone()
        if r:
            out.append(r[0])
    return out


def _day_rank(c, day, is_today):
    """当天的榜：(来源, 货号列表, 时间)；当天没发布、榜也没改过 → None。"""
    zlo, zhi = bounds_z(day)
    last = c.execute("""SELECT publish_no,created_at,rank FROM publishes WHERE status!='dropped' AND created_at>=? AND created_at<?
        ORDER BY publish_no DESC LIMIT 1""", (zlo, zhi)).fetchone()
    if last:
        return 'publish', json.loads(last['rank']), last['created_at']
    lo, hi = bounds(day)
    change = None
    for r in c.execute('SELECT payload,result,at FROM triage_requests WHERE at>=? AND at<? ORDER BY at, rowid', (lo, hi)):
        try:
            payload, result = json.loads(r['payload']), json.loads(r['result'])
            if payload[0] == 'rank' or result.get('left_rank'):
                ranked = result['rank']
                if isinstance(ranked, list):
                    change = (ranked, r['at'])
        except (ValueError, TypeError, IndexError, AttributeError, KeyError):
            continue
    if change is None:
        return None
    if is_today:
        return 'rank', publish.rank(c), change[1]
    return 'rank', change[0], change[1]


def rank_agreement(c, day, is_today):
    pred = _prediction(c, day, 'rank')
    if pred is None:
        return dict(status='no_prediction', size=RANK_TOP)
    got = _day_rank(c, day, is_today)
    if got is None:
        return dict(status='no_data', size=RANK_TOP, sealed_at=pred['sealed_at'])
    source, codes, at = got
    mine = set(_codes_to_ids(c, codes))
    system = set(pred['items'][:RANK_TOP])
    overlap = len(mine & system)
    return dict(status='ok', size=RANK_TOP, overlap=overlap, ratio=overlap / RANK_TOP, source=source, at=at,
                sealed_at=pred['sealed_at'])


def _before_first(day):
    return dict(day=day.isoformat(), final=True,
                pool=dict(status='no_data', reason='before_first_seal'),
                rank=dict(status='no_data', reason='before_first_seal', size=RANK_TOP))


def _result(c, day, root, now_day, cards):
    first = first_sealed_day(c)
    if first is None or day < first:
        return _before_first(day)
    frozen = day <= now_day - timedelta(days=FREEZE_AFTER)
    if frozen:
        r = c.execute('SELECT result FROM daily_agreement WHERE day=?', (day.isoformat(),)).fetchone()
        if r:
            return json.loads(r[0])
        cards = True            # 写库的结果总是完整的（含差异清单）
    out = dict(day=day.isoformat(), final=frozen, pool=pool_agreement(c, day, root, cards),
               rank=rank_agreement(c, day, day == now_day))
    if frozen:
        c.execute('INSERT OR IGNORE INTO daily_agreement(day,result,computed_at) VALUES(?,?,?)',
                  (day.isoformat(), json.dumps(out, ensure_ascii=False), _now()))
    return out


def day_result(store, day=None, root=ROOT, now_day=None, cards=True):
    """某个柏林日期的符合度。前天及更早的结果首次计算后写入 daily_agreement，之后原样返回。未来日期报错。"""
    now_day = now_day or today()
    day = parse_day(day or now_day)
    if day > now_day:
        raise ValueError('日期还没到')
    with store.connect() as c:
        return _result(c, day, root, now_day, cards)


def summary(r):
    p, k = r['pool'], r['rank']
    return dict(day=r['day'], status=p['status'], n=p.get('n'), seen=p.get('seen'), hits=p.get('hits'),
                agreement=p.get('agreement'), baseline=p.get('baseline'), lift=p.get('lift'),
                rank_status=k['status'], rank_overlap=k.get('overlap'))


def _days(days):
    if type(days) is not int or not 1 <= days <= HISTORY_MAX:
        raise ValueError('天数须为 1–%d' % HISTORY_MAX)
    return days


def history(store, days=30, root=ROOT, now_day=None, known=None):
    """近 days 天（含今天，旧到新）的每日摘要（不查卡片）。known：已算好的 {day: 结果}，不重复计算。"""
    days = _days(days)
    now_day = now_day or today()
    known = known or {}
    out = []
    with store.connect() as c:
        for n in range(days - 1, -1, -1):
            d = now_day - timedelta(days=n)
            r = known.get(d.isoformat()) or _result(c, d, root, now_day, cards=False)
            out.append(summary(r))
    return dict(days=out)


def view(store, day=None, root=ROOT, now_day=None, days=30):
    """「符合度」页 GET /api/agreement：指定日（默认今天）、它的前一天与近 days 天的摘要。"""
    now_day = now_day or today()
    d = parse_day(day or now_day)
    if d > now_day:
        raise ValueError('日期还没到')
    try:
        p = d - timedelta(days=1)
    except OverflowError:
        raise ValueError('日期无效') from None
    cur, prev = day_result(store, d, root, now_day), day_result(store, p, root, now_day)
    return dict(day=cur, prev=prev, today=now_day.isoformat(),
                history=history(store, days, root, now_day, {cur['day']: cur, prev['day']: prev})['days'])
