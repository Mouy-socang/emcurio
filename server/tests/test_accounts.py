"""故事 9：注册与登录（TestClient，不连任何外部主机；发信用内存假邮箱或本机文件）。

覆盖 I/O 矩阵每一行：注册成功、邀请码错、用户名重复、未勾选同意、登录失败与限频、未登录访问、会话过期、
找回（有/无邮箱）、重设后旧会话失效、停用、换邀请码。
"""
import re
import sqlite3
import stat
import uuid
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

import accounts
import app as server
import guest
import mailer

TOKEN = 'test-admin-token-not-real'
AUTH = {'Authorization': 'Bearer ' + TOKEN}
CODE = 'MOMO2026'
PASSWORD = 'meissen1710'
T0 = 1_800_000_000.0
SAME_ORIGIN = {'Origin': 'http://testserver'}   # 浏览器表单 POST 都带 Origin


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


class Inbox:
    def __init__(self):
        self.sent = []

    def send(self, to, subject, body):
        self.sent.append((to, subject, body))


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def inbox():
    return Inbox()


@pytest.fixture
def db(tmp_path):
    return tmp_path / 'server.db'


@pytest.fixture
def make(db, clock, inbox):
    def _make(**kw):
        opts = dict(db_path=db, admin_token=TOKEN, clock=clock, mailer=inbox, cookie_secure=False,
                    public_url='https://curio.test')
        opts.update(kw)
        return TestClient(server.create_app(**opts), follow_redirects=False, headers=SAME_ORIGIN)
    return _make


@pytest.fixture
def client(make):
    return make()


_seq = [0]


def set_invite(client, code=CODE, target_seq=None, command_id=None):
    if target_seq is None:
        _seq[0] += 1
        target_seq = 1000 + _seq[0]
    env = {'schema_version': 1, 'event_id': str(uuid.uuid4()), 'kind': 'set_invite_code',
           'created_at': '2026-11-03T08:00:00.000Z',
           'payload': {'schema_version': 1, 'command_id': command_id or str(uuid.uuid4()),
                       'target_seq': target_seq, 'code': code}}
    r = client.post('/admin/events', json=env, headers=AUTH)
    assert r.status_code == 200, r.text
    return env


def reg_form(**over):
    form = {'invite': CODE, 'username': 'wangjie', 'password': PASSWORD, 'nickname': '王姐', 'email': '',
            'consent': 'yes', 'next': '/'}
    form.update(over)
    return {k: v for k, v in form.items() if v is not None}


def register(client, **over):
    return client.post('/register', data=reg_form(**over))


def login(client, username='wangjie', password=PASSWORD, next='/'):
    return client.post('/login', data={'username': username, 'password': password, 'next': next})


def rows(db, sql, *args):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, args)]
    finally:
        c.close()


def logged_in_home(r):
    return r.status_code == 200 and '<h1 class="display">今日榜</h1>' in r.text and '/register' not in r.text


def logged_in_item(r):
    """已登录并看到单件页（故事 10）：没有发布快照时为 404"此件已不在展出"，不是注册/登录或同意页。"""
    return (r.status_code == 404 and '此件已不在展出' in r.text and 'data-signed-in' in r.text
            and '/register' not in r.text and 'action="/consent"' not in r.text)


# ---------- 注册 ----------

def test_register_success_logs_in_records_consent_and_returns_to_path(client, db, clock):
    set_invite(client)
    page = client.get('/item/CO-0001')
    assert page.status_code == 200 and '<h1 tabindex="-1">注册 Curiobject</h1>' in page.text
    assert 'name="next" value="/item/CO-0001"' in page.text
    r = register(client, next='/item/CO-0001', email='lily@example.com')
    assert r.status_code == 303 and r.headers['location'] == '/item/CO-0001'
    cookie = r.headers['set-cookie']
    assert 'HttpOnly' in cookie and 'SameSite=lax' in cookie.replace('Lax', 'lax')
    assert f'Max-Age={90 * 86400}' in cookie
    assert logged_in_item(client.get('/item/CO-0001'))
    [acct] = rows(db, 'SELECT * FROM accounts')
    assert acct['username'] == 'wangjie' and acct['nickname'] == '王姐' and acct['email'] == 'lily@example.com'
    assert acct['status'] == 'active' and acct['account_rev'] == 1
    assert acct['password_hash'].startswith('$argon2id$v=19$m=19456,t=2,p=1$')
    consents = rows(db, 'SELECT kind,terms_version,agreed_at FROM consents ORDER BY kind')
    version = guest.load_terms(guest.DEFAULT_TERMS)[0]
    assert [c['kind'] for c in consents] == ['cross_border_storage', 'terms_privacy']
    assert all(c['terms_version'] == version and c['agreed_at'] == accounts.iso(clock.now) for c in consents)


def test_no_plaintext_secrets_in_database(client, db, inbox):
    set_invite(client)
    r = register(client, email='lily@example.com')
    token = r.cookies.get(guest.COOKIE) or re.search(r'curio_session=([^;]+)', r.headers['set-cookie']).group(1)
    client.post('/forgot', data={'username': 'wangjie'})
    reset_token = parse_qs(urlsplit(re.search(r'https://\S+', inbox.sent[0][2]).group(0)).query)['token'][0]
    raw = b''
    for suffix in ('', '-wal'):
        p = db.with_name(db.name + suffix)
        if p.exists():
            raw += p.read_bytes()
    for secret in (PASSWORD, token, reset_token, CODE, CODE.lower()):
        assert secret.encode() not in raw


def test_register_form_fields_order_and_no_phone(client):
    set_invite(client)
    html = client.get('/').text
    names = re.findall(r'<input class="ti[^"]*" id="r-[a-z]+" name="([a-z]+)"', html)
    assert names == ['invite', 'username', 'password', 'nickname', 'email']
    assert 'type="tel"' not in html and '手机' not in html and '短信' not in html
    assert 'autocomplete="new-password"' in html and 'autocomplete="nickname"' in html
    assert '（数据存放在中国境外）' in html and 'href="/legal#terms"' in html and 'href="/legal#privacy"' in html
    # 未勾选同意：注册为 aria-disabled，并说明原因
    assert re.search(r'<button[^>]*data-consent-gate aria-disabled="true" aria-describedby="why-reg">注册</button>', html)
    assert '请先勾选同意用户协议与隐私政策' in html


def test_wrong_invite_keeps_other_fields_and_counts(client, db):
    set_invite(client)
    r = register(client, invite='MOMO2025', email='lily@example.com')
    assert r.status_code == 400
    assert '邀请码不正确，请向墨墨确认。' in r.text
    assert 'value="wangjie"' in r.text and 'value="王姐"' in r.text and 'value="lily@example.com"' in r.text
    assert 'value="MOMO2025"' in r.text and PASSWORD not in r.text
    assert re.search(r'id="r-code"[^>]*aria-invalid="true"[^>]*autofocus', r.text)
    assert rows(db, 'SELECT count(*) n FROM accounts')[0]['n'] == 0
    assert rows(db, 'SELECT count(*) n FROM attempts')[0]['n'] >= 2      # IP 与用户名各记一次


def test_invite_is_case_and_space_insensitive(client):
    set_invite(client)
    assert register(client, invite='  momo2026 ').status_code == 303


def test_duplicate_username_any_case(client):
    set_invite(client)
    assert register(client).status_code == 303
    other = make_client_like(client)
    r = register(other, username='WangJie')
    assert r.status_code == 400 and '此用户名已被使用，请换一个。' in r.text
    assert re.search(r'id="r-user"[^>]*aria-invalid="true"', r.text)


def make_client_like(client):
    """同一应用、不带 Cookie 的另一位访客。"""
    return TestClient(client.app, follow_redirects=False, headers=SAME_ORIGIN)


def test_username_taken_not_revealed_without_valid_invite(client):
    set_invite(client)
    register(client)
    r = register(make_client_like(client), invite='WRONG1', username='wangjie')
    assert '此用户名已被使用' not in r.text


def test_consent_required(client, db):
    set_invite(client)
    r = register(client, consent=None)
    assert r.status_code == 400 and '请先勾选同意用户协议与隐私政策' in r.text
    assert re.search(r'id="r-consent"[^>]*aria-invalid="true"', r.text)
    assert rows(db, 'SELECT count(*) n FROM accounts')[0]['n'] == 0


@pytest.mark.parametrize('field,value,message', [
    ('username', 'a', '用户名为 2–20 个字符'),
    ('username', 'a' * 21, '用户名为 2–20 个字符'),
    ('username', 'wang jie', '用户名为 2–20 个字符'),
    ('username', 'wang-jie', '用户名为 2–20 个字符'),
    ('password', '1234567', '密码至少 8 位。'),
    ('password', 'x' * 129, '密码最多 128 位。'),
    ('nickname', '', '请填写微信名/称呼'),
    ('nickname', '王' * 31, '请填写微信名/称呼'),
    ('email', 'not-an-email', '邮箱格式不正确。'),
])
def test_field_rules(client, field, value, message):
    set_invite(client)
    r = register(client, **{field: value})
    assert r.status_code == 400 and message in r.text


@pytest.mark.parametrize('username', ['王姐', 'Lily_SG', '王_2', 'ab', 'x' * 20])
def test_valid_usernames(client, username):
    set_invite(client)
    assert register(client, username=username).status_code == 303


def test_first_error_gets_focus(client):
    set_invite(client)
    r = register(client, invite='', username='a', password='short')
    assert len(re.findall(r'autofocus', r.text)) == 1
    assert re.search(r'id="r-code"[^>]*autofocus', r.text)


def test_registration_closed_without_invite(client, db):
    assert '注册暂未开放' in client.get('/').text
    r = register(client)
    assert r.status_code == 400 and '注册暂未开放' in r.text
    assert rows(db, 'SELECT count(*) n FROM accounts')[0]['n'] == 0


def test_register_rate_limit_by_username(client):
    set_invite(client)
    for _ in range(5):
        assert register(client, invite='WRONG1').status_code == 400
    r = register(client)                         # 正确邀请码也被限频
    assert r.status_code == 429 and '尝试次数过多，请稍后再试。' in r.text
    assert re.search(r'aria-disabled="true" aria-describedby="why-reg-rate">注册</button>', r.text)


# ---------- 登录 ----------

def test_login_failure_message_keeps_username_clears_password(client):
    set_invite(client)
    register(make_client_like(client))
    for bad in [('wangjie', 'wrong-password'), ('nobody', PASSWORD)]:
        r = login(client, *bad)
        assert r.status_code == 401 and '用户名或密码不正确。' in r.text
        assert f'value="{bad[0]}"' in r.text and bad[1] not in r.text
        assert re.search(r'id="l-pass"[^>]*aria-invalid="true"[^>]*autofocus', r.text)
        assert '<title>登录 · Curiobject</title>' in r.text


def test_login_rate_limited_from_sixth_failure(client, clock):
    set_invite(client)
    register(make_client_like(client))
    for _ in range(5):
        assert login(client, password='wrong-password').status_code == 401
    r = login(client)                            # 第 6 次：即使密码正确也拒绝
    assert r.status_code == 429 and '尝试次数过多，请稍后再试。' in r.text
    assert 'value="wangjie"' in r.text
    assert login(client, 'WANGJIE').status_code == 429   # 用户名不区分大小写
    clock.now += accounts.RATE_WINDOW + 1
    assert login(client).status_code == 303


def test_unknown_usernames_rate_limited_same_way(client):
    for _ in range(5):
        assert login(client, 'ghost').status_code == 401
    assert login(client, 'ghost').status_code == 429


def test_login_rate_limited_by_ip(client):
    for i in range(20):
        assert login(client, f'user{i}').status_code == 401
    r = login(client, 'someone_else')
    assert r.status_code == 429


def test_success_clears_username_failures(client):
    set_invite(client)
    register(make_client_like(client))
    for _ in range(4):
        login(client, password='wrong-password')
    assert login(client).status_code == 303
    for _ in range(4):
        assert login(make_client_like(client), password='wrong-password').status_code == 401


def test_unauthenticated_item_then_login_returns_there(client):
    set_invite(client)
    register(make_client_like(client))
    page = client.get('/item/CO-0001')
    assert page.status_code == 200 and 'name="next" value="/item/CO-0001"' in page.text
    assert '/login?next=/item/CO-0001' in page.text
    assert 'CO-0001' not in page.text.replace('/item/CO-0001', '')    # 不露货
    r = login(client, next='/item/CO-0001')
    assert r.status_code == 303 and r.headers['location'] == '/item/CO-0001'
    assert logged_in_item(client.get('/item/CO-0001'))


@pytest.mark.parametrize('bad', ['//evil.com', 'https://evil.com/', '/login', '/admin/events', '/\\evil',
                                 'javascript:alert(1)', '/a/../login', '/x?y=1', '/anything', '/item/CO-1',
                                 '/item/CO-0001/extra'])
def test_next_only_local_guest_paths(bad):
    assert guest.safe_next(bad) == '/'
    assert guest.safe_next('/item/CO-0001') == '/item/CO-0001' and guest.safe_next('/') == '/'


def test_login_page_redirects_when_logged_in(client):
    set_invite(client)
    register(client)
    r = client.get('/login?next=/item/CO-0002')
    assert r.status_code == 303 and r.headers['location'] == '/item/CO-0002'


def test_logout_invalidates_session(client):
    set_invite(client)
    register(client)
    r = client.post('/logout')
    assert r.status_code == 303 and r.headers['location'] == '/login'
    assert '<h1 tabindex="-1">登录 Curiobject</h1>' in client.get('/login').text


def test_logged_out_cookie_replay_is_expired(client):
    set_invite(client)
    r = register(client)
    token = re.search(r'curio_session=([^;]+)', r.headers['set-cookie']).group(1)
    client.post('/logout')
    other = make_client_like(client)
    other.cookies.set(guest.COOKIE, token)
    assert '登录已过期，请重新登录。' in other.get('/').text


# ---------- 会话过期 ----------

def test_session_expires_after_90_days_with_username_prefilled(client, clock):
    set_invite(client)
    register(client, next='/item/CO-0003')
    clock.now += 89 * 86400
    assert logged_in_home(client.get('/'))
    clock.now += 2 * 86400
    r = client.get('/item/CO-0003')
    assert r.status_code == 200 and '登录已过期，请重新登录。' in r.text
    assert '<title>登录 · Curiobject</title>' in r.text
    assert re.search(r'id="l-user"[^>]*value="wangjie"', r.text)
    assert 'name="next" value="/item/CO-0003"' in r.text
    assert 'Max-Age=0' in r.headers['set-cookie'] or 'expires=' in r.headers['set-cookie'].lower()
    assert login(client, next='/item/CO-0003').headers['location'] == '/item/CO-0003'


def test_unknown_cookie_is_treated_as_expired(client):
    client.cookies.set(guest.COOKIE, 'not-a-real-token')
    r = client.get('/')
    assert '登录已过期，请重新登录。' in r.text and '今日榜' not in r.text


def test_cookie_secure_by_default(make, monkeypatch):
    monkeypatch.delenv('CURIO_COOKIE_SECURE', raising=False)
    c = make(cookie_secure=None)
    set_invite(c)
    r = register(c)
    assert '; Secure' in r.headers['set-cookie'] or 'secure' in r.headers['set-cookie'].lower().split('; ')


# ---------- 找回与重设 ----------

def reset_link(inbox):
    return re.search(r'https://curio\.test/reset\?token=\S+', inbox.sent[-1][2]).group(0)


def test_forgot_with_email_sends_link_and_reset_flow(client, db, clock, inbox):
    set_invite(client)
    old = make_client_like(client)
    assert register(old, email='lily@example.com').status_code == 303
    page = client.get('/forgot')
    assert '<h1>找回密码</h1>' in page.text and '返回登录' in page.text
    r = client.post('/forgot', data={'username': 'WangJie'})
    assert r.status_code == 200 and '重设链接已发送，请到邮箱查看。' in r.text
    assert 'lily@example.com' not in r.text
    assert len(inbox.sent) == 1 and inbox.sent[0][0] == 'lily@example.com'
    link = reset_link(inbox)
    path = link.replace('https://curio.test', '')
    form = client.get(path)
    assert form.status_code == 200 and '<h1>设置新密码</h1>' in form.text and 'autocomplete="new-password"' in form.text
    token = parse_qs(urlsplit(link).query)['token'][0]
    assert client.post('/reset', data={'token': token, 'password': 'short'}).status_code == 400
    r = client.post('/reset', data={'token': token, 'password': 'new-password-1'})
    assert r.status_code == 303 and r.headers['location'] == '/login?done=reset'
    assert '新密码已保存，请重新登录。' in client.get('/login?done=reset').text
    # 旧会话已失效 → 登录表单
    assert '登录已过期，请重新登录。' in old.get('/').text
    assert login(client).status_code == 401
    assert login(client, password='new-password-1').status_code == 303
    # 令牌一次性
    again = client.get(path)
    assert again.status_code == 410 and '此链接已失效，请重新找回密码。' in again.text
    assert rows(db, 'SELECT account_rev FROM accounts')[0]['account_rev'] == 2


def test_reset_link_expires_after_one_hour(client, clock, inbox):
    set_invite(client)
    register(make_client_like(client), email='lily@example.com')
    client.post('/forgot', data={'username': 'wangjie'})
    path = reset_link(inbox).replace('https://curio.test', '')
    clock.now += 3599
    assert client.get(path).status_code == 200
    clock.now += 2
    r = client.get(path)
    assert r.status_code == 410 and '此链接已失效，请重新找回密码。' in r.text and 'href="/login"' in r.text
    token = parse_qs(urlsplit(path).query)['token'][0]
    assert client.post('/reset', data={'token': token, 'password': 'new-password-1'}).status_code == 410


def test_forgot_without_email(client, inbox):
    set_invite(client)
    register(make_client_like(client))
    r = client.post('/forgot', data={'username': 'wangjie'})
    assert '此账号未填写邮箱，请在微信中联系墨墨重置密码。' in r.text and inbox.sent == []


def test_forgot_unknown_user_does_not_reveal(client, inbox):
    r = client.post('/forgot', data={'username': 'nobody'})
    assert '重设链接已发送，请到邮箱查看。' in r.text and inbox.sent == []


def test_forgot_rate_limited_per_username(client, inbox):
    set_invite(client)
    register(make_client_like(client), email='lily@example.com')
    for _ in range(5):
        assert client.post('/forgot', data={'username': 'wangjie'}).status_code == 200
    r = client.post('/forgot', data={'username': 'wangjie'})
    assert r.status_code == 429 and '尝试次数过多，请稍后再试。' in r.text and len(inbox.sent) == 5


def test_mail_failure_is_reported(make):
    class Broken:
        def send(self, *a):
            raise mailer.MailError('x')
    c = make(mailer=Broken())
    set_invite(c)
    register(make_client_like(c), email='lily@example.com')
    r = c.post('/forgot', data={'username': 'wangjie'})
    assert r.status_code == 503 and '邮件未能发出' in r.text


def test_file_mailer_writes_private_eml(tmp_path):
    m = mailer.FileMailer(tmp_path / 'mail')
    path = m.send('lily@example.com', 'Curiobject 重设密码', '链接：https://curio.test/reset?token=abc')
    assert path.exists() and stat.S_IMODE(path.stat().st_mode) == 0o600
    text = path.read_text()
    assert 'To: lily@example.com' in text and 'Subject:' in text


def test_mailer_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv('CURIO_MAIL_DIR', str(tmp_path))
    monkeypatch.delenv('CURIO_MAILER', raising=False)
    assert isinstance(mailer.from_env(), mailer.FileMailer)
    monkeypatch.setenv('CURIO_MAILER', 'smtp')
    with pytest.raises(mailer.MailError):
        mailer.from_env()


# ---------- 停用 ----------

def test_disabled_account_sees_only_disabled_page(client, db, clock):
    set_invite(client)
    holder = make_client_like(client)
    register(holder)
    [acct] = rows(db, 'SELECT account_id FROM accounts')
    c = server.connect(db)
    c.row_factory = sqlite3.Row
    try:
        accounts.set_status(c, acct['account_id'], 'disabled', clock.now)
    finally:
        c.close()
    assert '登录已过期，请重新登录。' in holder.get('/').text     # 现有会话立即失效
    assert login(client, next='/item/CO-0001').status_code == 303
    for path in ('/', '/item/CO-0001'):
        r = client.get(path)
        assert r.status_code == 403 and '<h1>账号已停用</h1>' in r.text
        assert '此账号已停用，如有疑问请在微信中联系墨墨。' in r.text and '退出登录' in r.text
        assert '今日榜' not in r.text
    assert client.post('/logout').status_code == 303


# ---------- 邀请码 ----------

def test_changing_invite_code_invalidates_old(client, db):
    set_invite(client, 'OLDCODE1', target_seq=1)
    assert register(client, invite='OLDCODE1', username='first').status_code == 303
    set_invite(client, 'NEWCODE2', target_seq=2)
    other = make_client_like(client)
    r = register(other, invite='OLDCODE1', username='second')
    assert r.status_code == 400 and '邀请码不正确，请向墨墨确认。' in r.text
    assert register(other, invite='NEWCODE2', username='second').status_code == 303


def test_stale_target_seq_ignored_and_command_once(client, db):
    set_invite(client, 'NEWCODE2', target_seq=2)
    set_invite(client, 'OLDCODE1', target_seq=1)        # 迟到的旧指令
    assert register(client, invite='OLDCODE1').status_code == 400
    assert register(client, invite='NEWCODE2').status_code == 303
    cmds = rows(db, 'SELECT target_seq,applied FROM commands ORDER BY target_seq')
    assert cmds == [{'target_seq': 1, 'applied': 0}, {'target_seq': 2, 'applied': 1}]


def test_invite_ack_reports_applied_and_equal_seq_not_applied(client):
    def send(code, seq, env=None):
        env = env or {'schema_version': 1, 'event_id': str(uuid.uuid4()), 'kind': 'set_invite_code',
                      'created_at': '2026-11-03T08:00:00.000Z',
                      'payload': {'schema_version': 1, 'command_id': str(uuid.uuid4()), 'target_seq': seq,
                                  'code': code}}
        return env, client.post('/admin/events', json=env, headers=AUTH).json()
    env, ack = send('FIRST111', 3)
    assert ack['applied'] is True
    assert send('FIRST111', 3, env)[1]['applied'] is True          # 重放：同一结果
    assert send('SECOND22', 3)[1]['applied'] is False              # 序号相同：严格大于才生效
    assert send('THIRD333', 2)[1]['applied'] is False
    assert register(client, invite='FIRST111').status_code == 303


def test_invite_event_replay_and_redaction(client, db):
    env = set_invite(client, 'SECRET99', target_seq=5)
    again = client.post('/admin/events', json=env, headers=AUTH)
    assert again.status_code == 200
    assert len(rows(db, 'SELECT * FROM commands')) == 1
    changed = dict(env, payload=dict(env['payload'], code='OTHER999'))
    assert client.post('/admin/events', json=changed, headers=AUTH).status_code == 409
    stored = rows(db, 'SELECT payload,envelope FROM events')[0]
    assert 'SECRET99' not in stored['payload'] and 'SECRET99' not in stored['envelope']
    assert 'hmac-sha256:' in stored['payload']
    assert accounts._sha(CODE) not in stored['payload'] and accounts._sha('SECRET99') not in stored['payload']
    # 同一 event_id 只改大小写：内容不同 → 409，不当作重放
    lower = dict(env, payload=dict(env['payload'], code='secret99'))
    assert client.post('/admin/events', json=lower, headers=AUTH).status_code == 409


def test_same_command_id_in_new_event_runs_once(client, db):
    cid = str(uuid.uuid4())
    set_invite(client, 'FIRST111', target_seq=7, command_id=cid)
    set_invite(client, 'SECOND22', target_seq=8, command_id=cid)
    assert register(client, invite='FIRST111').status_code == 303


@pytest.mark.parametrize('payload', [
    {'code': 'abc'},                      # 太短
    {'code': 'has space'},
    {'code': 'MOMO2026\n'},
    {'target_seq': 0},
    {'target_seq': '1'},
    {'command_id': 'not-a-uuid'},
    {'command_id': str(uuid.uuid4()) + '\n'},
    {'target_seq': 1.0},
    {'target_seq': True},
    {'target_seq': 2 ** 63},
    {'target_seq': -1},
    {'extra': 1},
])
def test_bad_invite_payload_400(client, db, payload):
    base = {'schema_version': 1, 'command_id': str(uuid.uuid4()), 'target_seq': 1, 'code': CODE}
    env = {'schema_version': 1, 'event_id': str(uuid.uuid4()), 'kind': 'set_invite_code',
           'created_at': '2026-11-03T08:00:00.000Z', 'payload': dict(base, **payload)}
    r = client.post('/admin/events', json=env, headers=AUTH)
    assert r.status_code == 400
    assert rows(db, 'SELECT count(*) n FROM settings')[0]['n'] == 0


def test_invite_requires_admin_auth(client):
    env = {'schema_version': 1, 'event_id': str(uuid.uuid4()), 'kind': 'set_invite_code',
           'created_at': '2026-11-03T08:00:00.000Z',
           'payload': {'schema_version': 1, 'command_id': str(uuid.uuid4()), 'target_seq': 1, 'code': CODE}}
    assert client.post('/admin/events', json=env).status_code == 401


# ---------- 说明页、外壳与页面规则 ----------

def test_legal_page_public_with_version_and_draft(client):
    r = client.get('/legal')
    version = guest.load_terms(guest.DEFAULT_TERMS)[0]
    assert r.status_code == 200 and '<title>用户协议与隐私说明 · Curiobject</title>' in r.text
    assert version in r.text and '草稿' in r.text and '香港' in r.text and '只用于找回密码' in r.text
    assert 'id="terms"' in r.text and 'id="privacy"' in r.text


def test_logged_in_shell_links_legal_and_logout(client):
    set_invite(client)
    register(client)
    html = client.get('/').text
    assert html.count('href="/legal"') == 2 and html.count('action="/logout"') == 2
    assert '墨墨正在准备今天的藏品，请稍后再来。' in html


def test_pages_follow_design_rules(client):
    css = client.get('/static/guest.css').text
    assert 'box-shadow' not in css and 'border-radius:0' in css and '--tap:44px' in css
    small = [rule for rule in re.findall(r'([^{}]+)\{[^}]*font-size:(?:[0-9]|1[0-3])px', css)]
    # 小于 14px 的只有装饰性 eyebrow 与辅助信息（12px caption：更新时间、卡片货号、名次、品牌编号、图片占位字、
    # 单件页编号小节的编号与状态牌——DESIGN.md numbered-section index = eyebrow、status-plate = caption）。
    assert {r.strip() for r in small} <= {'.eb', '.upd', '.code', '.tc .idx', '.rk-n', '.ph-none', '.ph.broken::after',
                                          '.ns h2 .ix', '.plate'}
    html = client.get('/').text
    assert '<html lang="zh-CN">' in html
    assert 'content="width=device-width, initial-scale=1"' in html and 'user-scalable' not in html
    assert 'style=' not in html and '<script>' not in html          # CSP：无内联样式与脚本
    r = client.get('/')
    assert "script-src 'self'" in r.headers['content-security-policy']
    assert r.headers['cache-control'] == 'no-store' and r.headers['referrer-policy'] == 'same-origin'


def test_forms_post_without_js(client):
    html = client.get('/').text
    assert 'method="post" action="/register"' in html and 'method="post" action="/login"' in html
    assert re.search(r'<button class="btn btn-primary" type="submit"', html)


def test_cross_origin_post_rejected(client):
    r = client.post('/login', data={'username': 'a', 'password': 'b'}, headers={'Origin': 'https://evil.example'})
    assert r.status_code == 403


def test_origin_null_falls_back_to_fetch_metadata(client):
    set_invite(client)
    assert register(client, next='/').status_code == 303
    bare = TestClient(client.app, follow_redirects=False)      # 不带默认 Origin
    form = {'username': 'wangjie', 'password': PASSWORD}
    assert bare.post('/login', data=form, headers={'Origin': 'null', 'Sec-Fetch-Site': 'same-origin'}).status_code == 303
    assert bare.post('/login', data=form, headers={'Origin': 'null', 'Sec-Fetch-Site': 'cross-site'}).status_code == 403
    assert bare.post('/login', data=form, headers={'Sec-Fetch-Site': 'cross-site'}).status_code == 403
    assert bare.post('/login', data=form, headers={'Sec-Fetch-Site': 'same-origin'}).status_code == 303
    assert bare.post('/login', data=form).status_code == 403                 # 两个头都没有：拒绝
    assert bare.post('/login', data=form, headers={'Origin': 'null'}).status_code == 403


def test_non_form_post_rejected(client):
    assert client.post('/login', json={'username': 'a'}).status_code == 400


def test_admin_and_docs_paths_not_swallowed(client):
    assert client.get('/admin/events').status_code in (404, 405)
    for path in ('/docs', '/redoc', '/openapi.json'):
        assert client.get(path).status_code == 404


def test_terms_parser(tmp_path):
    p = tmp_path / 't.md'
    p.write_text('---\nversion: v9\n---\n# 标题\n\n## 甲 {#a}\n\n- 一\n- **二**\n\n> 注意 <b>\n\n段落\n')
    version, html = guest.load_terms(p)
    assert version == 'v9' and '<h2 id="a">甲</h2>' in html and '<strong>二</strong>' in html
    assert '&lt;b&gt;' in html and '<h1>' not in html
    p.write_text('# 无版本\n')
    with pytest.raises(ValueError):
        guest.load_terms(p)


# ---------- 评审后补充 ----------

def test_logout_revokes_all_sessions_of_account(client):
    set_invite(client)
    register(client)
    other = make_client_like(client)
    assert login(other).status_code == 303 and logged_in_home(other.get('/'))
    client.post('/logout')
    assert '登录已过期，请重新登录。' in other.get('/').text


def test_forgot_without_public_url_and_secure_cookie_sends_nothing(make, inbox, caplog):
    c = make(public_url=None, cookie_secure=True)
    set_invite(c)
    register(make_client_like(c), email='lily@example.com')
    with caplog.at_level('WARNING'):
        r = c.post('/forgot', data={'username': 'wangjie'}, headers={'Host': 'evil.example', 'Origin': 'http://evil.example'})
    assert r.status_code == 503 and '暂时无法发送重设邮件，请在微信中联系墨墨重置密码。' in r.text
    assert inbox.sent == [] and 'CURIO_PUBLIC_URL' in caplog.text


def test_forgot_host_fallback_only_for_local_http(make, inbox):
    c = make(public_url=None, cookie_secure=False)
    set_invite(c)
    register(make_client_like(c), email='lily@example.com')
    c.post('/forgot', data={'username': 'wangjie'})
    assert 'http://testserver/reset?token=' in inbox.sent[-1][2]


def test_public_url_from_env_with_trailing_slash(make, inbox, monkeypatch):
    monkeypatch.setenv('CURIO_PUBLIC_URL', 'https://curio.example/')
    c = make(public_url=None, cookie_secure=True)
    set_invite(c)
    register(make_client_like(c), email='lily@example.com')
    c.post('/forgot', data={'username': 'wangjie'})
    assert 'https://curio.example/reset?token=' in inbox.sent[-1][2] and '//reset' not in inbox.sent[-1][2]


def test_rate_limit_reserve_is_atomic_under_concurrency(client, db, clock):
    import threading
    results = []
    barrier = threading.Barrier(10)

    def worker():
        c = server.connect(db)
        c.row_factory = sqlite3.Row
        try:
            barrier.wait()
            results.append(accounts.reserve(c, 'login', clock.now, ip=None, user_key='wangjie')[0])
        finally:
            c.close()
    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(w == 0 for w in results).count(True) == 5        # 只有 5 次被放行，第 6 次起拒绝


def test_register_rate_limited_by_ip(client):
    set_invite(client)
    for i in range(20):
        assert register(client, invite='WRONG1', username=f'user{i}').status_code == 400
    assert register(client, username='fresh_one').status_code == 429


def test_forgot_rate_limited_by_ip(client):
    for i in range(20):
        assert client.post('/forgot', data={'username': f'user{i}'}).status_code == 200
    assert client.post('/forgot', data={'username': 'another'}).status_code == 429


def test_session_cleanup_keeps_row_for_prefill(client, clock):
    set_invite(client)
    register(client)
    other = make_client_like(client)
    assert register(other, username='second').status_code == 303
    other.post('/logout')
    clock.now += 91 * 86400
    assert login(other, 'second').status_code == 303          # 建新会话时会清理旧行
    r = client.get('/')
    assert '登录已过期，请重新登录。' in r.text and re.search(r'id="l-user"[^>]*value="wangjie"', r.text)


def test_form_body_too_large(client):
    big = 'username=' + 'a' * (guest.MAX_FORM + 10)
    r = client.post('/login', content=big, headers={'Content-Type': 'application/x-www-form-urlencoded'})
    assert r.status_code == 400


def test_forgot_for_disabled_account(client, db, clock, inbox):
    set_invite(client)
    register(make_client_like(client), email='lily@example.com')
    client.post('/forgot', data={'username': 'wangjie'})
    path = reset_link(inbox).replace('https://curio.test', '')
    [acct] = rows(db, 'SELECT account_id FROM accounts')
    c = server.connect(db)
    c.row_factory = sqlite3.Row
    try:
        accounts.set_status(c, acct['account_id'], 'disabled', clock.now)
    finally:
        c.close()
    r = client.post('/forgot', data={'username': 'wangjie'})
    assert '此账号已停用，请在微信中联系墨墨。' in r.text and len(inbox.sent) == 1
    assert client.get(path).status_code == 410
    token = parse_qs(urlsplit(path).query)['token'][0]
    assert client.post('/reset', data={'token': token, 'password': 'new-password-1'}).status_code == 410


def test_reconsent_after_terms_version_change(make, db, tmp_path):
    c1 = make()
    set_invite(c1)
    register(c1)
    newer = tmp_path / 'terms.md'
    newer.write_text(guest.DEFAULT_TERMS.read_text(encoding='utf-8').replace('version: ', 'version: newer-', 1),
                     encoding='utf-8')
    c2 = make(terms_path=newer)
    c2.cookies = c1.cookies
    r = c2.get('/item/CO-0001')
    assert r.status_code == 200 and '用户协议已更新' in r.text and '今日榜' not in r.text
    assert 'action="/consent"' in r.text and 'name="next" value="/item/CO-0001"' in r.text
    assert c2.get('/legal').status_code == 200                       # 说明页仍可读
    bad = c2.post('/consent', data={'next': '/item/CO-0001'})
    assert bad.status_code == 400 and '请先勾选同意用户协议与隐私政策' in bad.text
    ok = c2.post('/consent', data={'next': '/item/CO-0001', 'consent': 'yes'})
    assert ok.status_code == 303 and ok.headers['location'] == '/item/CO-0001'
    assert logged_in_item(c2.get('/item/CO-0001'))
    versions = rows(db, 'SELECT kind,terms_version FROM consents ORDER BY terms_version,kind')
    assert len(versions) == 4 and {v['terms_version'] for v in versions[2:]} == {'newer-' + guest.load_terms(
        guest.DEFAULT_TERMS)[0]}


def test_server_secret_file_and_env(tmp_path, monkeypatch):
    monkeypatch.delenv('CURIO_SERVER_SECRET', raising=False)
    key = accounts.load_secret(str(tmp_path))
    path = tmp_path / 'server_secret'
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and accounts.load_secret(str(tmp_path)) == key
    monkeypatch.setenv('CURIO_SERVER_SECRET', 'x' * 32)
    assert accounts.load_secret(str(tmp_path)) == b'x' * 32
    monkeypatch.setenv('CURIO_SERVER_SECRET', 'short')
    with pytest.raises(ValueError):
        accounts.load_secret(str(tmp_path))


def test_terms_bom_and_crlf(tmp_path):
    p = tmp_path / 't.md'
    p.write_bytes('\ufeff---\r\nversion: v2\r\n---\r\n## 甲\r\n\r\n段落\r\n'.encode('utf-8'))
    version, html = guest.load_terms(p)
    assert version == 'v2' and '<h2>甲</h2>' in html and '\r' not in html
