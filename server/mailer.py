"""可替换的发信接口（找回密码用，CAP-13、AD-20）。

服务器只调用 `mailer.send(to, subject, body)`。发信服务在购买服务器时再选（见 README）；
在那之前只有本机实现 `FileMailer`：把每封邮件写成 `.eml` 文件，不连接任何邮件服务。

设置（环境变量）：
- `CURIO_MAILER`：`file`（默认，唯一已实现）。
- `CURIO_MAIL_DIR`：`FileMailer` 的目录，默认 `server/data/mail/`。

邮件正文含一次性重设链接，等同于密码：`.eml` 文件只用于本机测试，目录权限 0700、文件 0600。
"""
import os
import uuid
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_MAIL_DIR = HERE / 'data' / 'mail'
FROM_ADDRESS = 'Curiobject <no-reply@curiobject.invalid>'


class MailError(Exception):
    pass


class FileMailer:
    """本机测试用：每封信写一个 .eml 文件。"""

    def __init__(self, directory=None):
        self.dir = Path(directory or DEFAULT_MAIL_DIR)

    def send(self, to, subject, body):
        msg = EmailMessage()
        msg['From'] = FROM_ADDRESS
        msg['To'] = to
        msg['Subject'] = subject
        msg['Date'] = datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S +0000')
        msg.set_content(body)
        try:
            self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            path = self.dir / f'{stamp}-{uuid.uuid4().hex[:8]}.eml'
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as f:
                f.write(bytes(msg))
        except OSError as e:
            raise MailError(f'邮件未能写入（{type(e).__name__}）') from None
        return path


def from_env():
    kind = os.environ.get('CURIO_MAILER', 'file')
    if kind == 'file':
        return FileMailer(os.environ.get('CURIO_MAIL_DIR') or None)
    raise MailError(f'未实现的 CURIO_MAILER：{kind}')
