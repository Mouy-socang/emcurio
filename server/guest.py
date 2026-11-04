"""客人页面路由（CAP-9、CAP-13；AD-6、AD-8、AD-18）：注册/登录、找回、设置新密码、登录过期、停用、说明页、首页与单件页。

- 服务器端渲染 HTML，表单是普通 POST（application/x-www-form-urlencoded），不依赖 JS；`static/guest.js` 只做增强。
- 未登录访问任何客人路径（含 `/item/<货号>`）都只显示注册/登录页，原路径作为 `next` 带在表单里，完成后 303 回去。
- 免登录的只有：注册/登录/找回/设置新密码页、说明页 `/legal`、静态外壳 `/static/`、`/healthz`。
- 首页（故事 10）：今日榜横滑 + 全部藏品（品牌分区、器型/价格区间/图案筛选、双列瀑布流、每批 20 件续载）；
  筛选与续载都是普通链接（不跑 JS 也能用），`static/guest.js` 增强为就地刷新与滚到底自动续载（`/api/pool`）。
- 单件页（故事 12）：画廊（左右滑、上一张/下一张、缩略图、页码、放大层）→ 货号 → 名称 → 卖家原价 → 01 品相（卖家原文）
  → 02 预估费用（档位与金额，或"请咨询"）→ 03 墨墨点评（有才出现）→ 复制货号 → 浏览全部。隐藏后 90 天内去色并标
  "已售出 / 已移出"（无复制栏），超过 90 天或不在快照里显示"此件已不在展出"。
- 币种（故事 12，AD-14）：页头 €/¥ 切换，选择存在账号（accounts.currency，POST /currency，无 JS 时是表单提交）；
  ¥ 按快照汇率换算并标"约"，汇率缺失或日期距今超过 7 天时只显示欧元、不出现切换。
- 行为事件（故事 15，AD-25）：已登录客人的首页与单件页带 `data-track-page` 标记（含账号标记 `data-who`，
  HMAC 前 16 位），guest.js 据此生成事件并经 `POST /events`（app.py / inbox.py）上报；币种切换由 `POST /currency`
  在服务器端记录（值变了才记）。Mac 预览与停用、待同意页不带标记、不上报。
- 千人千面（故事 18，AD-11）：首页"全部藏品"、筛选与续载（`/api/pool`）按当前会话账号在快照 orders 里的顺序排
  （snapshot.View.for_account）；今日榜、单件页不变；没有本人顺序时按墨墨顺序。Mac 预览不带 orders（墨墨顺序）。
- 快照数据（`/api/pool`）与图片（`/img/<name>`）只经会话鉴权提供（AD-6）：未登录 401、停用或待同意 403。
"""
import html
import logging
import math
import re
import time
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape
from starlette.concurrency import run_in_threadpool

import accounts
from accounts import MSG
import mailer as mailer_mod
import snapshot

log = logging.getLogger('curiobject.server')
HERE = Path(__file__).resolve().parent
TEMPLATES = HERE / 'templates'
STATIC = HERE / 'static'
DEFAULT_TERMS = HERE / 'legal' / 'terms.md'
COOKIE = 'curio_session'
MAX_FORM = 16_384
_ITEM_RE = re.compile(r'/item/CO-[0-9]{4,8}')
REG_FIELDS = ('invite', 'username', 'password', 'nickname', 'email', 'consent')
CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; form-action 'self'; "
       "frame-ancestors 'none'; base-uri 'none'")
PAGE_SIZE = 20                       # 每批件数（自动续载）
MAX_SHOWN = 2000
CN_TZ = timezone(timedelta(hours=8))   # 客人在中国：更新时间按北京时间显示
FILTER_LABELS = {'form': '器型', 'band': '价格区间', 'pattern': '图案'}
FX_MAX_DAYS = 7                       # AD-14：汇率日期距今超过 7 天只显示欧元
# 预估费用档位的展示文字（fee-tiers.md；金额来自快照，这里只有名称）。
FEE_LABELS = {'A': '普通杯碟', 'B': '30cm 以下花瓶', 'C': '30–50cm', 'D': '50cm–1m'}


# ---------- 用户协议与隐私说明（AD-18：文本与版本号同一文件） ----------

def load_terms(path):
    """读 terms.md：前置块里的 version + 正文（极简 Markdown：##、-、>、**粗体**、段落）。返回 (version, html)。"""
    text = Path(path).read_text(encoding='utf-8-sig').replace('\r\n', '\n').replace('\r', '\n')
    m = re.match(r'---\n(.*?)\n---\n', text, re.S)
    if not m:
        raise ValueError('terms.md 缺少前置版本块')
    version = None
    for line in m.group(1).splitlines():
        k, _, v = line.partition(':')
        if k.strip() == 'version':
            version = v.strip()
    if not version:
        raise ValueError('terms.md 缺少 version')
    return version, _render_md(text[m.end():])


def _inline(s):
    s = html.escape(s)
    return re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', s)


def _render_md(body):
    out, para, items = [], [], []

    def flush():
        if para:
            out.append('<p>' + _inline(' '.join(para)) + '</p>')
            para.clear()
        if items:
            out.append('<ul>' + ''.join('<li>' + _inline(i) + '</li>' for i in items) + '</ul>')
            items.clear()
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            flush()
        elif line.startswith('# '):
            flush()            # 页面 h1 由模板给出
        elif line.startswith('## '):
            flush()
            title = line[3:]
            m = re.search(r'\s*\{#([a-z][a-z0-9-]*)\}$', title)
            anchor = ''
            if m:
                anchor = f' id="{m.group(1)}"'
                title = title[:m.start()]
            out.append(f'<h2{anchor}>' + _inline(title) + '</h2>')
        elif line.startswith('- '):
            if para:
                flush()
            items.append(line[2:])
        elif line.startswith('> '):
            flush()
            out.append('<blockquote><p>' + _inline(line[2:]) + '</p></blockquote>')
        else:
            if items:
                flush()
            para.append(line)
    flush()
    return '\n'.join(out)


# ---------- 首页与单件页的显示小工具 ----------

def price_text(value):
    """€185 / €1,250 / €99.50（欧元为准）。"""
    if isinstance(value, float) and not value.is_integer():
        return '€{:,.2f}'.format(value)
    return '€{:,}'.format(int(value))


# ---------- 币种（CAP-11、AD-14：欧元为准，¥ 只是按快照汇率的约数） ----------

def fx_usable(rate, date, now):
    """快照里的汇率可用：有汇率与日期，且日期距今不超过 7 天（按 UTC 日期）。"""
    if not isinstance(rate, (int, float)) or isinstance(rate, bool) or not math.isfinite(rate) or not 1 <= rate <= 50:
        return False
    try:
        day = datetime.strptime(date, '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return False
    today = datetime.fromtimestamp(now, timezone.utc).date()
    return 0 <= (today - day).days <= FX_MAX_DAYS          # 未来日期（时钟或数据有误）不用


def currency_ctx(chosen, view, now, local=False):
    """本页的币种显示：chosen 为账号里存的选择（EUR / CNY）。汇率不可用时一律欧元、不出现切换。
    local：预览里切换只在页面内生效、不保存。"""
    ok = fx_usable(view.fx_rate, view.fx_date, now)
    chosen = chosen if chosen in ('EUR', 'CNY') else 'EUR'
    return {'code': 'CNY' if chosen == 'CNY' and ok else 'EUR', 'chosen': chosen,
            'rate': float(view.fx_rate) if ok else None, 'available': ok,
            'fallback': chosen == 'CNY' and not ok, 'local': local}


EUR_ONLY = {'code': 'EUR', 'chosen': 'EUR', 'rate': None, 'available': False, 'fallback': False, 'local': False}


def cny(value, rate):
    """欧元 → 人民币整数（四舍五入）。"""
    return int(Decimal(repr(float(value) * rate)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def money_text(value, cur=None, approx=False):
    """读屏与 aria-label 用的纯文字：€185 / 约 €42 / 约 1443 元人民币。"""
    cur = cur or EUR_ONLY
    if cur['code'] == 'CNY':
        return f'约 {cny(value, cur["rate"])} 元人民币'
    return ('约 ' if approx else '') + price_text(value)


def money(value, cur=None, approx=False):
    """价格（HTML）：€185；approx 时"约 €42"；人民币显示"≈ ¥1,443"、读作"约 1443 元人民币"。
    data-eur 带欧元原值，页内切换币种时由 guest.js 就地换算。"""
    cur = cur or EUR_ONLY
    attrs = f' data-eur="{escape(repr(float(value)))}"' + (' data-approx' if approx else '')
    if cur['code'] == 'CNY':
        n = cny(value, cur['rate'])
        inner = (f'<span aria-hidden="true">≈ ¥{n:,}</span><span class="vh">约 {n} 元人民币</span>')
    else:
        inner = escape(('约 ' if approx else '') + price_text(value))
    return Markup(f'<span class="m"{attrs}>{inner}</span>')


def updated_text(received_at):
    try:
        t = datetime.fromisoformat(received_at.replace('Z', '+00:00')).astimezone(CN_TZ)
    except (AttributeError, ValueError):
        return None
    return t.strftime('%Y.%m.%d · %H:%M')


def auction_end_text(ends):
    """拍卖结束时间（故事 30）：按北京时间（客人在中国，同更新时间）显示为「11月5日 21:05 结束（北京时间）」；读不出时空。"""
    try:
        t = datetime.fromisoformat(ends.replace('Z', '+00:00')).astimezone(CN_TZ)
    except (AttributeError, ValueError):
        return ''
    return '%d月%d日 %02d:%02d 结束（北京时间）' % (t.month, t.day, t.hour, t.minute)


def chosen_filters(params, options):
    """只认商品池里实际有的取值；其它忽略。"""
    out = {}
    for key, values in options.items():
        v = params.get(key)
        if v and any(v == k for k, _, _ in values):
            out[key] = v
    return out


def filter_query(chosen, **change):
    q = dict(chosen)
    for k, v in change.items():
        if v is None:
            q.pop(k, None)
        else:
            q[k] = v
    return urlencode([(k, q[k]) for k in ('brand', 'form', 'band', 'pattern') if k in q])


def filter_href(chosen, **change):
    q = filter_query(chosen, **change)
    return '/?' + q + '#pool' if q else '/#pool'


def label_of(options, key, value):
    return next((label for k, label, _ in options[key] if k == value), value)


def summary_labels(options, chosen):
    return [label_of(options, k, chosen[k]) for k in ('brand', 'form', 'band', 'pattern') if k in chosen]


def _int(value, default, low, high):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, n))


# ---------- 小工具 ----------

_HOME_QUERY_RE = re.compile(r'/\?(?:(?:brand|form|band|pattern|n)=[A-Za-z0-9%+._-]{1,120}&?){1,5}')


def safe_next(value):
    """只接受 `/`、带筛选参数的首页 `/?brand=…&form=…`（只认 brand/form/band/pattern/n）与 `/item/CO-NNNN`；
    其它一律回首页（防开放跳转）。"""
    if isinstance(value, str) and (value == '/' or _ITEM_RE.fullmatch(value) or _HOME_QUERY_RE.fullmatch(value)):
        return value
    return '/'


def client_ip(request):
    """Caddy 反代时 uvicorn 已按 X-Forwarded-For 改写 client；直连本机且带该头时取最后一跳。"""
    host = request.client.host if request.client else ''
    if host in ('127.0.0.1', '::1'):
        fwd = request.headers.get('x-forwarded-for')
        if fwd:
            return fwd.split(',')[-1].strip()[:64]
    return host[:64]


def same_origin(request):
    """拒绝跨站表单提交。Origin 缺失或为 'null'（no-referrer 等）时退回看 Sec-Fetch-Site；两者都没有就拒绝。"""
    origin = request.headers.get('origin')
    if origin is None or origin == 'null':
        return request.headers.get('sec-fetch-site') in ('same-origin', 'none')
    return urlsplit(origin).netloc == request.headers.get('host', '')


async def read_form(request):
    """返回表单字典（每个键取第一个值）；不合格返回 None。"""
    ctype = request.headers.get('content-type', '')
    if not ctype.startswith('application/x-www-form-urlencoded'):
        return None
    declared = request.headers.get('content-length')
    if declared is not None and (not declared.isdigit() or int(declared) > MAX_FORM):
        return None
    body = await request.body()
    if len(body) > MAX_FORM:
        return None
    try:
        parsed = parse_qs(body.decode('utf-8'), keep_blank_values=True, max_num_fields=20)
    except (UnicodeDecodeError, ValueError):
        return None
    return {k: v[0] for k, v in parsed.items()}


def minutes(seconds):
    return max(1, math.ceil(seconds / 60))


# ---------- 纯渲染入口（服务器路由与 Mac 预览共用，AD-28） ----------

def make_env():
    """客人页模板环境（server/templates）。Mac 预览经 server/preview.py 用同一个环境渲染。"""
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=select_autoescape(['html']))
    env.globals.update(price_text=price_text, filter_href=filter_href, money=money, money_text=money_text,
                       fee_labels=FEE_LABELS, auction_end_text=auction_end_text)
    return env


def render_page(env, name, title, status=200, **ctx):
    """返回 (status, html)。"""
    return status, env.get_template(name).render(title=title, msg=MSG, **ctx)


def pool_state(view, params):
    options = snapshot.filter_options(view.pool)
    chosen = chosen_filters(params, options)
    shown = snapshot.apply_filters(view.pool, chosen)
    return options, chosen, shown


def home_page(env, view, params, cur=None, stale=False, track=False):
    """首页：今日榜 + 全部藏品（筛选、首批卡片）。cur：currency_ctx 结果（缺省欧元）。
    stale：已发布货下架检查超过 2 小时未成功（AD-10），页头下方显示状态未更新提示。
    track：行为事件的账号标记（accounts.track_tag，故事 15）；只有服务器上已登录的客人页给出，Mac 预览（AD-28）不带。
    返回 (status, html)。"""
    cur = cur or EUR_ONLY
    options, chosen, shown = pool_state(view, params)
    n = _int(params.get('n'), PAGE_SIZE, PAGE_SIZE, MAX_SHOWN)
    first = shown[:n]
    labels = summary_labels(options, chosen)
    more = filter_query(chosen)
    more = (more + '&' if more else '') + f'n={len(first) + PAGE_SIZE}'
    return render_page(env, 'home.html', 'MyR12', view=view, rank=view.rank, pool_total=len(view.pool),
                       rank_auction=getattr(view, 'rank_auction', []),
                       options=options, chosen=chosen, cards=first, shown_total=len(shown), labels=labels,
                       next_offset=len(first) if len(first) < min(len(shown), MAX_SHOWN) else None,
                       query=filter_query(chosen),
                       more_href='/?' + more + '#pool', updated=updated_text(view.received_at),
                       chosen_labels={k: label_of(options, k, v) for k, v in chosen.items()},
                       filter_labels=FILTER_LABELS, cur=cur, here='/?' + filter_query(chosen) if chosen else '/',
                       stale=stale, track=track)


def item_view_page(env, view, code, cur=None, now=None, stale=False, track=False):
    """单件页。now：Unix 秒，用于 90 天保留期（None 时不判）。stale、track 同 home_page。返回 (status, html)。"""
    cur = cur or EUR_ONLY
    state = view.item_state(code, now)
    it = view.items.get(code)
    here = '/item/' + code
    if state == 'show':
        return render_page(env, 'item.html', it['title'], state=state, it=it, code=code, cur=cur, here=here,
                           stale=stale, track=track)
    if state in ('sold', 'removed'):
        title = it['title'] if it else ('已售出' if state == 'sold' else '已移出')
        return render_page(env, 'item.html', title, state=state, it=it, code=code, cur=cur, here=here, stale=stale, track=track)
    return render_page(env, 'item.html', '此件已不在展出', 404, state=None, it=None, code=code, cur=cur, here=here,
                       stale=stale)


def pool_batch(env, view, params, cur=None):
    """自动续载的下一批卡片（HTML 片段，与首页同一模板）。返回 JSON 字典。
    params.pn：页面首批所属的发布编号；与当前快照不同时 reset=True，从头给第一批（页面整列替换）。"""
    cur = cur or EUR_ONLY
    _, _, shown = pool_state(view, params)
    # 最多显示 MAX_SHOWN 件：到上限就停，不再给下一批（否则会反复返回同一批）。
    limit = min(len(shown), MAX_SHOWN)
    offset = _int(params.get('offset'), 0, 0, limit)
    # 续载期间换了新快照（顺序可能整体变了，故事 18）：按偏移续下去会重复或漏掉，改为从头给第一批，页面整列替换。
    reset = params.get('pn') not in (None, '') and params.get('pn') != str(view.publish_no)
    if reset:
        offset = 0
    batch = shown[offset:min(offset + PAGE_SIZE, limit)]
    cards = env.get_template('_pool.html').module
    html_ = ''.join(str(cards.card(it, cur)) for it in batch)
    end = offset + len(batch)
    return {'ok': True, 'html': html_, 'count': len(batch), 'shown': end, 'total': len(shown),
            'next_offset': end if end < limit else None, 'publish_no': view.publish_no, 'reset': reset}


def register_guest_routes(app, db_path, connect, clock=None, mailer=None, cookie_secure=True,
                          public_url=None, terms_path=None, record_event=None):
    """record_event(c, account_id, type, params, now)：服务器自己记行为事件（币种切换，故事 15）；None 时不记。"""
    clock = clock or time.time
    terms_version, terms_html = load_terms(terms_path or DEFAULT_TERMS)
    env = make_env()
    send_mail = mailer if mailer is not None else mailer_mod.from_env()
    app.state.terms_version = terms_version

    def db():
        import sqlite3
        c = connect(db_path)
        c.row_factory = sqlite3.Row
        return c

    def page(name, title, status=200, **ctx):
        status, body = render_page(env, name, title, status, **ctx)
        return HTMLResponse(body, status_code=status)

    def set_session(resp, token):
        resp.set_cookie(COOKIE, token, max_age=accounts.SESSION_SECONDS, httponly=True, secure=cookie_secure,
                        samesite='lax', path='/')

    def clear_session(resp):
        resp.delete_cookie(COOKIE, path='/', httponly=True, secure=cookie_secure, samesite='lax')

    def auth_page(mode, next_path, status=200, reg=None, login=None):
        c = db()
        try:
            open_ = accounts.invite_open(c)
        finally:
            c.close()
        reg = dict({'errors': {}, 'consent': False}, **(reg or {}))
        login = dict({'errors': {}}, **(login or {}))
        title = '注册' if mode == 'register' else '登录'
        return page('auth.html', title, status, mode=mode, next=next_path, reg=reg, login=login, invite_open=open_)

    def current(request):
        """返回 (state, account)：'none' / 'ok' / 'expired'。"""
        c = db()
        try:
            return accounts.lookup_session(c, request.cookies.get(COOKIE), clock())
        finally:
            c.close()

    app.mount('/static', StaticFiles(directory=str(STATIC)), name='static')

    @app.middleware('http')
    async def security_headers(request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault('Referrer-Policy', 'same-origin')   # no-referrer 会让表单 POST 带 Origin: null
        resp.headers.setdefault('X-Frame-Options', 'DENY')
        resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
        if resp.headers.get('content-type', '').startswith('text/html'):
            resp.headers.setdefault('Content-Security-Policy', CSP)
            resp.headers.setdefault('Cache-Control', 'no-store')
        return resp

    # ----- 说明页（免登录） -----

    @app.get('/legal')
    def legal(request: Request):
        return page('legal.html', '用户协议与隐私说明', version=terms_version, html=terms_html, back='/')

    # ----- 注册 / 登录页 -----

    def login_or_register(request, mode):
        next_path = safe_next(request.query_params.get('next', '/'))
        state, account = current(request)
        if state == 'ok':
            return RedirectResponse(next_path, status_code=303)
        if state == 'expired':
            resp = auth_page('login', next_path, login={'notice': MSG['expired'],
                                                        'username': account['username'] if account else '',
                                                        'focus': 'password' if account else 'username'})
            clear_session(resp)
            return resp
        login = {}
        if mode == 'login' and request.query_params.get('done') == 'reset':
            login['notice'] = MSG['reset_done']
        return auth_page(mode, next_path, login=login)

    @app.get('/login')
    def login_page(request: Request):
        return login_or_register(request, 'login')

    @app.get('/register')
    def register_page(request: Request):
        return login_or_register(request, 'register')

    @app.post('/register')
    async def register(request: Request):
        if not same_origin(request):
            return HTMLResponse('来源不符', status_code=403)
        form = await read_form(request)
        if form is None:
            return HTMLResponse('请求无效', status_code=400)
        return await run_in_threadpool(_register, form, client_ip(request))

    def _register(form, ip):
        now = clock()
        next_path = safe_next(form.get('next', '/'))
        clean, errors = accounts.validate_registration(form)
        kept = {'invite': form.get('invite', '')[:64], 'username': form.get('username', '')[:64],
                'nickname': form.get('nickname', '')[:64], 'email': form.get('email', '')[:256],
                'consent': form.get('consent') == 'yes'}
        user_key = clean['username_key'] or None
        c = db()
        try:
            # 先在一个事务里检查并记下这次尝试，再做耗时的哈希（并发请求不能一起越过上限）。
            wait, ids = accounts.reserve(c, 'register', now, ip=ip, user_key=user_key)
            if wait:
                return auth_page('register', next_path, 429,
                                 reg=dict(kept, rate=True, rate_minutes=minutes(wait)))
            invite = accounts.check_invite(c, clean['invite'])
            if invite == 'closed':
                errors['invite'] = MSG['invite_closed']
            elif invite == 'wrong':
                errors['invite'] = MSG['invite_wrong']
            elif 'username' not in errors and accounts.username_taken(c, clean['username_key']):
                errors['username'] = MSG['username_taken']
            if not errors:
                try:
                    account_id = accounts.create_account(c, clean, terms_version, now)
                except accounts.UsernameTaken:
                    errors['username'] = MSG['username_taken']
            if errors:
                focus = next(f for f in REG_FIELDS if f in errors)
                return auth_page('register', next_path, 400, reg=dict(kept, errors=errors, focus=focus))
            accounts.release(c, ids, 'user')    # 用户名只数失败
            token = accounts.create_session(c, account_id, now)
        finally:
            c.close()
        resp = RedirectResponse(next_path, status_code=303)
        set_session(resp, token)
        return resp

    @app.post('/login')
    async def login(request: Request):
        if not same_origin(request):
            return HTMLResponse('来源不符', status_code=403)
        form = await read_form(request)
        if form is None:
            return HTMLResponse('请求无效', status_code=400)
        return await run_in_threadpool(_login, form, client_ip(request))

    def _login(form, ip):
        now = clock()
        next_path = safe_next(form.get('next', '/'))
        username = form.get('username', '')
        _, key = accounts.username_key(username)
        c = db()
        try:
            wait, ids = accounts.reserve(c, 'login', now, ip=ip, user_key=key or None)
            if wait:
                return auth_page('login', next_path, 429, login={'username': username[:64], 'rate': True,
                                                                  'rate_minutes': minutes(wait)})
            account = accounts.authenticate(c, username, form.get('password', ''))
            if account is None:       # 预先记下的那次尝试即为这次失败
                return auth_page('login', next_path, 401, login={'username': username[:64], 'focus': 'password',
                                                                  'errors': {'password': MSG['login_wrong']}})
            accounts.release(c, ids, 'ip')       # 登录只数失败
            accounts.clear_user(c, 'login', key)
            token = accounts.create_session(c, account['account_id'], now)
        finally:
            c.close()
        resp = RedirectResponse(next_path, status_code=303)
        set_session(resp, token)
        return resp

    @app.post('/logout')
    async def logout(request: Request):
        if not same_origin(request):
            return HTMLResponse('来源不符', status_code=403)
        token = request.cookies.get(COOKIE)

        def work():
            c = db()
            try:
                accounts.revoke_session(c, token, clock())
            finally:
                c.close()
        await run_in_threadpool(work)
        resp = RedirectResponse('/login', status_code=303)
        clear_session(resp)
        return resp

    # ----- 条款更新后重新同意（AD-18） -----

    def consent_missing(account):
        c = db()
        try:
            return accounts.needs_consent(c, account['account_id'], terms_version)
        finally:
            c.close()

    @app.post('/consent')
    async def consent(request: Request):
        if not same_origin(request):
            return HTMLResponse('来源不符', status_code=403)
        form = await read_form(request)
        if form is None:
            return HTMLResponse('请求无效', status_code=400)
        state, account = current(request)
        next_path = safe_next(form.get('next', '/'))
        if state != 'ok' or account['status'] != 'active':
            return RedirectResponse(next_path, status_code=303)
        if form.get('consent') != 'yes':
            return page('consent.html', '确认用户协议', 400, next=next_path, error=MSG['consent'])
        c = db()
        try:
            accounts.record_consent(c, account['account_id'], terms_version, clock())
        finally:
            c.close()
        return RedirectResponse(next_path, status_code=303)

    # ----- 找回与设置新密码 -----

    @app.get('/forgot')
    def forgot_page(request: Request):
        return page('forgot.html', '找回密码', username='')

    @app.post('/forgot')
    async def forgot(request: Request):
        if not same_origin(request):
            return HTMLResponse('来源不符', status_code=403)
        form = await read_form(request)
        if form is None:
            return HTMLResponse('请求无效', status_code=400)
        # 重设链接只用配置的站点地址；Host 头可被伪造，只在本机 http 测试（Cookie 不带 Secure）时退而用它。
        base = public_url or (None if cookie_secure else str(request.base_url).rstrip('/'))
        return await run_in_threadpool(_forgot, form, client_ip(request), base)

    def _forgot(form, ip, base):
        now = clock()
        username = form.get('username', '')
        _, key = accounts.username_key(username)
        shown = username[:64]
        c = db()
        try:
            wait, _ = accounts.reserve(c, 'forgot', now, ip=ip, user_key=key or None)
            if wait:
                return page('forgot.html', '找回密码', 429, username=shown, rate=True, rate_minutes=minutes(wait),
                            result=MSG['rate'])
            if base is None:
                log.warning('未设置 CURIO_PUBLIC_URL：不发重设邮件')
                return page('forgot.html', '找回密码', 503, username=shown, failed=True,
                            result=MSG['reset_unavailable'])
            result, account, token = accounts.start_reset(c, username, now)
        finally:
            c.close()
        if result == 'no_email':
            return page('forgot.html', '找回密码', username=shown, result=MSG['reset_no_email'])
        if result == 'disabled':
            return page('forgot.html', '找回密码', username=shown, result=MSG['reset_disabled'])
        if result == 'sent':
            link = f'{base}/reset?token={token}'
            body = (f'{account["nickname"]}，您好：\n\n'
                    f'请在 1 小时内打开下面的链接，为用户名 {account["username"]} 设置新密码：\n\n{link}\n\n'
                    '链接只能使用一次。如果不是您本人操作，请忽略这封邮件，原密码不变。\n\n— Curiobject')
            try:
                send_mail.send(account['email'], 'Curiobject 重设密码', body)
            except Exception:
                return page('forgot.html', '找回密码', 503, username=shown, failed=True,
                            result='邮件未能发出，请稍后再试。')
        # 用户名不存在时同样显示"已发送"，不泄露账号是否存在。
        return page('forgot.html', '找回密码', username=shown, result=MSG['reset_sent'])

    @app.get('/reset')
    def reset_page(request: Request):
        token = request.query_params.get('token', '')
        c = db()
        try:
            valid = accounts.check_reset(c, token, clock()) is not None
        finally:
            c.close()
        return page('reset.html', '设置新密码', 200 if valid else 410, valid=valid, token=token if valid else '')

    @app.post('/reset')
    async def reset(request: Request):
        if not same_origin(request):
            return HTMLResponse('来源不符', status_code=403)
        form = await read_form(request)
        if form is None:
            return HTMLResponse('请求无效', status_code=400)
        return await run_in_threadpool(_reset, form)

    def _reset(form):
        now = clock()
        token = form.get('token', '')
        password = form.get('password', '')
        c = db()
        try:
            if accounts.check_reset(c, token, now) is None:
                return page('reset.html', '设置新密码', 410, valid=False, token='')
            error = accounts.validate_password(password)
            if error:
                return page('reset.html', '设置新密码', 400, valid=True, token=token, error=error)
            if not accounts.finish_reset(c, token, password, now):
                return page('reset.html', '设置新密码', 410, valid=False, token='')
        finally:
            c.close()
        resp = RedirectResponse('/login?done=reset', status_code=303)
        clear_session(resp)
        return resp

    # ----- 首页、单件页、快照数据与图片（AD-6：只经会话鉴权） -----

    def api_gate(request):
        """快照数据与图片的会话鉴权：返回 None（放行）或错误响应。"""
        state, account = current(request)
        if state != 'ok':
            return JSONResponse({'ok': False, 'detail': '请先登录'}, status_code=401)
        if account['status'] == 'disabled' or consent_missing(account):
            return JSONResponse({'ok': False, 'detail': '无权查看'}, status_code=403)
        return None

    def load_view():
        c = db()
        try:
            return snapshot.current_view(c, db_path, clock())
        finally:
            c.close()

    @app.get('/img/{name}')
    def image(name: str, request: Request):
        denied = api_gate(request)
        if denied:
            return denied
        path = snapshot.image_dir(db_path) / name
        if not snapshot.IMAGE_RE.fullmatch(name) or not path.is_file():
            return JSONResponse({'ok': False, 'detail': '图片不存在'}, status_code=404)
        # 文件名即内容哈希、不可变：私有长缓存。
        return FileResponse(path, media_type='image/jpeg',
                            headers={'Cache-Control': 'private, max-age=31536000, immutable'})

    def cur_for(account, view):
        chosen = account['currency'] if account is not None and 'currency' in account.keys() else 'EUR'
        return currency_ctx(chosen, view, clock())

    @app.get('/api/pool')
    def api_pool(request: Request):
        """自动续载：返回下一批卡片（HTML 片段，与首页同一模板）。"""
        denied = api_gate(request)
        if denied:
            return denied
        _, account = current(request)
        view = load_view().for_account(account['account_id'])
        return JSONResponse(pool_batch(env, view, request.query_params, cur_for(account, view)),
                            headers={'Cache-Control': 'no-store'})

    def stale_now():
        """AD-10：页面加载时读一次心跳。"""
        c = db()
        try:
            return snapshot.heartbeat_stale(c, clock())
        finally:
            c.close()

    def home(params, account):
        view = load_view().for_account(account['account_id'])     # 全部藏品按本人顺序（AD-11），榜不变
        status, body = home_page(env, view, params, cur_for(account, view), stale_now(),
                                 track=accounts.track_tag(account['account_id']))
        return HTMLResponse(body, status_code=status)

    def item_page(code, account):
        view = load_view()
        status, body = item_view_page(env, view, code, cur_for(account, view), clock(), stale_now(),
                                      track=accounts.track_tag(account['account_id']))
        return HTMLResponse(body, status_code=status)

    @app.post('/currency')
    async def currency(request: Request):
        """改显示币种（CAP-11）：存进账号（换设备沿用），递增 account_rev。
        页面脚本以 Accept: application/json 调用、就地换算；不跑脚本时是普通表单提交，303 回到原页面。"""
        wants_json = 'application/json' in request.headers.get('accept', '')
        if not same_origin(request):
            return (JSONResponse({'ok': False, 'detail': '来源不符'}, status_code=403) if wants_json
                    else HTMLResponse('来源不符', status_code=403))
        form = await read_form(request)
        if form is None or form.get('currency') not in accounts.CURRENCIES:
            return (JSONResponse({'ok': False, 'detail': '请求无效'}, status_code=400) if wants_json
                    else HTMLResponse('请求无效', status_code=400))
        next_path = safe_next(form.get('next', '/'))
        state, account = current(request)
        if state != 'ok':
            if wants_json:
                return JSONResponse({'ok': False, 'detail': '请先登录'}, status_code=401)
            return RedirectResponse('/login?' + urlencode({'next': next_path}), status_code=303)
        if account['status'] == 'disabled' or consent_missing(account):
            if wants_json:
                return JSONResponse({'ok': False, 'detail': '无权操作'}, status_code=403)
            return RedirectResponse(next_path, status_code=303)

        def work():
            c = db()
            try:
                now = clock()
                # 只在值真的变了时记一条 currency 事件（脚本与无脚本表单提交同一处记，不重复）。
                if accounts.set_currency(c, account['account_id'], form['currency'], now) and record_event:
                    try:
                        record_event(c, account['account_id'], 'currency', {'currency': form['currency']}, now)
                    except Exception:      # 记录失败不影响币种保存
                        log.warning('币种事件未能记录', exc_info=True)
            finally:
                c.close()
        await run_in_threadpool(work)
        if wants_json:
            return JSONResponse({'ok': True, 'currency': form['currency']}, headers={'Cache-Control': 'no-store'})
        return RedirectResponse(next_path, status_code=303)

    # ----- 客人路径（须登录）：放在最后，兜住其它所有 GET -----

    @app.get('/{path:path}')
    def guest(request: Request, path: str):
        full = '/' + path
        if full.startswith(('/admin', '/static', '/api/', '/img/')) or full in ('/docs', '/redoc', '/openapi.json'):
            return HTMLResponse('Not Found', status_code=404)
        state, account = current(request)
        next_path = safe_next(full)
        if state == 'none':
            return auth_page('register', next_path)
        if state == 'expired':
            resp = auth_page('login', next_path, login={'notice': MSG['expired'],
                                                        'username': account['username'] if account else '',
                                                        'focus': 'password' if account else 'username'})
            clear_session(resp)
            return resp
        if account['status'] == 'disabled':
            return page('disabled.html', '账号已停用', 403)
        if consent_missing(account):
            return page('consent.html', '确认用户协议', next=next_path)
        if full == '/':
            return home(request.query_params, account)
        if _ITEM_RE.fullmatch(full):
            return item_page(full[len('/item/'):], account)
        return HTMLResponse('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>页面不存在 · Curiobject</title>'
                            '<p>页面不存在。<a href="/">返回首页</a></p></html>', status_code=404)
