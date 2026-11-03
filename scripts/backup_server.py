"""每日备份服务器库：在服务器上 `VACUUM INTO` 出一份一致副本，拉回 Mac `data/server-backups/`，保留 30 天（AD-19）。

副本写在服务器 `mktemp -d` 建的私有目录（0700）里，拉回后删除整个目录。

设置只读仓库根 .env：
  CURIO_SSH_HOST        必填，如 deploy@203.0.113.10
  CURIO_REMOTE_DIR      选填，默认 /opt/curiobject
  CURIO_REMOTE_DB       选填，默认 /var/lib/curiobject/server.db
  CURIO_REMOTE_PYTHON   选填，默认 <CURIO_REMOTE_DIR>/server/.venv/bin/python

ssh/rsync 一律 BatchMode、连接 15 秒超时，每步另有总时限。拉回后本机做 `PRAGMA integrity_check`，
不通过则删除并非零退出。任一步失败都非零退出。
用法：uv run python scripts/backup_server.py
**连接真实 VPS 须墨墨授权后执行。**
"""
import argparse, os, re, shlex, sqlite3, subprocess, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from curio.config import ROOT, _env

KEEP_DAYS = 30
NAME_RE = re.compile(r'^server-(\d{8}T\d{6}Z)\.db$')
STAMP = '%Y%m%dT%H%M%SZ'
SSH_OPTS = ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15']
RSYNC_SSH = 'ssh ' + ' '.join(SSH_OPTS)
STEP_TIMEOUT = 600
_HOST_RE = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9_.@-]*$')
_TMPDIR_RE = re.compile(r'^/[A-Za-z0-9._/-]+$')
# 在服务器上运行（服务器 venv 的 python）：库不存在时不新建空库。
REMOTE_SNIPPET = (
    'import os,sqlite3,sys\n'
    'src,dst=sys.argv[1],sys.argv[2]\n'
    'if not os.path.exists(src):sys.exit("服务器库不存在："+src)\n'
    'c=sqlite3.connect(src,timeout=30)\n'
    'c.execute("PRAGMA busy_timeout=30000")\n'
    'c.execute("VACUUM INTO ?",(dst,))\n'
    'c.close()\n'
)


class BackupError(Exception):
    pass


def settings(root=ROOT):
    values = _env(root, ('CURIO_SSH_HOST', 'CURIO_REMOTE_DIR', 'CURIO_REMOTE_DB', 'CURIO_REMOTE_PYTHON')) or {}
    host = values.get('CURIO_SSH_HOST', '')
    if not host:
        raise BackupError('.env 缺少 CURIO_SSH_HOST')
    if not _HOST_RE.match(host):
        raise BackupError('CURIO_SSH_HOST 格式无效')
    remote = (values.get('CURIO_REMOTE_DIR') or '/opt/curiobject').rstrip('/')
    return {'host': host,
            'db': values.get('CURIO_REMOTE_DB') or '/var/lib/curiobject/server.db',
            'python': values.get('CURIO_REMOTE_PYTHON') or f'{remote}/server/.venv/bin/python'}


def ssh(host, command):
    return ['ssh', *SSH_OPTS, host, command]


def snapshot_command(s):
    """服务器上：mktemp -d（0700）→ VACUUM INTO 到其中 → 打印目录；失败时删掉目录。"""
    q = shlex.quote
    return ('d=$(mktemp -d) || exit 1; '
            f'if {q(s["python"])} -c {q(REMOTE_SNIPPET)} {q(s["db"])} "$d/server.db"; '
            'then printf "%s\\n" "$d"; else rc=$?; rm -rf -- "$d"; exit $rc; fi')


def check_db(path):
    """能打开且 integrity_check 为 ok。"""
    c = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    try:
        c.execute('SELECT count(*) FROM sqlite_master').fetchone()
        return c.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    except sqlite3.DatabaseError:
        return False
    finally:
        c.close()


def prune(dest, now, keep_days=KEEP_DAYS):
    """删除超过 keep_days 天的备份（按文件名里的 UTC 时间）。返回删除的文件名。"""
    removed = []
    for p in sorted(Path(dest).glob('server-*.db')):
        m = NAME_RE.match(p.name)
        if not m:
            continue
        taken = datetime.strptime(m.group(1), STAMP).replace(tzinfo=timezone.utc)
        if now - taken > timedelta(days=keep_days):
            p.unlink()
            removed.append(p.name)
    return removed


def backup(root=ROOT, runner=subprocess.run, now=None, out=print):
    s = settings(root)
    now = now or datetime.now(timezone.utc)
    stamp = now.strftime(STAMP)
    dest = Path(root) / 'data' / 'server-backups'
    dest.mkdir(parents=True, exist_ok=True)
    final = dest / f'server-{stamp}.db'
    part = dest / f'server-{stamp}.db.part'
    r = runner(ssh(s['host'], snapshot_command(s)), timeout=STEP_TIMEOUT, capture_output=True, text=True)
    if r.returncode != 0:
        raise BackupError(f'服务器上 VACUUM INTO 失败（退出码 {r.returncode}）')
    lines = (r.stdout or '').strip().splitlines()
    tmpdir = lines[-1] if lines else ''
    if not _TMPDIR_RE.match(tmpdir) or '..' in tmpdir.split('/'):
        raise BackupError('服务器未返回有效的临时目录')
    try:
        r = runner(['rsync', '-a', '-e', RSYNC_SSH, f'{s["host"]}:{tmpdir}/server.db', str(part)],
                   timeout=STEP_TIMEOUT)
        if r.returncode != 0 or not part.exists():
            raise BackupError(f'拉回备份失败（退出码 {r.returncode}）')
        if not check_db(part):
            raise BackupError('拉回的备份无法打开或校验不通过')
        os.replace(part, final)
    finally:
        part.unlink(missing_ok=True)
        try:  # 删掉服务器上的临时目录；失败不影响结果
            runner(ssh(s['host'], f'rm -rf -- {shlex.quote(tmpdir)}'), timeout=60)
        except (OSError, subprocess.SubprocessError):
            pass
    removed = prune(dest, now)
    out(f'备份完成：{final}')
    if removed:
        out('已删除超过 %d 天的旧备份：%s' % (KEEP_DAYS, '、'.join(removed)))
    return final


def main(argv=None, root=ROOT, runner=subprocess.run):
    argparse.ArgumentParser(description='备份服务器库到 data/server-backups/（须墨墨授权后才对真实 VPS 执行）').parse_args(argv)
    try:
        backup(root, runner)
    except subprocess.TimeoutExpired as e:
        print(f'备份失败：命令超时（{e.timeout} 秒）', file=sys.stderr)
        return 1
    except (BackupError, OSError) as e:
        print('备份失败：' + str(e), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
