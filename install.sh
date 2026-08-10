#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "این اسکریپت باید با sudo اجرا شود: sudo bash install.sh"
  exit 1
fi

if ! command -v apt-get >/dev/null 2>&1; then
  echo "این نصب خودکار فعلاً برای Ubuntu و Debian طراحی شده است."
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl python3 nginx certbot python3-certbot-nginx

if ! python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 10))'; then
  echo "Python 3.10 یا جدیدتر لازم است. از Ubuntu 22.04+ یا Debian 12+ استفاده کنید."
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/"$(. /etc/os-release && echo "$ID")"/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/$ID $VERSION_CODENAME stable" > /etc/apt/sources.list.d/docker.list
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi

systemctl enable --now docker nginx
echo "نصب‌کننده روی پورت 9090 اجرا می‌شود. پس از پایان، با Ctrl+C آن را ببندید."
exec python3 installer/server.py
