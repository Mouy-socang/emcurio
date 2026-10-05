"""Finder launcher: ensure the hourly collector is loaded, without opening a browser."""
import fcntl
import os
from pathlib import Path
import subprocess

PROJECT = Path('/Users/shichunxiao/.codex/.chatgpt-projects/g-p-6a61c1bbe9f88191bd79146cc18d7fa0/curio-phase1')
LABEL = 'com.curio.collect.phase1'


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
