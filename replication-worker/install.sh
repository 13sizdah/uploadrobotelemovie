#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "با دسترسی root اجرا کنید: sudo bash install.sh"
  exit 1
fi

command -v docker >/dev/null 2>&1 || {
  echo "Docker نصب نیست. ابتدا Docker Engine و compose plugin را نصب کنید."
  exit 1
}

read -r -p "آدرس HTTPS کنترلر (مثال https://dl11.example.com): " controller_url
read -r -p "توکن Worker از تنظیمات پنل: " api_token
read -r -p "نام Worker [iran-worker-1]: " worker_id
worker_id="${worker_id:-iran-worker-1}"
read -r -p "نام دقیق فضای مقصد در پنل (مثال parspack): " target_backend
read -r -p "JSON فضاهای S3 مبدا و مقصد (یک خط): " s3_json

umask 077
template="$(mktemp)"
sed \
  -e "s|^CONTROLLER_URL=.*|CONTROLLER_URL=${controller_url%/}|" \
  -e "s|^REPLICATION_API_TOKEN=.*|REPLICATION_API_TOKEN=${api_token}|" \
  -e "s|^WORKER_ID=.*|WORKER_ID=${worker_id}|" \
  -e "s|^TARGET_BACKENDS=.*|TARGET_BACKENDS=${target_backend}|" \
  -e "s|^S3_BACKENDS_JSON=.*|S3_BACKENDS_JSON=${s3_json}|" \
  .env.example > "${template}"
install -m 600 "${template}" .env
rm -f "${template}"

docker compose up -d --build
echo "Worker نصب شد. وضعیت: docker compose ps"
echo "گزارش زنده: docker compose logs -f replication-worker"
