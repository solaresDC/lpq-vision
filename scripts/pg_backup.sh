#!/usr/bin/env bash
# LPQ_VISION - scripts/pg_backup.sh (T-E2): nightly pg_dump of the lpq database to a dated file and, from Fase 3
# (Section 3.17), the operator state beside it: a copy of config.yaml and of textos.yaml under the same stamp.
# Runs on the HOST (laptop for the proof, the server under a systemd timer that runs this file in place): it asks
# the postgres container to dump, custom format (-Fc, restorable with pg_restore), copies the two state files, and
# keeps KEEP_DAYS days of all three. Every file it writes is readable by its owner only (umask 077): the dump holds
# the password hashes and the gafetes, config.yaml the chat ids.
# Every knob is a variable below or an environment override; no secret is needed (the dump runs inside the
# container, where local connections are trusted).
# The restore road: the dump with pg_restore reading stdin, no file argument (CLAUDE.md rule 20); the two copies
# go back into config/ under their own names (config.yaml, textos.yaml) with the stack stopped.
set -euo pipefail
umask 077

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

# The operator state (Section 3.17): config.yaml always exists; textos.yaml exists once a bot text was edited (absent
# = the factory texts, nothing to copy). Same stamp as the dump, the same atomic rename. A missing config.yaml keeps the
# dump and the retention, and ends the run failed.
status=0
for name in config.yaml textos.yaml; do
    source_file="config/${name}"
    copy="${BACKUP_DIR}/lpq_${stamp}.${name}"
    if [ ! -f "${source_file}" ]; then
        rm -f "${copy}"                       # a copy of this same minute's earlier run never stays beside this dump
        if [ "${name}" = "config.yaml" ]; then
            echo "pg_backup: ${source_file} is missing: the dump is kept, this run ends failed" >&2
            status=1
        else
            echo "pg_backup: ${source_file} absent (the factory texts are in use), nothing to copy"
        fi
        continue
    fi
    cp "${source_file}" "${copy}.part"
    mv "${copy}.part" "${copy}"
    echo "pg_backup: copied ${source_file} -> ${copy}, $(stat -c %s "${copy}" 2>/dev/null || stat -f %z "${copy}") bytes"
done

# Retention: delete what is older than KEEP_DAYS in BACKUP_DIR itself (never in a folder inside it): the dumps, both
# copies, and a .part a crashed run left; today's files are never eligible.
for kind in dump config.yaml textos.yaml part; do
    find "${BACKUP_DIR}" -maxdepth 1 -name "lpq_*.${kind}" -type f -mtime +"${KEEP_DAYS}" -print -delete | sed 's/^/pg_backup: pruned /' || true
done
echo "pg_backup: kept $(ls -1 "${BACKUP_DIR}"/lpq_*.dump 2>/dev/null | wc -l) dump(s), $(ls -1 "${BACKUP_DIR}"/lpq_*.config.yaml 2>/dev/null | wc -l) config.yaml and $(ls -1 "${BACKUP_DIR}"/lpq_*.textos.yaml 2>/dev/null | wc -l) textos.yaml copies in ${BACKUP_DIR}"
exit "${status}"
