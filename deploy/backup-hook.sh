#!/usr/bin/env bash
# DWCS-504 backup command stub / hook.
# DWCS-505 replaces this with SQLite online backup + restic offsite.
# This stub proves the nightly timer, flock path, heartbeat wiring, and
# backup-age monitor seam without implementing backup internals.
#
# Side effects (host only):
#   - touches /srv/mma/data/backup.last_ok (UTC stamp for age monitors)
#   - appends a redacted line to /var/log/mma-model/backup.log
#
# Exit 0 on success. Non-zero fails the systemd oneshot and triggers
# the Healthchecks /fail ping via deploy/run-job.sh.

set -euo pipefail

DATA_ROOT="${MMA_DATA_DIR_HOST:-/srv/mma/data}"
LOG_DIR="${MMA_LOG_DIR:-/var/log/mma-model}"
STAMP_FILE="${MMA_BACKUP_STAMP:-${DATA_ROOT}/backup.last_ok}"
HOOK_MARKER="${MMA_BACKUP_HOOK_MARKER:-dwcs-504-stub}"

mkdir -p "${DATA_ROOT}" "${LOG_DIR}"

ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf '%s\n' "${ts}" > "${STAMP_FILE}.tmp"
mv "${STAMP_FILE}.tmp" "${STAMP_FILE}"
chmod 644 "${STAMP_FILE}" 2>/dev/null || true

# Keep ownership aligned with the worker uid when possible.
if id -u 10001 >/dev/null 2>&1 || getent passwd 10001 >/dev/null 2>&1; then
  chown 10001:10001 "${STAMP_FILE}" 2>/dev/null || true
fi

{
  printf '[backup-hook] %s marker=%s stamp=%s note=DWCS-505-will-replace\n' \
    "${ts}" "${HOOK_MARKER}" "${STAMP_FILE}"
} | tee -a "${LOG_DIR}/backup.log" >/dev/null

printf '[backup-hook] stub ok at %s (DWCS-505 replaces this)\n' "${ts}"
exit 0
