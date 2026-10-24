#!/usr/bin/env bash
# 在服务器上以 root 运行：代码已放在 /opt/health-sources。
# 只建用户、venv、数据目录和 systemd 单元；不碰 nginx、网站或其他服务。
set -euo pipefail
APP=/opt/health-sources
DATA=/var/lib/health-sources

id health-sources >/dev/null 2>&1 || useradd --system --no-create-home --home-dir "$DATA" --shell /usr/sbin/nologin health-sources
install -d -o health-sources -g health-sources -m 0755 "$DATA"

if [ ! -x "$APP/.venv/bin/python" ]; then
  python3 -m venv "$APP/.venv"
fi
"$APP/.venv/bin/pip" install --quiet --disable-pip-version-check -r "$APP/requirements.txt"

install -m 0644 "$APP/deploy/health-sources.service" /etc/systemd/system/health-sources.service
install -m 0644 "$APP/deploy/health-sources.timer" /etc/systemd/system/health-sources.timer
systemctl daemon-reload
systemctl enable --now health-sources.timer
systemctl list-timers health-sources.timer --no-pager
