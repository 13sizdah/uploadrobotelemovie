#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 1 ]]; then
  echo "Usage: sudo $0 /path/to/uploadrobotelemovie-full-TIMESTAMP.tar"
  exit 1
fi
if [[ "${EUID}" -ne 0 ]]; then
  echo "برای بازگردانی با sudo اجرا کنید."
  exit 1
fi

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARCHIVE="$(realpath "$1")"
if [[ ! -f "${ARCHIVE}" ]]; then
  echo "فایل بکاپ پیدا نشد: ${ARCHIVE}"
  exit 1
fi
if ! tar -tf "${ARCHIVE}" | grep -qx '.env'; then
  echo "بکاپ معتبر نیست: فایل .env وجود ندارد."
  exit 1
fi
if tar -tf "${ARCHIVE}" | grep -Eq '(^/|(^|/)\.\.(/|$))'; then
  echo "بکاپ دارای مسیر ناامن است و بازگردانی نشد."
  exit 1
fi
if [[ -f "${ARCHIVE}.sha256" ]]; then
  (cd "$(dirname "${ARCHIVE}")" && sha256sum -c "$(basename "${ARCHIVE}.sha256")")
fi

echo "این عملیات سرویس را متوقف و داده فعلی را جایگزین می‌کند."
read -r -p "برای ادامه عبارت RESTORE را وارد کنید: " CONFIRM
if [[ "${CONFIRM}" != "RESTORE" ]]; then
  echo "لغو شد."
  exit 1
fi

cd "${PROJECT_DIR}"
if [[ -f .env ]]; then
  docker compose down
fi
RECOVERY_DIR="${PROJECT_DIR}/.restore-recovery-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "${RECOVERY_DIR}"
[[ -f .env ]] && mv .env "${RECOVERY_DIR}/.env"
[[ -d data ]] && mv data "${RECOVERY_DIR}/data"

if ! tar -xf "${ARCHIVE}" -C "${PROJECT_DIR}"; then
  echo "استخراج ناموفق بود؛ داده قبلی در ${RECOVERY_DIR} محفوظ است."
  exit 1
fi
chmod 600 .env
mkdir -p data
docker compose up -d --build
echo "بازیابی انجام شد. نسخه قبل در ${RECOVERY_DIR} قرار دارد."
