#!/bin/bash
# Curiobject 服务器一次性准备（README "服务器准备清单" 第 4、5 步的脚本化版本）。Ubuntu 24.04，以 root 或 sudo 运行：
#   bash bootstrap_server.sh <域名>
# 前提：root（或经 sudo 运行的用户）的 ~/.ssh/authorized_keys 里已有墨墨 Mac 的公钥（deploy 用户沿用同一把公钥）。
# 做的事：装 rsync/curl/sqlite3、Caddy（官方源）、uv（装到 /usr/local/bin）；建 curiobject 系统用户与 deploy 部署用户；
# 建 /opt/curiobject、/etc/curiobject；生成管理密钥写入 /etc/curiobject/server.env（0600，不打印）；
# deploy 免密 sudo 只限重启服务；Caddy 环境写入域名；ufw 只开 22/80/443；关闭 SSH 密码登录。
# 可重复运行：已存在的用户、目录、密钥不会被覆盖。
set -euo pipefail
DOMAIN="${1:?用法：bash bootstrap_server.sh <域名>}"
[ "$(id -u)" = 0 ] || { echo '请用 root 运行'; exit 1; }
# 公钥来源：经 sudo 运行时取调用者（如阿里云镜像的 admin）的 authorized_keys，否则取 root 的。
KEYS=/root/.ssh/authorized_keys
[ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ] && KEYS="$(getent passwd "$SUDO_USER" | cut -d: -f6)/.ssh/authorized_keys"
[ -s "$KEYS" ] || { echo "$KEYS 为空：先把 Mac 公钥加进去，否则关闭密码登录后会进不来"; exit 1; }

export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q rsync curl sqlite3 ufw debian-keyring debian-archive-keyring apt-transport-https gnupg

# Caddy 官方 apt 源
if ! command -v caddy >/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -q
  apt-get install -y -q caddy
fi

# uv（系统级，非交互 SSH 的 PATH 里也找得到）
if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
fi

# 用户
id curiobject >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin curiobject
id deploy >/dev/null 2>&1 || useradd --create-home --shell /bin/bash deploy
usermod -aG curiobject deploy
install -d -m 700 -o deploy -g deploy /home/deploy/.ssh
install -m 600 -o deploy -g deploy "$KEYS" /home/deploy/.ssh/authorized_keys
printf 'deploy ALL=(root) NOPASSWD: /usr/bin/systemctl restart curiobject\n' > /etc/sudoers.d/curiobject-deploy
chmod 440 /etc/sudoers.d/curiobject-deploy
visudo -cf /etc/sudoers.d/curiobject-deploy >/dev/null

# 目录与设置
install -d -m 755 -o deploy -g deploy /opt/curiobject
install -d -m 755 /etc/curiobject
if [ ! -s /etc/curiobject/server.env ]; then
  umask 077
  printf 'CURIO_ADMIN_TOKEN=%s\nCURIO_SERVER_DB=/var/lib/curiobject/server.db\n' "$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '=\n')" > /etc/curiobject/server.env
  umask 022
fi
chown root:root /etc/curiobject/server.env
chmod 600 /etc/curiobject/server.env

# Caddy 的域名（Caddyfile 本身在首次部署后从 /opt/curiobject/server/Caddyfile 复制）
install -d /etc/systemd/system/caddy.service.d
printf '[Service]\nEnvironment=CURIO_DOMAIN=%s\n' "$DOMAIN" > /etc/systemd/system/caddy.service.d/curiobject.conf
systemctl daemon-reload

# 防火墙
ufw allow 22/tcp >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null

# 只允许密钥登录
printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\n' > /etc/ssh/sshd_config.d/10-curiobject.conf
systemctl reload ssh

echo "准备完成：caddy $(caddy version | cut -d' ' -f1)，uv $(uv --version | cut -d' ' -f2)，域名 $DOMAIN"
