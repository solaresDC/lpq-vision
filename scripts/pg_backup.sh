#!/usr/bin/env bash
# LPQ_VISION - scripts/pg_backup.sh (T-E2): nightly pg_dump of the lpq database to a dated file.
# Runs on the HOST (laptop for the proof, the server under a systemd timer): it asks the postgres
# container to dump, custom format (-Fc, restorable with pg_restore), and keeps KEEP_DAYS days.
# Every knob is a variable below or an environment override; no secret is needed (the dump runs
# inside the container, where local connections are trusted).
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/opt/lpq_vision}"
COMPOSE_FILE="${COMPOSE_FILE:-compose.mac.yaml}"
BACKUP_DIR="${BACKUP_DIR:-${PROJECT_DIR}/backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"
DB_USER="${DB_USER:-lpq}"
DB_NAME="${DB_NAME:-lpq}"

stamp="$(date +%Y%m%d_%H%M)"
target="${BACKUP_DIR}/lpq_${stamp}.dump"
tmp="${target}.part"

mkdir -p "${BACKUP_DIR}"
cd "${PROJECT_DIR}"

echo "pg_backup: dumping ${DB_NAME} -> ${target}"
docker compose -f "${COMPOSE_FILE}" exec -T postgres pg_dump -U "${DB_USER}" -d "${DB_NAME}" -Fc > "${tmp}"
mv "${tmp}" "${target}"                       # atomic: a half-written dump never carries the final name
size="$(stat -c %s "${target}" 2>/dev/null || stat -f %z "${target}")"
echo "pg_backup: done, ${size} bytes"

# Retention: delete dumps older than KEEP_DAYS; today's file is never eligible.
find "${BACKUP_DIR}" -name 'lpq_*.dump' -type f -mtime +"${KEEP_DAYS}" -print -delete | sed 's/^/pg_backup: pruned /' || true
echo "pg_backup: kept $(ls -1 "${BACKUP_DIR}"/lpq_*.dump 2>/dev/null | wc -l) dump(s) in ${BACKUP_DIR}"
