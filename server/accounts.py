"""客人账号核心（AD-2、AD-8、AD-17、AD-18、AD-24）：表、校验、会话、限频、重设令牌、同意记录、邀请码。

- 密码与邀请码只存 Argon2id 哈希（OWASP 参数：m=19 MiB、t=2、p=1）；会话令牌与重设令牌只存 SHA-256。
- 明文密码、令牌、邀请码不进日志、不进数据库；限频计数的键（IP、用户名）与 events 表里的邀请码副本
  用服务器密钥做 HMAC-SHA256（密钥见 load_secret：CURIO_SERVER_SECRET，或数据目录里 0600 的 server_secret 文件）。
- 所有函数接收调用方的 sqlite3 连接（autocommit 模式），写操作自己开 `BEGIN IMMEDIATE`。
- 时间一律由调用方传入 `now`（Unix 秒），便于测试；写入库的时间文字为 UTC ISO 8601（带 Z）。
"""
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import unicodedata
import uuid
from datetime import datetime, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

SESSION_SECONDS = 90 * 86400        # 约 90 天
RESET_SECONDS = 3600                 # 重设链接 1 小时有效
RATE_WINDOW = 15 * 60                # 限频窗口 15 分钟
# 每个动作：按 IP 与按用户名各自的上限（窗口内次数）。
#   login：只数失败；第 6 次失败起（同一用户名）拒绝。
#   register：IP 数每次提交；用户名数失败（邀请码错、用户名已被使用等）。
#   forgot：IP 与用户名都数每次提交（防止对同一账号反复发信）。
RATE_LIMITS = {
    'register': {'ip': 20, 'user': 5},
    'login': {'ip': 20, 'user': 5},
    'forgot': {'ip': 20, 'user': 5},
}
PASSWORD_MIN, PASSWORD_MAX = 8, 128
NICKNAME_MAX = 30
EMAIL_MAX = 254
INVITE_TARGET = 'invite'

HASHER = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1, hash_len=32, salt_len=16)
# 用户名不存在时也做一次同样耗时的校验，避免靠响应时间判断账号是否存在。
_DUMMY_HASH = HASHER.hash(secrets.token_urlsafe(16))

_USERNAME_RE = re.compile(r'[A-Za-z0-9_㐀-䶿一-鿿]{2,20}')
_EMAIL_RE = re.compile(r'[^@\s]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?'
                       r'(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+')
_INVITE_RE = re.compile(r'[A-Za-z0-9_-]{4,32}')
_MAX_SEQ = 2 ** 63 - 1
_SECRET = [secrets.token_bytes(32)]   # create_app 启动时用 load_secret() 替换
_UUID_RE = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')

# 页面措辞（EXPERIENCE.md · Voice and Tone / State Patterns）。
MSG = {
    'invite_wrong': '邀请码不正确，请向墨墨确认。',
    'invite_closed': '注册暂未开放，请在微信中联系墨墨。',
    'username_bad': '用户名为 2–20 个字符，只能用中文、字母、数字或下划线。',
    'username_taken': '此用户名已被使用，请换一个。',
    'password_short': '密码至少 8 位。',
    'password_long': '密码最多 128 位。',
    'nickname_bad': '请填写微信名/称呼（1–30 字）。',
    'email_bad': '邮箱格式不正确。',
    'consent': '请先勾选同意用户协议与隐私政策',
    'login_wrong': '用户名或密码不正确。',
    'rate': '尝试次数过多，请稍后再试。',
    'reset_sent': '重设链接已发送，请到邮箱查看。',
    'reset_no_email': '此账号未填写邮箱，请在微信中联系墨墨重置密码。',
    'reset_invalid': '此链接已失效，请重新找回密码。',
    'reset_done': '新密码已保存，请重新登录。',
    'expired': '登录已过期，请重新登录。',
    'disabled': '此账号已停用，如有疑问请在微信中联系墨墨。',
    'reset_disabled': '此账号已停用，请在微信中联系墨墨。',
    'reset_unavailable': '暂时无法发送重设邮件，请在微信中联系墨墨重置密码。',
}


class UsernameTaken(Exception):
    pass


class BadCommand(ValueError):
    """指令载荷在 Schema 之外的严格检查不过（中文）。"""


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def load_secret(data_dir):
    """HMAC 密钥：环境变量 CURIO_SERVER_SECRET；否则在数据目录生成一次 server_secret（0600）并沿用。"""
    env = os.environ.get('CURIO_SERVER_SECRET')
    if env:
        key = env.encode('utf-8')
    else:
        path = os.path.join(data_dir, 'server_secret')
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            with open(path, 'rb') as f:
                key = f.read().strip()
        else:
            key = secrets.token_hex(32).encode()
            with os.fdopen(fd, 'wb') as f:
                f.write(key)
    if len(key) < 16:
        raise ValueError('服务器密钥太短（至少 16 字节）')
    _SECRET[0] = key
    return key


def _mac(text):
    return hmac.new(_SECRET[0], text.encode('utf-8'), hashlib.sha256).hexdigest()


def _sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def ensure(c):
    c.executescript('''
    CREATE TABLE IF NOT EXISTS accounts(
        account_id TEXT PRIMARY KEY,
        username TEXT NOT NULL,
        username_key TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        nickname TEXT NOT NULL,
        email TEXT,
        status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','disabled')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        account_rev INTEGER NOT NULL DEFAULT 1);
    -- AD-18：同意记录（版本 + UTC 时间），只追加不改；kind：terms_privacy / cross_border_storage。
    -- 注册时写首次记录；条款版本更新后客人重新同意再追加一行。
    CREATE TABLE IF NOT EXISTS consents(
        account_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        terms_version TEXT NOT NULL,
        agreed_at TEXT NOT NULL,
        PRIMARY KEY(account_id, kind, terms_version));
    -- 会话只存令牌哈希；失效的行保留 revoked_at，用于"登录已过期"时预填用户名，过期 90 天后清理。
    CREATE TABLE IF NOT EXISTS sessions(
        token_hash TEXT PRIMARY KEY,
        account_id TEXT NOT NULL,
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        revoked_at REAL);
    CREATE INDEX IF NOT EXISTS sessions_account ON sessions(account_id);
    CREATE TABLE IF NOT EXISTS reset_tokens(
        token_hash TEXT PRIMARY KEY,
        account_id TEXT NOT NULL,
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        used_at REAL);
    CREATE TABLE IF NOT EXISTS attempts(bucket TEXT NOT NULL, at REAL NOT NULL);
    CREATE INDEX IF NOT EXISTS attempts_bucket ON attempts(bucket, at);
    CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT NOT NULL);
    -- AD-24：已执行的 Mac 指令（同一 command_id 只执行一次）。
    CREATE TABLE IF NOT EXISTS commands(
        command_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        target TEXT NOT NULL,
        target_seq INTEGER NOT NULL,
        applied INTEGER NOT NULL,
        at TEXT NOT NULL);
    ''')


def _tx(c):
    """BEGIN IMMEDIATE … COMMIT；异常回滚。"""
    class _T:
        def __enter__(self):
            c.execute('BEGIN IMMEDIATE')
            return c

        def __exit__(self, et, ev, tb):
            c.execute('ROLLBACK' if et else 'COMMIT')
            return False
    return _T()


# ---------- 规范化与校验 ----------

def username_key(raw):
    """返回 (显示用用户名, 唯一键)。全角转半角、去首尾空白；唯一键不区分大小写。"""
    name = unicodedata.normalize('NFKC', raw or '').strip()
    return name, name.casefold()


def normalize_invite(raw):
    return unicodedata.normalize('NFKC', raw or '').strip().upper()


def validate_registration(form):
    """返回 (clean, errors)。errors：字段名 → 中文提示（不含邀请码正误与用户名是否已被使用）。"""
    errors = {}
    invite = normalize_invite(form.get('invite'))
    username, key = username_key(form.get('username'))
    password = form.get('password') or ''
    nickname = unicodedata.normalize('NFKC', form.get('nickname') or '').strip()
    email = (form.get('email') or '').strip()
    if not invite:
        errors['invite'] = MSG['invite_wrong']
    if not _USERNAME_RE.fullmatch(username):
        errors['username'] = MSG['username_bad']
    if len(password) < PASSWORD_MIN:
        errors['password'] = MSG['password_short']
    elif len(password) > PASSWORD_MAX:
        errors['password'] = MSG['password_long']
    if not 1 <= len(nickname) <= NICKNAME_MAX or any(unicodedata.category(ch)[0] == 'C' for ch in nickname):
        errors['nickname'] = MSG['nickname_bad']
    if email and (len(email) > EMAIL_MAX or not _EMAIL_RE.fullmatch(email)):
        errors['email'] = MSG['email_bad']
    if form.get('consent') != 'yes':
        errors['consent'] = MSG['consent']
    clean = {'invite': invite, 'username': username, 'username_key': key, 'password': password,
             'nickname': nickname, 'email': email or None}
    return clean, errors


def validate_password(password):
    if len(password) < PASSWORD_MIN:
        return MSG['password_short']
    if len(password) > PASSWORD_MAX:
        return MSG['password_long']
    return None


# ---------- 邀请码（AD-2：只由 Mac 指令写；服务器只存哈希） ----------

def invite_open(c):
    return c.execute("SELECT 1 FROM settings WHERE k='invite_hash'").fetchone() is not None


def check_invite(c, code):
    """返回 'ok' / 'wrong' / 'closed'。"""
    row = c.execute("SELECT v FROM settings WHERE k='invite_hash'").fetchone()
    if row is None:
        return 'closed'
    if not code:
        return 'wrong'
    try:
        HASHER.verify(row[0], code)
        return 'ok'
    except (VerificationError, InvalidHashError):
        return 'wrong'


def check_invite_command(payload):
    """Schema 之外的严格检查（Schema 的 `$` 会放过结尾换行）。"""
    if not _UUID_RE.fullmatch(payload['command_id']):
        raise BadCommand('set_invite_code 载荷不合契约：command_id 不符合要求')
    if not _INVITE_RE.fullmatch(payload['code']):
        raise BadCommand('set_invite_code 载荷不合契约：code 不符合要求')
    seq = payload['target_seq']
    if type(seq) is not int or not 1 <= seq <= _MAX_SEQ:
        raise BadCommand('set_invite_code 载荷不合契约：target_seq 必须为 1 到 2^63-1 的整数')


def redact(kind, payload):
    """存入 events 表前去掉明文：邀请码换成原文的 HMAC-SHA256（只用于重放比对；大小写不同即不同内容）。"""
    if kind == 'set_invite_code':
        payload = dict(payload, code='hmac-sha256:' + _mac(payload['code']))
    return payload


def apply_set_invite_code(c, payload, now):
    """在调用方事务内执行（AD-24）。同一 command_id 只执行一次，重放返回第一次的结果；
    target_seq 不大于已应用的最高值时不生效。返回是否生效。"""
    done = c.execute('SELECT applied FROM commands WHERE command_id=?', (payload['command_id'],)).fetchone()
    if done:
        return bool(done[0])
    row = c.execute("SELECT v FROM settings WHERE k='invite_target_seq'").fetchone()
    applied = row is None or payload['target_seq'] > int(row[0])
    if applied:
        code_hash = HASHER.hash(normalize_invite(payload['code']))
        c.execute("INSERT OR REPLACE INTO settings VALUES('invite_hash',?)", (code_hash,))
        c.execute("INSERT OR REPLACE INTO settings VALUES('invite_target_seq',?)", (str(payload['target_seq']),))
        c.execute("INSERT OR REPLACE INTO settings VALUES('invite_set_at',?)", (iso(now),))
    c.execute('INSERT INTO commands(command_id,kind,target,target_seq,applied,at) VALUES(?,?,?,?,?,?)',
              (payload['command_id'], 'set_invite_code', INVITE_TARGET, payload['target_seq'], int(applied), iso(now)))
    return applied


# ---------- 账号 ----------

def get_account(c, account_id):
    return c.execute('SELECT * FROM accounts WHERE account_id=?', (account_id,)).fetchone()


def username_taken(c, key):
    return c.execute('SELECT 1 FROM accounts WHERE username_key=?', (key,)).fetchone() is not None


def create_account(c, clean, terms_version, now):
    """建号 + 同意记录（含境外存储单独同意），同一事务。用户名已被使用抛 UsernameTaken。返回 account_id。"""
    password_hash = HASHER.hash(clean['password'])
    account_id = uuid.uuid4().hex
    at = iso(now)
    try:
        with _tx(c):
            c.execute('INSERT INTO accounts(account_id,username,username_key,password_hash,nickname,email,status,'
                      'created_at,updated_at,account_rev) VALUES(?,?,?,?,?,?,?,?,?,1)',
                      (account_id, clean['username'], clean['username_key'], password_hash, clean['nickname'],
                       clean['email'], 'active', at, at))
            for kind in ('terms_privacy', 'cross_border_storage'):
                c.execute('INSERT INTO consents(account_id,kind,terms_version,agreed_at) VALUES(?,?,?,?)',
                          (account_id, kind, terms_version, at))
    except sqlite3.IntegrityError:
        raise UsernameTaken() from None
    return account_id


def authenticate(c, username, password):
    """用户名 + 密码正确返回账号行，否则 None。用户名不存在时也做一次哈希校验。"""
    _, key = username_key(username)
    row = c.execute('SELECT * FROM accounts WHERE username_key=?', (key,)).fetchone() if key else None
    if row is None or not password or len(password) > PASSWORD_MAX:
        try:
            HASHER.verify(_DUMMY_HASH, password[:PASSWORD_MAX] or 'x')
        except (VerificationError, InvalidHashError):
            pass
        return None
    try:
        HASHER.verify(row['password_hash'], password)
    except (VerificationError, InvalidHashError):
        return None
    if HASHER.check_needs_rehash(row['password_hash']):
        c.execute('UPDATE accounts SET password_hash=? WHERE account_id=?', (HASHER.hash(password), row['account_id']))
    return row


def set_password(c, account_id, password, now):
    """改密码 / 重设 / 重置共用：写新哈希、递增 account_rev、使该账号全部会话与重设令牌失效。"""
    password_hash = HASHER.hash(password)
    with _tx(c):
        c.execute('UPDATE accounts SET password_hash=?,updated_at=?,account_rev=account_rev+1 WHERE account_id=?',
                  (password_hash, iso(now), account_id))
        _revoke_all(c, account_id, now)


def set_status(c, account_id, status, now):
    """账号状态只由 Mac 指令改（AD-24，故事 14 接入）；停用使全部会话失效。"""
    if status not in ('active', 'disabled'):
        raise ValueError('未知状态')
    with _tx(c):
        c.execute('UPDATE accounts SET status=?,updated_at=?,account_rev=account_rev+1 WHERE account_id=?',
                  (status, iso(now), account_id))
        if status == 'disabled':
            _revoke_all(c, account_id, now)


def _revoke_all(c, account_id, now):
    c.execute('UPDATE sessions SET revoked_at=? WHERE account_id=? AND revoked_at IS NULL', (now, account_id))
    c.execute('UPDATE reset_tokens SET used_at=? WHERE account_id=? AND used_at IS NULL', (now, account_id))


# ---------- 会话 ----------

def create_session(c, account_id, now):
    token = secrets.token_urlsafe(32)
    with _tx(c):
        # 清理过期很久的旧行。
        c.execute('DELETE FROM sessions WHERE expires_at < ?', (now - SESSION_SECONDS,))
        c.execute('INSERT INTO sessions(token_hash,account_id,created_at,expires_at) VALUES(?,?,?,?)',
                  (_sha(token), account_id, now, now + SESSION_SECONDS))
    return token


def lookup_session(c, token, now):
    """返回 (state, account)。state：'none'（无 Cookie）/ 'ok' / 'expired'（account 可能为 None）。"""
    if not token:
        return 'none', None
    if len(token) > 128:
        return 'expired', None
    row = c.execute('SELECT * FROM sessions WHERE token_hash=?', (_sha(token),)).fetchone()
    if row is None:
        return 'expired', None
    account = get_account(c, row['account_id'])
    if account is None:
        return 'expired', None
    if row['revoked_at'] is not None or row['expires_at'] <= now:
        return 'expired', account
    return 'ok', account


def revoke_session(c, token, now):
    """退出：使该 Cookie 所属账号的全部会话失效（spec：退出使该账号全部会话失效）。"""
    if not token or len(token) > 128:
        return
    row = c.execute('SELECT account_id FROM sessions WHERE token_hash=?', (_sha(token),)).fetchone()
    if row:
        c.execute('UPDATE sessions SET revoked_at=? WHERE account_id=? AND revoked_at IS NULL', (now, row[0]))


def needs_consent(c, account_id, version):
    """该账号对当前条款版本是否还缺同意（两类都要有）。"""
    n = c.execute('SELECT count(DISTINCT kind) FROM consents WHERE account_id=? AND terms_version=? '
                  "AND kind IN ('terms_privacy','cross_border_storage')", (account_id, version)).fetchone()[0]
    return n < 2


def record_consent(c, account_id, version, now):
    with _tx(c):
        for kind in ('terms_privacy', 'cross_border_storage'):
            c.execute('INSERT OR IGNORE INTO consents(account_id,kind,terms_version,agreed_at) VALUES(?,?,?,?)',
                      (account_id, kind, version, iso(now)))


# ---------- 限频（按 IP 与用户名，窗口 15 分钟） ----------

def _bucket(action, dim, value):
    return _mac(f'{action}\0{dim}\0{value}')


def retry_after(c, action, ip, user_key, now):
    """已超限时返回需等待的秒数（≥1），否则 0。"""
    wait = 0
    for dim, value in (('ip', ip), ('user', user_key)):
        if not value:
            continue
        limit = RATE_LIMITS[action][dim]
        rows = c.execute('SELECT at FROM attempts WHERE bucket=? AND at>? ORDER BY at DESC LIMIT ?',
                         (_bucket(action, dim, value), now - RATE_WINDOW, limit)).fetchall()
        if len(rows) >= limit:
            wait = max(wait, int(rows[-1][0] + RATE_WINDOW - now) + 1)
    return wait


def reserve(c, action, now, ip=None, user_key=None):
    """检查并记下一次尝试，同一个 BEGIN IMMEDIATE 内完成（并发请求无法一起越过上限）。
    返回 (wait, ids)：wait > 0 表示已超限、什么也没记；否则 ids 为本次记下的行（dim → rowid），
    成功的尝试若不该计数，用 release() 删掉对应行。"""
    with _tx(c):
        c.execute('DELETE FROM attempts WHERE at <= ?', (now - RATE_WINDOW,))
        wait = retry_after(c, action, ip, user_key, now)
        if wait:
            return wait, {}
        ids = {}
        for dim, value in (('ip', ip), ('user', user_key)):
            if value:
                ids[dim] = c.execute('INSERT INTO attempts(bucket,at) VALUES(?,?)',
                                     (_bucket(action, dim, value), now)).lastrowid
        return 0, ids


def release(c, ids, *dims):
    for dim in dims:
        if dim in ids:
            c.execute('DELETE FROM attempts WHERE rowid=?', (ids[dim],))


def hit(c, action, now, ip=None, user_key=None):
    with _tx(c):
        c.execute('DELETE FROM attempts WHERE at <= ?', (now - RATE_WINDOW,))
        for dim, value in (('ip', ip), ('user', user_key)):
            if value:
                c.execute('INSERT INTO attempts(bucket,at) VALUES(?,?)', (_bucket(action, dim, value), now))


def clear_user(c, action, user_key):
    c.execute('DELETE FROM attempts WHERE bucket=?', (_bucket(action, 'user', user_key),))


# ---------- 找回与重设 ----------

def start_reset(c, username, now):
    """返回 (result, account, token)。result：'sent'（有邮箱，已生成令牌）/ 'no_email' / 'disabled' / 'unknown'。"""
    _, key = username_key(username)
    row = c.execute('SELECT * FROM accounts WHERE username_key=?', (key,)).fetchone() if key else None
    if row is None:
        return 'unknown', None, None
    if row['status'] != 'active':
        return 'disabled', row, None
    if not row['email']:
        return 'no_email', row, None
    token = secrets.token_urlsafe(32)
    with _tx(c):
        c.execute('DELETE FROM reset_tokens WHERE expires_at < ?', (now - 86400,))
        c.execute('INSERT INTO reset_tokens(token_hash,account_id,created_at,expires_at) VALUES(?,?,?,?)',
                  (_sha(token), row['account_id'], now, now + RESET_SECONDS))
    return 'sent', row, token


def check_reset(c, token, now):
    """令牌有效返回 account_id，否则 None。"""
    if not token or len(token) > 128:
        return None
    row = c.execute('SELECT r.account_id,r.expires_at,r.used_at,a.status FROM reset_tokens r '
                    'JOIN accounts a ON a.account_id=r.account_id WHERE r.token_hash=?', (_sha(token),)).fetchone()
    if row is None or row['used_at'] is not None or row['expires_at'] <= now or row['status'] != 'active':
        return None
    return row['account_id']


def finish_reset(c, token, password, now):
    """用重设令牌设新密码；令牌一次性（并发重放只有一个成功）。成功返回 True。"""
    if check_reset(c, token, now) is None:
        return False
    password_hash = HASHER.hash(password)
    with _tx(c):
        row = c.execute('SELECT r.account_id FROM reset_tokens r JOIN accounts a ON a.account_id=r.account_id '
                        "WHERE r.token_hash=? AND r.used_at IS NULL AND r.expires_at>? AND a.status='active'",
                        (_sha(token), now)).fetchone()
        if row is None:
            return False
        c.execute('UPDATE accounts SET password_hash=?,updated_at=?,account_rev=account_rev+1 WHERE account_id=?',
                  (password_hash, iso(now), row['account_id']))
        _revoke_all(c, row['account_id'], now)   # 该账号全部会话与重设令牌（含本令牌）作废
    return True
