"""经 SSH 把 server/ 与 contract/ 部署到香港服务器（AD-19、AD-21）。

步骤：rsync 同步 server/（不含 .venv、data）与 contract/ → 服务器上 `uv sync --frozen --no-dev`
（Python 装到 <远端目录>/python，不在 /home 下）→ `sudo systemctl restart curiobject`
→ 服务器本机 /healthz 版本核对 → 填了 CURIO_SERVER_URL 时再经 HTTPS 核对一次版本。

设置只读仓库根 .env（不读系统环境变量）：
  CURIO_SSH_HOST      必填，如 deploy@203.0.113.10（墨墨买好 VPS 后填写）
  CURIO_REMOTE_DIR    选填，默认 /opt/curiobject
  CURIO_REMOTE_UV     选填，服务器上 uv 的路径，默认 uv
  CURIO_SERVER_URL    选填，填了就经 HTTPS 检查 /healthz

ssh/rsync 一律 BatchMode（不弹密码提示）、连接 15 秒超时，每步另有总时限。
用法：uv run python scripts/deploy_server.py [--dry-run]
**连接真实 VPS 须墨墨授权后执行**；--dry-run 只打印将执行的命令。
"""
import argparse, json, re, shlex, subprocess, sys
from pathlib import Path
from urllib.parse import urlsplit
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from curio.config import ROOT, _env

SERVICE = 'curiobject'
PORT = 8790
SSH_OPTS = ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15']
RSYNC_SSH = 'ssh ' + ' '.join(SSH_OPTS)
STEP_TIMEOUT = 600
EXCLUDES = ('.venv/', 'data/', '__pycache__/', '.pytest_cache/', '*.db', '*.db-*')
_HOST_RE = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9_.@-]*$')


class DeployError(Exception):
    pass


def local_version(root=ROOT):
    """server/app.py 里的 VERSION（Mac 端没有 FastAPI，按文本读）。"""
    m = re.search(r"^VERSION = '([^']+)'", (Path(root) / 'server' / 'app.py').read_text(encoding='utf-8'), re.M)
    if not m:
        raise DeployError('读不到 server/app.py 的 VERSION')
    return m.group(1)


def settings(root=ROOT):
    values = _env(root, ('CURIO_SSH_HOST', 'CURIO_REMOTE_DIR', 'CURIO_REMOTE_UV', 'CURIO_SERVER_URL')) or {}
    host = values.get('CURIO_SSH_HOST', '')
    if not host:
        raise DeployError('.env 缺少 CURIO_SSH_HOST（服务器还没买好时不要部署）')
    if not _HOST_RE.match(host):
        raise DeployError('CURIO_SSH_HOST 格式无效')
    remote = (values.get('CURIO_REMOTE_DIR') or '/opt/curiobject').rstrip('/')
    if not remote.startswith('/'):
        raise DeployError('CURIO_REMOTE_DIR 必须为绝对路径')
    url = (values.get('CURIO_SERVER_URL') or '').rstrip('/')
    if url:
        parts = urlsplit(url)
        if parts.scheme != 'https' and parts.hostname not in ('127.0.0.1', 'localhost'):
            raise DeployError('CURIO_SERVER_URL 必须为 https 地址')
    return {'host': host, 'remote_dir': remote, 'uv': values.get('CURIO_REMOTE_UV') or 'uv', 'url': url}


def ssh(host, command):
    return ['ssh', *SSH_OPTS, host, command]


def commands(root, s):
    host, remote, q = s['host'], s['remote_dir'], shlex.quote
    excludes = [a for e in EXCLUDES for a in ('--exclude', e)]
    server_dir = f'{remote}/server'
    return [
        ('建立远端目录', ssh(host, f'mkdir -p {q(server_dir)} {q(remote + "/contract")}')),
        ('同步 server/', ['rsync', '-az', '--delete', '-e', RSYNC_SSH, *excludes,
                          f'{Path(root) / "server"}/', f'{host}:{server_dir}/']),
        ('同步 contract/', ['rsync', '-az', '--delete', '-e', RSYNC_SSH,
                            f'{Path(root) / "contract"}/', f'{host}:{remote}/contract/']),
        ('安装依赖并重启服务', ssh(host, f'cd {q(server_dir)} && UV_PYTHON_INSTALL_DIR={q(remote + "/python")}'
                                       f' {q(s["uv"])} sync --frozen --no-dev'
                                       f' && sudo systemctl restart {SERVICE}')),
    ]


def health_command(s):
    return ssh(s['host'], f'curl -fsS --max-time 5 --retry 10 --retry-delay 1 --retry-connrefused'
                          f' http://127.0.0.1:{PORT}/healthz')


def _check_version(label, text, expected):
    try:
        got = json.loads(text)
    except (ValueError, TypeError):
        raise DeployError(f'{label}返回的不是 JSON') from None
    if not isinstance(got, dict) or got.get('status') != 'ok' or got.get('version') != expected:
        raise DeployError(f'{label}版本不符：期望 {expected}，实际 {got.get("version") if isinstance(got, dict) else "?"}')


def _http_get(url):
    import requests
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    return r.text


def deploy(root=ROOT, runner=subprocess.run, dry_run=False, out=print, http_get=_http_get):
    s = settings(root)
    expected = local_version(root)
    for label, cmd in commands(root, s):
        out(f'· {label}：{shlex.join(cmd)}')
        if dry_run:
            continue
        r = runner(cmd, timeout=STEP_TIMEOUT)
        if r.returncode != 0:
            raise DeployError(f'{label}失败（退出码 {r.returncode}）')
    cmd = health_command(s)
    out(f'· 服务器本机健康检查：{shlex.join(cmd)}')
    if s['url']:
        out(f'· 经 HTTPS 健康检查：{s["url"]}/healthz')
    if dry_run:
        out('（演练，未执行）')
        return
    r = runner(cmd, timeout=120, capture_output=True, text=True)
    if r.returncode != 0:
        raise DeployError(f'服务器本机健康检查失败（退出码 {r.returncode}）')
    _check_version('服务器本机健康检查', r.stdout, expected)
    if s['url']:
        try:
            text = http_get(s['url'] + '/healthz')
        except Exception as e:
            raise DeployError(f'经 HTTPS 健康检查失败（{type(e).__name__}）') from None
        _check_version('经 HTTPS 健康检查', text, expected)
    out(f'部署完成：版本 {expected}')


def main(argv=None, root=ROOT, runner=subprocess.run, http_get=_http_get):
    ap = argparse.ArgumentParser(description='经 SSH 部署服务器（须墨墨授权后才对真实 VPS 执行）')
    ap.add_argument('--dry-run', action='store_true', help='只打印命令，不执行')
    args = ap.parse_args(argv)
    try:
        deploy(root, runner, args.dry_run, http_get=http_get)
    except subprocess.TimeoutExpired as e:
        print(f'部署失败：命令超时（{e.timeout} 秒）', file=sys.stderr)
        return 1
    except (DeployError, OSError) as e:
        print('部署失败：' + str(e), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
