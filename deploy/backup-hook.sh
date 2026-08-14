#!/usr/bin/env bash
# DWCS-505 backup hook — entrypoint for run-job.sh backup / mma-backup.service.
# Delegates to deploy/backup.sh so the systemd unit path stays stable.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_SH="${MMA_BACKUP_SH:-${SCRIPT_DIR}/backup.sh}"

if [[ ! -f "${BACKUP_SH}" ]]; then
  printf '[backup-hook] ERROR: missing %s\n' "${BACKUP_SH}" >&2
  exit 1
fi

exec /bin/bash "${BACKUP_SH}"
