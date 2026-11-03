"""后台 06 客人（故事 14，CAP-14；AD-8、AD-12、AD-24）：账号关联与管理。

- 账号名单是服务器账号的副本（sync.pull_accounts 写 guest_accounts）；这里只读。
- 关联只记在 curio.db（guest_links + 只追加的 guest_link_log），不经服务器、即时生效、没有"待同步"；
  从不写 GoodCustomer 的任何文件：已有客户只读 customer.json 搜索（跳过已合并 / 已归档），
  新建档案只调用 GoodCustomer 自带的 `gcdb.py --db <资料库> new-customer --name …`，拿到 GC 编号后同一操作内关联。
- 停用、重置密码是 AD-24 指令，经发件箱发送（sync.enqueue_account_command）；入队后显示"待同步"，
  发件箱确认后为"已停用""已重置"，被拒收为"未同步 · 请处理"。重置的临时密码只在本次响应里交给墨墨的浏览器，
  不写入 guest_requests 或任何别的表（发件箱里的明文在服务器确认后由 flush 抹掉）。
- 邀请码：当前码 = 最近一条已送达的 set_invite_code；更新的未送达那条显示"待同步"（期间旧码仍有效）。
- 写操作带 request_id，同一 request_id 重放返回第一次的结果（guest_requests）。
- 关联、更正（前后两个客户）、解除后重算受影响客户的行为交接文件（curio/behavior.py，故事 15）；只需 GC 编号，不读 GC 库。
"""
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import unicodedata
from pathlib import Path

from . import behavior, sync

log = logging.getLogger(__name__)

GC_ID = re.compile(r'GC[0-9]{6}')
ACCOUNT_ID = re.compile(r'[0-9a-f]{32}')
GC_ROOTS = ('customers', '潜在客户')      # gcdb：客户在 customers/，潜在客户（含新建档案）在 潜在客户/
SKIP_STATUS = ('merged', 'archived')
GCDB_TIMEOUT = 30
SEARCH_LIMIT = 20
MSG_NO_GC = '找不到 GoodCustomer 资料库'
_write_lock = threading.Lock()
GC_CACHE_SECONDS = 30
_gc_cache = {}                         # gc['db'] -> (读取时刻, 全部档案)；新建档案后作废
_gc_cache_lock = threading.Lock()


class GuestError(ValueError):
    """06 客人写操作不能执行（中文，直接显示在行内）。"""


def _now():
    return sync.utc_now()


def _norm(text):
    return unicodedata.normalize('NFKC', text or '').strip().casefold()


# ---------- GoodCustomer（只读） ----------

def gc_available(gc):
    return bool(gc) and (Path(gc['db']) / '_system' / 'db.json').is_file()


def _gc_invalidate(gc):
    with _gc_cache_lock:
        _gc_cache.pop(gc['db'], None)


def gc_customers(gc, include_skipped=False):
    """读取资料库里全部档案的 customer.json（只读，进程内缓存 30 秒）。默认跳过已合并 / 已归档的档案。
    资料库不在时返回 []。"""
    if not gc_available(gc):
        return []
    with _gc_cache_lock:
        hit = _gc_cache.get(gc['db'])
    if hit and time.monotonic() - hit[0] < GC_CACHE_SECONDS:
        rows = hit[1]
    else:
        rows = _read_gc(gc)
        with _gc_cache_lock:
            _gc_cache[gc['db']] = (time.monotonic(), rows)
    return [c for c in rows if include_skipped or c['status'] not in SKIP_STATUS]


def _read_gc(gc):
    out = []
    for root in GC_ROOTS:
        base = Path(gc['db']) / root
        if not base.is_dir():
            continue
        for d in sorted(base.iterdir()):
            if not GC_ID.fullmatch(d.name):
                continue
            try:
                c = json.loads((d / 'customer.json').read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            if not isinstance(c, dict) or c.get('customer_id') != d.name:
                continue
            aliases = c.get('aliases') if isinstance(c.get('aliases'), list) else []
            accounts = c.get('platform_accounts') if isinstance(c.get('platform_accounts'), list) else []
            names = [c.get('display_name')] + aliases
            for acct in accounts:
                if isinstance(acct, dict):
                    names += [acct.get('display_name'), acct.get('handle')]
            display = c.get('display_name') if isinstance(c.get('display_name'), str) and c.get('display_name') else c['customer_id']
            out.append({'customer_id': c['customer_id'], 'display_name': display,
                        'aliases': [a for a in aliases if isinstance(a, str)],
                        'status': c.get('status'),
                        'keys': [_norm(n) for n in names if isinstance(n, str) and n.strip()]})
    return sorted(out, key=lambda c: c['customer_id'])


def search(gc, query, limit=SEARCH_LIMIT):
    """按微信名、显示名、别名或编号即时搜索（不区分大小写的包含匹配）；编号完全相同的排最前。"""
    if not gc_available(gc):
        raise GuestError(MSG_NO_GC)
    q = _norm(query)
    if not q:
        return []
    hits = [c for c in gc_customers(gc) if q in _norm(c['customer_id']) or any(q in k for k in c['keys'])]
    hits.sort(key=lambda c: (_norm(c['customer_id']) != q, c['customer_id']))
    return [{'customer_id': c['customer_id'], 'display_name': c['display_name'], 'aliases': c['aliases'][:5]}
            for c in hits[:limit]]


def _gc_customer(gc, customer_id):
    if not gc_available(gc):
        raise GuestError(MSG_NO_GC)
    if not isinstance(customer_id, str) or not GC_ID.fullmatch(customer_id):
        raise GuestError('客户编号无效')
    for c in gc_customers(gc):
        if c['customer_id'] == customer_id:
            return c
    raise GuestError(f'找不到客户 {customer_id}（或该档案已合并、已归档）')


def gcdb_new_customer(gc, name, run=None):
    """调用 GoodCustomer 自己的 gcdb.py 新建档案，返回新 GC 编号。失败抛 GuestError（中文）。"""
    if not gc_available(gc):
        raise GuestError(MSG_NO_GC)
    script = Path(gc['gcdb'])
    if not script.is_file():
        raise GuestError('找不到 GoodCustomer 的 gcdb.py')
    run = run or subprocess.run
    cmd = [sys.executable, str(script), '--db', gc['db'], 'new-customer', '--name=' + name]
    try:
        r = run(cmd, capture_output=True, text=True, timeout=GCDB_TIMEOUT, check=False,
                env={k: v for k, v in os.environ.items() if k != 'GOODCUSTOMER_DB'})
    except subprocess.TimeoutExpired:
        raise GuestError('未能新建档案：gcdb 超时') from None
    except OSError as e:
        raise GuestError(f'未能新建档案（{type(e).__name__}）') from None
    lines = (r.stdout or '').strip().splitlines()
    cid = lines[-1].strip() if lines else ''
    if r.returncode == 0 and not GC_ID.fullmatch(cid):
        raise GuestError('档案可能已新建，请先在 GoodCustomer 搜索确认')
    if r.returncode != 0:
        detail = ((r.stderr or '').strip().splitlines() or [''])[-1][:120]
        raise GuestError('未能新建档案' + ('：' + detail if detail else ''))
    return cid


# ---------- 幂等 ----------

def _replay(c, request_id, payload):
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 100:
        raise GuestError('请求编号无效')
    row = c.execute('SELECT payload,result FROM guest_requests WHERE request_id=?', (request_id,)).fetchone()
    if row is None:
        return None
    if row['payload'] != json.dumps(payload, ensure_ascii=False, sort_keys=True):
        raise GuestError('重复请求内容不同')
    return dict(json.loads(row['result']), replayed=True)


def _remember(c, request_id, payload, result):
    c.execute('INSERT INTO guest_requests(request_id,payload,result,at) VALUES(?,?,?,?)',
              (request_id, json.dumps(payload, ensure_ascii=False, sort_keys=True),
               json.dumps(result, ensure_ascii=False), _now()))


def _account(c, account_id):
    if not isinstance(account_id, str) or not ACCOUNT_ID.fullmatch(account_id):
        raise GuestError('账号编号无效')
    row = c.execute('SELECT * FROM guest_accounts WHERE account_id=?', (account_id,)).fetchone()
    if row is None:
        raise GuestError('找不到这个账号，请刷新本页')
    return row


def _linked(c, account_id):
    row = c.execute('SELECT customer_id FROM guest_links WHERE account_id=?', (account_id,)).fetchone()
    return row[0] if row else None


def _log(c, account_id, action, before, after, request_id):
    c.execute('INSERT INTO guest_link_log(account_id,action,before_id,after_id,at,request_id) VALUES(?,?,?,?,?,?)',
              (account_id, action, before, after, _now(), request_id))


# ---------- 关联（只在 curio.db，AD-12） ----------

def link(store, gc, action, account_id, request_id, customer_id=None):
    """action：link（关联到已有客户）/ relink（更正关联）/ unlink（解除关联）。返回结果字段。"""
    if action not in ('link', 'relink', 'unlink'):
        raise GuestError('未知操作')
    payload = {'op': action, 'account_id': account_id, 'customer_id': customer_id}
    with _write_lock:
        with store.connect() as c:
            prev = _replay(c, request_id, payload)
        if prev:
            return prev
        target = _gc_customer(gc, customer_id) if action != 'unlink' else None
        with store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            prev = _replay(c, request_id, payload)
            if prev:
                return prev
            acct = _account(c, account_id)
            before = _linked(c, account_id)
            if action == 'link' and before:
                raise GuestError(f'此账号已关联到 {before}，请用"更正关联"')
            if action in ('relink', 'unlink') and not before:
                raise GuestError('此账号还没有关联')
            if action == 'relink' and before == customer_id:
                raise GuestError(f'此账号已关联到 {customer_id}')
            at = _now()
            if action == 'unlink':
                c.execute('DELETE FROM guest_links WHERE account_id=?', (account_id,))
            else:
                c.execute('INSERT OR REPLACE INTO guest_links(account_id,customer_id,linked_at) VALUES(?,?,?)',
                          (account_id, customer_id, at))
            _log(c, account_id, action, before, customer_id if action != 'unlink' else None, request_id)
            # 故事 15（AD-12）：前后两个客户的交接文件都待重算（同一事务标记，提交后立即重算）。
            behavior.mark_dirty(c, before, customer_id if action != 'unlink' else None)
            result = {'ok': True, 'action': action, 'account_id': account_id, 'nickname': acct['nickname'],
                      'before': before, 'customer_id': customer_id if action != 'unlink' else None,
                      'display_name': target['display_name'] if target else None}
            _remember(c, request_id, payload, result)
    behavior.after_link_change(store)
    return result


def create_and_link(store, gc, account_id, request_id, run=None):
    """新建档案：以微信名/称呼为显示名调用 gcdb new-customer，拿到 GC 编号后同一操作内关联。gcdb 失败不关联。
    gcdb 成功后立即把 request_id → 新编号记进 kv `gc_created:<request_id>`（单独的短事务），关联失败时同一
    request_id 重试沿用这个编号，不再调用 gcdb（不会建出重复档案）。"""
    payload = {'op': 'create', 'account_id': account_id}
    created_key = 'gc_created:' + str(request_id)
    with _write_lock:
        with store.connect() as c:
            prev = _replay(c, request_id, payload)
            if prev:
                return prev
            acct = _account(c, account_id)
            before = _linked(c, account_id)
            row = c.execute('SELECT v FROM kv WHERE k=?', (created_key,)).fetchone()
        customer_id = row[0] if row else None
        if before:
            if customer_id:
                raise GuestError(f'此账号已关联到 {before}；已新建的档案 {customer_id} 未关联')
            raise GuestError(f'此账号已关联到 {before}，请用"更正关联"')
        if customer_id is None:
            customer_id = gcdb_new_customer(gc, acct['nickname'], run)
            _gc_invalidate(gc)
            with store.connect() as c:
                c.execute('INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)', (created_key, customer_id))
        try:
            with store.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                linked = _linked(c, account_id)
                if linked:
                    raise GuestError(f'此账号已关联到 {linked}；已新建的档案 {customer_id} 未关联')
                c.execute('INSERT INTO guest_links(account_id,customer_id,linked_at) VALUES(?,?,?)',
                          (account_id, customer_id, _now()))
                _log(c, account_id, 'link', None, customer_id, request_id)
                behavior.mark_dirty(c, customer_id)
                result = {'ok': True, 'action': 'create', 'account_id': account_id, 'nickname': acct['nickname'],
                          'before': None, 'customer_id': customer_id, 'display_name': acct['nickname'], 'created': True}
                _remember(c, request_id, payload, result)
        except GuestError:
            raise
        except Exception as e:
            log.warning('新建档案 %s 后未能关联：%s', customer_id, e)
            raise GuestError(f'已新建档案 {customer_id}，但未能关联；请重试（不会再新建）') from None
    behavior.after_link_change(store)
    return result


# ---------- 停用 / 重置密码（AD-24 指令） ----------

def _command_states(c, account_id=None):
    """每个账号、每种指令的最新一条（跳过已从发件箱删掉的）：state = pending / failed / done。"""
    sql = ('SELECT g.command_id,g.account_id,g.kind,g.target_seq,g.created_at,o.sent_at,o.failed_at,o.last_error '
           'FROM guest_commands g JOIN outbox o ON o.event_id=g.event_id')
    args = ()
    if account_id:
        sql += ' WHERE g.account_id=?'
        args = (account_id,)
    out = {}
    for r in c.execute(sql + ' ORDER BY g.target_seq', args):
        state = 'done' if r['sent_at'] else 'failed' if r['failed_at'] else 'pending'
        entry = out.setdefault(r['account_id'], {})
        info = {'command_id': r['command_id'], 'state': state, 'at': r['sent_at'] or r['created_at'],
                'error': r['last_error'] if state == 'failed' else None}
        if r['kind'] == 'disable_account':
            if state == 'done':
                entry['disabled_done'] = info
            entry['disable'] = info
        else:
            entry['reset'] = info
    return out


def command(store, kind, account_id, request_id):
    """停用账号或重置密码：入发件箱（同一事务记 guest_commands），返回结果。重置时结果里带临时密码明文——
    只在这一次响应里，重放时不再给出（replayed: true，password_lost: true）。"""
    if kind not in sync.ACCOUNT_COMMANDS:
        raise GuestError('未知操作')
    payload = {'op': kind, 'account_id': account_id}
    with _write_lock:
        with store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            prev = _replay(c, request_id, payload)
            if prev:
                if kind == 'reset_password':
                    prev['password_lost'] = True
                return prev
            acct = _account(c, account_id)
            states = _command_states(c, account_id).get(account_id, {})
            disabled = acct['status'] == 'disabled' or 'disabled_done' in states
            disabling = states.get('disable', {}).get('state') == 'pending'
            if any(states.get(k, {}).get('state') == 'failed' for k in ('disable', 'reset')) and not disabled:
                raise GuestError('上一条指令未同步，请先用 scripts/outbox.py 处理')
            if kind == 'disable_account':
                if disabled:
                    raise GuestError('此账号已停用')
                if disabling:
                    raise GuestError('停用还在待同步')
            else:
                if disabled or disabling:
                    raise GuestError('已停用的账号不提供重置')
                if states.get('reset', {}).get('state') == 'pending':
                    raise GuestError('上一次重置还在待同步')
            try:
                event_id, body = sync.enqueue_account_command(c, kind, account_id)
            except sync.ContractError as e:
                raise GuestError(str(e)) from None
            c.execute('INSERT INTO guest_commands(command_id,event_id,account_id,kind,target_seq,created_at) '
                      'VALUES(?,?,?,?,?,?)', (body['command_id'], event_id, account_id, kind, body['target_seq'], _now()))
            result = {'ok': True, 'action': kind, 'account_id': account_id, 'nickname': acct['nickname'],
                      'command_id': body['command_id'], 'state': 'pending'}
            _remember(c, request_id, payload, result)     # 不含临时密码
    if kind == 'reset_password':
        return dict(result, temp_password=body['temp_password'])
    return result


# ---------- 邀请码 ----------

def invite_state(c):
    """当前邀请码（最近一条已送达的 set_invite_code）与更新的待确认那条（pending / failed）。"""
    rows = c.execute("SELECT seq,payload,sent_at,failed_at FROM outbox WHERE kind='set_invite_code' ORDER BY seq").fetchall()
    current = pending = None
    for r in rows:
        try:
            code = json.loads(r['payload'])['code']
        except (ValueError, KeyError, TypeError):
            continue
        if r['sent_at']:
            current = {'code': code, 'at': r['sent_at'], 'seq': r['seq']}
            pending = None
        else:
            pending = {'code': code, 'state': 'failed' if r['failed_at'] else 'pending', 'seq': r['seq']}
    return {'current': current and {'code': current['code'], 'at': current['at']},
            'pending': pending and {'code': pending['code'], 'state': pending['state']}}


def change_invite(store, code, request_id):
    """更换邀请码：复用 sync.enqueue_invite（不合规的码抛 GuestError，就地显示）。"""
    if not isinstance(code, str):
        raise GuestError('邀请码无效')
    payload = {'op': 'invite', 'code': code.strip()}
    with _write_lock:
        with store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            prev = _replay(c, request_id, payload)
            if prev:
                return prev
            try:
                sync.enqueue_invite(c, code)
            except sync.ContractError as e:
                raise GuestError(str(e)) from None
            result = {'ok': True, 'action': 'invite', 'invite': invite_state(c)}
            _remember(c, request_id, payload, result)
    return result


# ---------- 视图 ----------

def _rows(c, names=None):
    states = _command_states(c)
    links = dict(c.execute('SELECT account_id,customer_id FROM guest_links').fetchall())
    out = {'pending': [], 'linked': [], 'disabled': []}
    for a in c.execute('SELECT * FROM guest_accounts ORDER BY created_at DESC,account_id').fetchall():
        s = states.get(a['account_id'], {})
        cid = links.get(a['account_id'])
        row = {'account_id': a['account_id'], 'username': a['username'], 'nickname': a['nickname'],
               'created_at': a['created_at'], 'customer_id': cid,
               'customer_name': (names or {}).get(cid) if cid else None,
               'disable': s.get('disable'), 'reset': s.get('reset')}
        if a['status'] == 'disabled' or 'disabled_done' in s:
            row['disable'] = s.get('disabled_done') or {'state': 'done', 'command_id': None, 'at': None, 'error': None}
            out['disabled'].append(row)
        elif cid:
            out['linked'].append(row)
        else:
            out['pending'].append(row)
    return out


def view(store, gc):
    """06 客人整页数据：三个列表、计数、当前邀请码、资料库状态、名单拉取时间与上次错误。"""
    available = gc_available(gc)
    names = {c['customer_id']: c['display_name'] for c in gc_customers(gc, include_skipped=True)} if available else {}
    with store.connect() as c:
        rows = _rows(c, names)
        invite = invite_state(c)
        kv = dict(c.execute('SELECT k,v FROM kv WHERE k IN (?,?)', (sync.KV_ACCOUNTS_AT, sync.KV_ACCOUNTS_ERROR)).fetchall())
    return dict(rows, count=len(rows['pending']), invite=invite, gc={'available': available},
                pulled_at=kv.get(sync.KV_ACCOUNTS_AT), pull_error=kv.get(sync.KV_ACCOUNTS_ERROR))
