"""Finder 启动入口：确保每 5 分钟唤醒的采集调度任务已加载，不打开浏览器。"""
import fcntl
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.install_hourly import LABEL, ROOT

PROJECT = ROOT


def start():
    python = PROJECT / '.venv/bin/python'
    if not python.exists():
        raise RuntimeError('Curio 后台环境不存在：' + str(PROJECT))
    data = PROJECT / 'data'
    data.mkdir(exist_ok=True)
    with (data / 'desktop-background.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        target = 'gui/' + str(os.getuid()) + '/' + LABEL
        loaded = subprocess.run(['/bin/launchctl', 'print', target], capture_output=True)
        if loaded.returncode == 0:
            return  # Repeated Finder clicks neither reset the timer nor launch extra collectors.
        subprocess.run([str(python), str(PROJECT / 'scripts/install_hourly.py')],
                       cwd=str(PROJECT), check=True, capture_output=True)
        subprocess.run(['/bin/launchctl', 'kickstart', target], check=True, capture_output=True)


if __name__ == '__main__':
    start()
