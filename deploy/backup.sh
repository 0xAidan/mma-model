#!/usr/bin/env bash
# DWCS-505 production backup: SQLite online snapshot + encrypted restic.
# Invoked by deploy/backup-hook.sh (same path the nightly timer already uses).
#
# Never raw-copies the live SQLite file. On failure: exit non-zero so
# deploy/run-job.sh pings Healthchecks /fail, and leave live data intact.
#
# Required env (typically /etc/mma-model/restic.env mode 0600):
#   RESTIC_REPOSITORY   local path or remote (s3:/b2:/sftp:/rest:…)
#   RESTIC_PASSWORD or RESTIC_PASSWORD_FILE
#
# Optional:
#   MMA_DATA_DIR_HOST, MMA_PUBLIC_DIR_HOST, MMA_REPO_ROOT, MMA_ENV_FILE
#   MMA_BACKUP_STAGING, MMA_BACKUP_STAMP, MMA_LOG_DIR
#   MMA_RESTIC_SKIP_CHECK=1  (tests only)
#
# Retention: 7 daily / 4 weekly / 6 monthly.

set -euo pipefail

REPO_ROOT="${MMA_REPO_ROOT:-/opt/mma-model}"
DATA_ROOT="${MMA_DATA_DIR_HOST:-/srv/mma/data}"
PUBLIC_ROOT="${MMA_PUBLIC_DIR_HOST:-/srv/mma/public}"
ENV_FILE="${MMA_ENV_FILE:-/etc/mma-model/mma.env}"
RESTIC_ENV="${MMA_RESTIC_ENV:-/etc/mma-model/restic.env}"
LOG_DIR="${MMA_LOG_DIR:-/var/log/mma-model}"
STAMP_FILE="${MMA_BACKUP_STAMP:-${DATA_ROOT}/backup.last_ok}"
STAGING_ROOT="${MMA_BACKUP_STAGING:-/var/tmp/mma-backup-staging}"
DEPLOY_DIR="${MMA_DEPLOY_DIR:-${REPO_ROOT}/deploy}"
DB_PATH="${MMA_DATABASE_PATH:-${DATA_ROOT}/mma.db}"
ARTIFACTS_DIR="${MMA_ARTIFACTS_DIR:-${DATA_ROOT}/artifacts}"
CONFIG_DIR="${MMA_CONFIG_DIR:-${REPO_ROOT}/config}"
KEEP_DAILY="${MMA_RESTIC_KEEP_DAILY:-7}"
KEEP_WEEKLY="${MMA_RESTIC_KEEP_WEEKLY:-4}"
KEEP_MONTHLY="${MMA_RESTIC_KEEP_MONTHLY:-6}"

mkdir -p "${LOG_DIR}" "${DATA_ROOT}"

log() {
  local line
  line="$(printf '[backup] %s' "$*")"
  printf '%s\n' "${line}"
  printf '%s\n' "${line}" >> "${LOG_DIR}/backup.log" 2>/dev/null || true
}

die() {
  log "ERROR: $*"
  exit 1
}

# Load optional restic credentials without printing them.
if [[ -f "${RESTIC_ENV}" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${RESTIC_ENV}"
  set +a
fi

command -v restic >/dev/null 2>&1 || die "restic not installed"
[[ -f "${DB_PATH}" ]] || die "missing live database ${DB_PATH}"
[[ -n "${RESTIC_REPOSITORY:-}" ]] || die "RESTIC_REPOSITORY unset (see docs/runbooks/backup-restore.md)"
if [[ -z "${RESTIC_PASSWORD:-}" && -z "${RESTIC_PASSWORD_FILE:-}" ]]; then
  die "RESTIC_PASSWORD or RESTIC_PASSWORD_FILE required"
fi

BUNDLE_DIR="${STAGING_ROOT}/bundle-$$"
rm -rf "${BUNDLE_DIR}"
mkdir -p "${BUNDLE_DIR}"

cleanup_staging() {
  rm -rf "${BUNDLE_DIR}" 2>/dev/null || true
}
trap cleanup_staging EXIT

PYTHON_BIN="${MMA_PYTHON:-python3}"
export PYTHONPATH="${REPO_ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"

log "online backup starting db=${DB_PATH} bundle=${BUNDLE_DIR}"

export MMA_BACKUP_DATABASE_PATH="${DB_PATH}"
export MMA_BACKUP_OUTPUT="${BUNDLE_DIR}"
export MMA_BACKUP_DATA_DIR="${DATA_ROOT}"
export MMA_BACKUP_PUBLIC_DIR="${PUBLIC_ROOT}"
export MMA_BACKUP_DEPLOY_DIR="${DEPLOY_DIR}"
export MMA_BACKUP_REPO_ROOT="${REPO_ROOT}"
export MMA_BACKUP_ARTIFACTS_DIR=""
export MMA_BACKUP_ENV_FILE=""
export MMA_BACKUP_CONFIG_DIR=""
if [[ -d "${ARTIFACTS_DIR}" ]]; then
  export MMA_BACKUP_ARTIFACTS_DIR="${ARTIFACTS_DIR}"
fi
if [[ -f "${ENV_FILE}" ]]; then
  export MMA_BACKUP_ENV_FILE="${ENV_FILE}"
fi
if [[ -d "${CONFIG_DIR}" ]]; then
  export MMA_BACKUP_CONFIG_DIR="${CONFIG_DIR}"
fi

# Invoke the stdlib-heavy backup service directly so the host does not need the
# full worker dependency tree (pydantic, sklearn, etc.) just to snapshot SQLite.
"${PYTHON_BIN}" - <<'PY' || die "bundle create failed (live data untouched)"
import os
from pathlib import Path

from mma_model.backup.service import BackupPaths, create_backup_bundle

def _opt(name: str) -> Path | None:
    raw = os.environ.get(name, "").strip()
    return Path(raw) if raw else None

result = create_backup_bundle(
    BackupPaths(
        database_path=Path(os.environ["MMA_BACKUP_DATABASE_PATH"]),
        output_dir=Path(os.environ["MMA_BACKUP_OUTPUT"]),
        data_dir=_opt("MMA_BACKUP_DATA_DIR"),
        public_dir=_opt("MMA_BACKUP_PUBLIC_DIR"),
        artifacts_dir=_opt("MMA_BACKUP_ARTIFACTS_DIR"),
        deploy_dir=_opt("MMA_BACKUP_DEPLOY_DIR"),
        env_file=_opt("MMA_BACKUP_ENV_FILE"),
        config_dir=_opt("MMA_BACKUP_CONFIG_DIR"),
        repo_root=_opt("MMA_BACKUP_REPO_ROOT"),
    )
)
print(
    f"backup create ok bundle={result.bundle_dir} "
    f"integrity={result.integrity_check.splitlines()[0]} "
    f"releases={','.join(result.release_ids) or 'none'} "
    f"artifacts={result.artifact_manifest_count}"
)
PY

# Initialize repo once when empty / missing.
if ! restic cat config >/dev/null 2>&1; then
  log "restic init (new repository)"
  restic init || die "restic init failed"
fi

log "restic backup"
bundle_parent="$(dirname "${BUNDLE_DIR}")"
bundle_name="$(basename "${BUNDLE_DIR}")"
(
  cd "${bundle_parent}"
  restic backup \
    --verbose=1 \
    --host mma-model \
    --tag dwcs-505 \
    --tag mma-backup \
    "${bundle_name}"
) || die "restic backup failed (live data untouched)"

log "restic forget+prune keep-daily=${KEEP_DAILY} keep-weekly=${KEEP_WEEKLY} keep-monthly=${KEEP_MONTHLY}"
restic forget \
  --keep-daily "${KEEP_DAILY}" \
  --keep-weekly "${KEEP_WEEKLY}" \
  --keep-monthly "${KEEP_MONTHLY}" \
  --prune \
  || die "restic forget/prune failed (live data untouched)"

if [[ "${MMA_RESTIC_SKIP_CHECK:-0}" != "1" ]]; then
  log "restic check"
  restic check || die "restic check failed (live data untouched)"
fi

ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf '%s\n' "${ts}" > "${STAMP_FILE}.tmp"
mv "${STAMP_FILE}.tmp" "${STAMP_FILE}"
chmod 644 "${STAMP_FILE}" 2>/dev/null || true
if id -u 10001 >/dev/null 2>&1 || getent passwd 10001 >/dev/null 2>&1; then
  chown 10001:10001 "${STAMP_FILE}" 2>/dev/null || true
fi

log "ok stamp=${STAMP_FILE} at ${ts}"
printf '[backup] ok at %s\n' "${ts}"
exit 0
