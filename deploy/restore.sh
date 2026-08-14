#!/usr/bin/env bash
# DWCS-505 empty-target restore from an encrypted restic repository.
#
# Usage:
#   deploy/restore.sh --target /tmp/mma-restore-empty
#   deploy/restore.sh --target /tmp/mma-restore-empty --snapshot latest
#   deploy/restore.sh --target /tmp/mma-restore-empty --serve-port 8765
#
# Restores into a completely empty directory, runs PRAGMA integrity_check,
# upgrades Alembic to head on a *copy* of the restored DB (never the live DB),
# optionally loads model artifacts, and can publish a localhost-only proof tree.
#
# Does not mutate /srv/mma or live Caddy. Does not delete local production data
# when restore fails.

set -euo pipefail

REPO_ROOT="${MMA_REPO_ROOT:-/opt/mma-model}"
RESTIC_ENV="${MMA_RESTIC_ENV:-/etc/mma-model/restic.env}"
TARGET=""
SNAPSHOT="latest"
SERVE_PORT=""
SKIP_MIGRATE=0
SKIP_ARTIFACT_LOAD=0
PROOF_PUBLIC=""

usage() {
  sed -n '1,20p' "$0"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target) TARGET="${2:-}"; shift 2 ;;
    --snapshot) SNAPSHOT="${2:-}"; shift 2 ;;
    --serve-port) SERVE_PORT="${2:-}"; shift 2 ;;
    --proof-public) PROOF_PUBLIC="${2:-}"; shift 2 ;;
    --skip-migrate) SKIP_MIGRATE=1; shift ;;
    --skip-artifact-load) SKIP_ARTIFACT_LOAD=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'restore: unknown arg %s\n' "$1" >&2; exit 2 ;;
  esac
done

die() { printf '[restore] ERROR: %s\n' "$*" >&2; exit 1; }
log() { printf '[restore] %s\n' "$*"; }

[[ -n "${TARGET}" ]] || die "--target is required"
TARGET="$(mkdir -p "$(dirname "${TARGET}")" && cd "$(dirname "${TARGET}")" && pwd)/$(basename "${TARGET}")"

if [[ -f "${RESTIC_ENV}" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${RESTIC_ENV}"
  set +a
fi

command -v restic >/dev/null 2>&1 || die "restic not installed"
[[ -n "${RESTIC_REPOSITORY:-}" ]] || die "RESTIC_REPOSITORY unset"
if [[ -z "${RESTIC_PASSWORD:-}" && -z "${RESTIC_PASSWORD_FILE:-}" ]]; then
  die "RESTIC_PASSWORD or RESTIC_PASSWORD_FILE required"
fi

if [[ -e "${TARGET}" ]]; then
  if [[ -d "${TARGET}" ]] && [[ -z "$(ls -A "${TARGET}" 2>/dev/null || true)" ]]; then
    :
  else
    die "target must be absent or completely empty: ${TARGET}"
  fi
fi
mkdir -p "${TARGET}"

START_EPOCH="$(date +%s)"
log "restic restore snapshot=${SNAPSHOT} -> ${TARGET}"
restic restore "${SNAPSHOT}" --target "${TARGET}" || die "restic restore failed"

# restic restores the backup path layout; locate the bundle root.
BUNDLE="${TARGET}"
if [[ ! -d "${BUNDLE}/sqlite" ]]; then
  # restic keeps the absolute source path under --target, so sqlite/ may be deep.
  child="$(find "${TARGET}" -type d -name sqlite 2>/dev/null | head -1 || true)"
  if [[ -n "${child}" ]]; then
    BUNDLE="$(dirname "${child}")"
  fi
fi
[[ -d "${BUNDLE}/sqlite" ]] || die "restored tree missing sqlite/ under ${TARGET}"

export PYTHONPATH="${REPO_ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"
PYTHON_BIN="${MMA_PYTHON:-python3}"

log "verify restored bundle at ${BUNDLE}"
export MMA_RESTORE_BUNDLE="${BUNDLE}"
export MMA_RESTORE_LOAD_ARTIFACTS="0"
if [[ "${SKIP_ARTIFACT_LOAD}" -eq 0 ]]; then
  # Only attempt full artifact loads when the modeling stack is importable.
  if "${PYTHON_BIN}" - <<'PY'
import importlib.util
import sys
ok = importlib.util.find_spec("sklearn") is not None
sys.exit(0 if ok else 1)
PY
  then
    export MMA_RESTORE_LOAD_ARTIFACTS="1"
  else
    log "modeling stack unavailable; skipping artifact load (integrity still checked)"
  fi
fi
VERIFY_JSON="$("${PYTHON_BIN}" - <<'PY'
import json
import os
from pathlib import Path

from mma_model.backup.service import verify_restored_bundle

bundle = Path(os.environ["MMA_RESTORE_BUNDLE"])
load = os.environ.get("MMA_RESTORE_LOAD_ARTIFACTS", "1") == "1"
report = verify_restored_bundle(
    bundle,
    require_artifacts=False,
    load_artifacts=load,
)
print(json.dumps(report, sort_keys=True))
PY
)" || die "bundle verification failed"

printf '%s\n' "${VERIFY_JSON}" | "${PYTHON_BIN}" -c 'import json,sys; d=json.load(sys.stdin); print("integrity=", d.get("integrity_check")); print("current=", d.get("current_release")); print("loaded=", d.get("loaded_artifacts"))'

DB_SRC="${BUNDLE}/sqlite/mma.db"
[[ -f "${DB_SRC}" ]] || die "missing restored db ${DB_SRC}"

if [[ "${SKIP_MIGRATE}" -eq 0 ]]; then
  MIGRATE_DB="${TARGET}/migrate-check.db"
  cp -f "${DB_SRC}" "${MIGRATE_DB}"
  log "alembic upgrade head on disposable copy ${MIGRATE_DB}"
  migrate_ok=0
  if [[ -f "${REPO_ROOT}/alembic.ini" ]]; then
    set +e
    (
      cd "${REPO_ROOT}"
      export MMA_DATABASE_URL="sqlite:///${MIGRATE_DB}"
      if command -v alembic >/dev/null 2>&1; then
        alembic -c alembic.ini upgrade head
      else
        "${PYTHON_BIN}" -m alembic -c alembic.ini upgrade head
      fi
    )
    host_migrate_status=$?
    set -e
    if [[ "${host_migrate_status}" -eq 0 ]]; then
      migrate_ok=1
    else
      log "host alembic unavailable/failed; trying compose worker"
      COMPOSE_FILE="${MMA_COMPOSE_FILE:-${REPO_ROOT}/deploy/compose.yaml}"
      ENV_FILE="${MMA_ENV_FILE:-/etc/mma-model/mma.env}"
      if [[ -f "${COMPOSE_FILE}" ]] && command -v docker >/dev/null 2>&1; then
        set +e
        docker compose -f "${COMPOSE_FILE}" run --rm --no-deps \
          -v "${MIGRATE_DB}:/tmp/migrate-check.db" \
          -e MMA_DATABASE_URL="sqlite:////tmp/migrate-check.db" \
          --entrypoint /bin/sh \
          worker \
          -c 'cd /app && alembic -c alembic.ini upgrade head' \
          >/tmp/mma-restore-migrate.log 2>&1
        docker_migrate_status=$?
        set -e
        if [[ "${docker_migrate_status}" -eq 0 ]]; then
          migrate_ok=1
        else
          log "compose migrate failed; see /tmp/mma-restore-migrate.log"
        fi
      fi
    fi
  fi
  if [[ "${migrate_ok}" -eq 1 ]]; then
    export MMA_RESTORE_MIGRATE_DB="${MIGRATE_DB}"
    "${PYTHON_BIN}" - <<'PY'
import os
import sqlite3
from pathlib import Path

path = Path(os.environ["MMA_RESTORE_MIGRATE_DB"])
conn = sqlite3.connect(path)
row = conn.execute("PRAGMA integrity_check").fetchone()
conn.close()
assert row and row[0] == "ok", row
print("post-migrate integrity_check=ok")
PY
  else
    log "WARNING: skipped alembic upgrade verification (host deps / worker unavailable)"
    "${PYTHON_BIN}" - <<'PY'
import os
import sqlite3
from pathlib import Path

path = Path(os.environ["MMA_RESTORE_BUNDLE"]) / "sqlite" / "mma.db"
conn = sqlite3.connect(path)
row = conn.execute("PRAGMA integrity_check").fetchone()
conn.close()
assert row and row[0] == "ok", row
print("restored sqlite integrity_check=ok (migrate skipped)")
PY
  fi
fi

# Optional: materialize a proof public tree without touching live /srv/mma/public.
if [[ -n "${PROOF_PUBLIC}" ]]; then
  [[ -e "${PROOF_PUBLIC}" ]] && [[ -n "$(ls -A "${PROOF_PUBLIC}" 2>/dev/null || true)" ]] \
    && die "proof public target must be empty: ${PROOF_PUBLIC}"
  mkdir -p "${PROOF_PUBLIC}"
  if [[ -d "${BUNDLE}/public" ]]; then
    cp -a "${BUNDLE}/public/." "${PROOF_PUBLIC}/"
  fi
  log "proof public tree at ${PROOF_PUBLIC}"
fi

if [[ -n "${SERVE_PORT}" ]]; then
  SERVE_ROOT="${PROOF_PUBLIC:-${BUNDLE}/public}"
  [[ -d "${SERVE_ROOT}" ]] || die "nothing to serve at ${SERVE_ROOT}"
  log "serving restored dashboard proof on 127.0.0.1:${SERVE_PORT} (Ctrl-C to stop)"
  exec "${PYTHON_BIN}" -m http.server "${SERVE_PORT}" --bind 127.0.0.1 --directory "${SERVE_ROOT}"
fi

END_EPOCH="$(date +%s)"
ELAPSED="$((END_EPOCH - START_EPOCH))"
log "restore ok elapsed_sec=${ELAPSED} bundle=${BUNDLE}"
printf '%s\n' "${ELAPSED}" > "${TARGET}/restore.elapsed_sec"
exit 0
