#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="${1:-/var/backups/uploadrobotelemovie}"
MODE="${2:-full}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-7}"

if [[ "${MODE}" != "full" && "${MODE}" != "metadata" ]]; then
  echo "Usage: $0 [backup-directory] [full|metadata]"
  exit 1
fi
if [[ ! -f "${PROJECT_DIR}/compose.yaml" || ! -f "${PROJECT_DIR}/.env" ]]; then
  echo "compose.yaml یا .env پیدا نشد؛ اسکریپت را از پروژه نصب‌شده اجرا کنید."
  exit 1
fi
if ! [[ "${RETENTION_DAYS}" =~ ^[0-9]+$ ]] || [[ "${RETENTION_DAYS}" -lt 1 ]]; then
  echo "BACKUP_RETENTION_DAYS باید یک عدد مثبت باشد."
  exit 1
fi

mkdir -p "${BACKUP_DIR}"
chmod 700 "${BACKUP_DIR}"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
ARCHIVE="${BACKUP_DIR}/uploadrobotelemovie-${MODE}-${TIMESTAMP}.tar"
CHECKSUM="${ARCHIVE}.sha256"
WAS_RUNNING="$(docker compose -f "${PROJECT_DIR}/compose.yaml" ps --status running -q file-link-bot 2>/dev/null || true)"

restart_bot() {
  if [[ -n "${WAS_RUNNING}" ]]; then
    docker compose -f "${PROJECT_DIR}/compose.yaml" start file-link-bot >/dev/null
  fi
}
trap restart_bot EXIT

if [[ -n "${WAS_RUNNING}" ]]; then
  echo "توقف کوتاه ربات برای تهیه snapshot سازگار…"
  docker compose -f "${PROJECT_DIR}/compose.yaml" stop -t 30 file-link-bot >/dev/null
fi

if [[ "${MODE}" == "full" ]]; then
  tar -C "${PROJECT_DIR}" -cf "${ARCHIVE}" .env compose.yaml data
else
  TMP_DIR="$(mktemp -d)"
  trap 'rm -rf "${TMP_DIR}"; restart_bot' EXIT
  mkdir -p "${TMP_DIR}/data"
  cp "${PROJECT_DIR}/.env" "${PROJECT_DIR}/compose.yaml" "${TMP_DIR}/"
  cp "${PROJECT_DIR}/data/files.sqlite3" "${TMP_DIR}/data/" 2>/dev/null || true
  cp "${PROJECT_DIR}/data/files.sqlite3-wal" "${TMP_DIR}/data/" 2>/dev/null || true
  cp "${PROJECT_DIR}/data/files.sqlite3-shm" "${TMP_DIR}/data/" 2>/dev/null || true
  tar -C "${TMP_DIR}" -cf "${ARCHIVE}" .env compose.yaml data
fi

chmod 600 "${ARCHIVE}"
if command -v sha256sum >/dev/null 2>&1; then
  (cd "${BACKUP_DIR}" && sha256sum "$(basename "${ARCHIVE}")") > "${CHECKSUM}"
else
  (cd "${BACKUP_DIR}" && shasum -a 256 "$(basename "${ARCHIVE}")") > "${CHECKSUM}"
fi
chmod 600 "${CHECKSUM}"

find "${BACKUP_DIR}" -maxdepth 1 -type f \
  \( -name 'uploadrobotelemovie-*.tar' -o -name 'uploadrobotelemovie-*.tar.sha256' \) \
  -mtime "+${RETENTION_DAYS}" -delete

echo "بکاپ ساخته شد: ${ARCHIVE}"
echo "Checksum: ${CHECKSUM}"
if [[ "${MODE}" == "metadata" ]]; then
  echo "توجه: این بکاپ فایل‌های آپلودشده را شامل نمی‌شود."
fi
