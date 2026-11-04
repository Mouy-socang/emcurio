"""后台 02 商品池与 03 R12（榜；故事 11，CAP-7；AD-5、AD-22、AD-26、AD-28）。

- 02 商品池：拣选状态 kept 的货（分数高到低，无分在后），每件带货号、名称、欧元价、平台、是否在售、是否在榜、
  不能上榜的原因；另列已移出商品池的货（可放回；标记售出的带 sold，可撤销售出）。移出 / 放回 / 标记售出 / 撤销售出
  经拣选状态机（同一事务里 publish.reconcile 入队隐藏 / 取消隐藏）；移出商品池与标记售出的货同一事务从榜里拿掉。
- 03 榜：存储与校验复用 publish.set_rank（已在榜上、之后下架或不再合格的货可原样留在榜里，便于先排序再补位；
  发布时仍被 publish.check_rank 拒绝）。行带"已下架 / 不能发布"标记。
- 发布状态：最近一次入队的发布（publishes，非 dropped）的时间；当前的 publish.status_fingerprint（榜、入选货号、
  欧元价、点评与替代文字）与该次发布记下的不同，即"有未发布的修改"。
- 写接口带 request_id（表 triage_requests，与故事 7 共用）：同一 request_id 重放返回首次结果。
- 预览（AD-28）：publish.candidate 生成候选快照（不入发件箱、不分配编号），由 server/preview.py 用客人页同一份模板
  渲染；Mac 端不装 Jinja2，经 server/.venv 子进程调用（见 render_preview）。
"""
import json
import subprocess
from pathlib import Path

from . import publish, triage
from .config import ROOT
from .store import card, now

SERVER_DIR = Path(__file__).resolve().parents[1] / 'server'
RENDER_TIMEOUT = 60
REASON_KA = 'Kleinanzeigen 的货不能加入 R12'
REASON_FULL = f'R12 已满 {publish.RANK_SIZE} 件，请先移出一件'


class PreviewError(RuntimeError):
    """预览渲染不可用（中文说明）。"""


def rank_reason(c, item, code):
    """这件货不能加入榜的原因（中文，界面用语）；能则 None。榜满由界面按当时件数判断。"""
    if item['platform'] == 'kleinanzeigen':
        return REASON_KA
    if not code:
        return '还没有货号，请先留下'
    if item['status'] != 'active':
        return '原平台已下架，不能加入 R12'
    if not publish._priced(item):
        return '没有欧元标价，不能加入 R12'
    return None


def _scored(c, state):
    return c.execute('''SELECT i.*, s.score sort_score, t.updated_at state_at FROM triage t
        JOIN items i ON i.item_id=t.item_id LEFT JOIN scores s ON s.item_id=i.item_id
        WHERE t.state=? ORDER BY s.score IS NULL, s.score DESC, t.updated_at DESC, i.item_id''', (state,)).fetchall()


def _pool_row(c, item, ranked, root):
    out = card(c, item, root)
    code = out['code']
    out['on_sale'] = item['status'] == 'active'
    out['rank_pos'] = ranked.index(code) + 1 if code in ranked else None
    out['rank_reason'] = rank_reason(c, item, code)
    out['sold'] = triage.is_sold(c, item['item_id'])
    return out


def pool(store, root=ROOT):
    """02 商品池：留下的货与已移出商品池的货，以及当前的榜。"""
    with store.connect() as c:
        ranked = publish.rank(c)
        kept = [_pool_row(c, r, ranked, root) for r in _scored(c, 'kept')]
        out = [_pool_row(c, r, ranked, root) for r in _scored(c, 'out_of_pool')]
    return dict(items=kept, out=out, rank=ranked, counts=dict(kept=len(kept), out=len(out), rank=len(ranked)))


def publish_status(c, fees=None):
    """{published_at, publish_no, dirty, orders}：最近一次入队的发布，与其后是否有未发布的修改（内容指纹不同）；
    orders 为该次发布为几位客人（账号）排好了个性化顺序（故事 18）。"""
    last = publish.last_publish(c)
    if last is None:
        return dict(published_at=None, publish_no=None, dirty=bool(publish.rank(c)), orders=0)
    if last.get('status_fp'):
        dirty = publish.status_fingerprint(c, fees) != last['status_fp']
    else:                       # 故事 11 之前的发布没有指纹：只比较榜
        dirty = json.loads(last['rank']) != publish.rank(c)
    return dict(published_at=last['created_at'], publish_no=last['publish_no'], dirty=dirty,
                orders=last.get('orders') or 0)


def detail_rank_reason(store, iid):
    """货详情用：这件货不能加入榜的原因（不在商品池、Kleinanzeigen、已下架、无欧元价）；能则 None。榜满由界面判断。"""
    with store.connect() as c:
        item = c.execute('SELECT * FROM items WHERE item_id=?', (iid,)).fetchone()
        if item is None:
            return None
        if item['platform'] == 'kleinanzeigen':
            return REASON_KA
        if triage.state(c, iid) != 'kept':
            return '不在商品池，不能加入 R12'
        return rank_reason(c, item, triage.code(c, iid))


def rank_state(store, root=ROOT):
    """03 榜：行（名次、缩略图、货号、名称、价格、是否已下架 / 不能发布）、件数与发布状态。"""
    with store.connect() as c:
        rows = []
        for pos, code in enumerate(publish.rank(c), 1):
            item = c.execute('SELECT i.* FROM item_codes k JOIN items i ON i.item_id=k.item_id WHERE k.code=?',
                             (code,)).fetchone()
            row = card(c, item, root) if item else dict(item_id=None, name=code, code=code, price='', tags=[],
                                                        image=None, platform='', status=None)
            reason = publish.why_not(c, code)
            row.update(pos=pos, code=code, off=reason is not None, reason=reason,
                       gone=item is not None and item['status'] != 'active')
            rows.append(row)
        status = publish_status(c, publish._fees(root))
    return dict(rows=rows, count=len(rows), size=publish.RANK_SIZE, status=status)


# ---------- 写：幂等（triage_requests） ----------

def _once(store, request_id, payload, fn, mark_replay=False):
    """BEGIN IMMEDIATE 里执行 fn(c)；同一 request_id 重放返回首次结果，内容不同报错。
    mark_replay：重放的结果带 replayed=True（调用方据此不重复触发发送）。"""
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 100:
        raise ValueError('请求编号无效')
    text = json.dumps(payload, ensure_ascii=False)
    with store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        prev = c.execute('SELECT * FROM triage_requests WHERE request_id=?', (request_id,)).fetchone()
        if prev:
            if prev['payload'] != text:
                raise ValueError('重复请求内容不同')
            out = json.loads(prev['result'])
            return dict(out, replayed=True) if mark_replay else out
        result = fn(c)
        c.execute('INSERT INTO triage_requests VALUES(?,?,?,?)',
                  (request_id, text, json.dumps(result, ensure_ascii=False), now()))
        return result


def save_rank(store, codes, request_id):
    """保存榜（按名次的货号列表）。返回 dict(rank=…)。校验失败抛 PublishError（ValueError）。"""
    if not isinstance(codes, list) or not all(isinstance(x, str) for x in codes):
        raise ValueError('R12 须为货号列表')

    def work(c):
        return dict(rank=publish.set_rank(c, codes, keep_stale=True))
    return _once(store, request_id, ['rank', codes], work)


POOL_ACTIONS = ('out_of_pool', 'back_to_pool', 'mark_sold', 'undo_sold')


def pool_action(store, action, iid, request_id):
    """02 商品池：移出商品池（out_of_pool）/ 放回商品池（back_to_pool）/ 标记售出（mark_sold）/ 撤销售出（undo_sold，
    撤销该货最近一次标记售出，回到留下）。移出与标记售出的货同一事务从榜里拿掉。
    返回值里 hidden_queued：本次是否往发件箱写了隐藏 / 取消隐藏（调用方据此立即触发一次发送）。"""
    if action not in POOL_ACTIONS:
        raise ValueError('未知的商品池动作')
    if not isinstance(iid, str) or not iid:
        raise ValueError('商品编号无效')

    def work(c):
        if action == 'undo_sold':
            if not triage.is_sold(c, iid):
                raise triage.TriageError('这件货没有标记售出，不能撤销')
            r = triage.undo(c, iid, actions=('mark_sold',))
        else:
            r = triage.transition(c, iid, action, source='backoffice')
        dropped = False
        if action in ('out_of_pool', 'mark_sold') and r['code']:
            ranked = publish.rank(c)
            if r['code'] in ranked:
                ranked.remove(r['code'])
                c.execute('DELETE FROM publish_rank')
                c.executemany('INSERT INTO publish_rank(pos,code) VALUES(?,?)', list(enumerate(ranked, 1)))
                dropped = True
        return dict(item_id=iid, state=r['state'], code=r['code'], left_rank=dropped, rank=publish.rank(c),
                    sold=triage.is_sold(c, iid), hidden_queued=bool(r.get('reconciled')))
    return _once(store, request_id, ['pool', action, iid], work, mark_replay=True)


# ---------- 预览渲染（server/preview.py 子进程） ----------

def render_preview(payload, path, query='', received_at=None, prefix='/preview', server_dir=SERVER_DIR):
    """用客人页同一份模板渲染候选快照。返回 dict(status, type, body)。服务器端环境缺失时抛 PreviewError。"""
    py = Path(server_dir) / '.venv' / 'bin' / 'python'
    if not py.exists():
        raise PreviewError('预览需要服务器端环境：在 server/ 下运行 uv sync')
    data = json.dumps(dict(payload=payload, path=path, query=query, received_at=received_at, prefix=prefix),
                      ensure_ascii=False).encode('utf-8')
    try:
        r = subprocess.run([str(py), 'preview.py'], cwd=str(server_dir), input=data, capture_output=True,
                           timeout=RENDER_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise PreviewError('预览渲染超时') from None
    if r.returncode != 0:
        raise PreviewError('预览渲染失败：' + r.stderr.decode('utf-8', 'replace').strip().splitlines()[-1][:200]
                           if r.stderr.strip() else '预览渲染失败')
    try:
        out = json.loads(r.stdout.decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        raise PreviewError('预览渲染失败：渲染程序没有返回结果') from None
    if not isinstance(out, dict) or not {'status', 'type', 'body'} <= set(out):
        raise PreviewError('预览渲染失败：渲染结果格式不对')
    return out
