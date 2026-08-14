#!/usr/bin/env bash
# DWCS-504 host job runner: one global flock + pinned Compose worker + heartbeats.
# Intended for systemd oneshots on the root-site host. Secrets stay in
# /etc/mma-model/*.env (0600). Never print Healthchecks write URLs or API keys.
#
# Usage:
#   deploy/run-job.sh tick
#   deploy/run-job.sh backup
#   deploy/run-job.sh --force-fail tick   # verification only
#
# Exit codes:
#   0   success
#   75  overlap (flock busy) — EX_TEMPFAIL
#   1   job failure
#   2   usage / configuration error
#
# Pinned-image note (DWCS-503 digest): the console script resolves alembic /
# contracts relative to site-packages. The worker therefore runs the CLI from
# /app via PYTHONPATH=src. Until a later digest includes the DWCS-504 DB guard
# and DWCS-505 backup package, run-job may overlay jobs/db_guard.py + cli.py
# and (when present) the host src/mma_model/backup/ directory onto
# /app/src/mma_model. Never bind-mount the whole host repo as tick_root
# (circular import failure). Production jobs use explicit sqlite:////data/mma.db
# (canonical /data/mma.db).

set -euo pipefail

FORCE_FAIL=0
JOB=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --force-fail) FORCE_FAIL=1; shift ;;
    -h|--help)
      printf 'Usage: %s [--force-fail] <tick|backup>\n' "$(basename "$0")"
      exit 0
      ;;
    *)
      if [[ -n "${JOB}" ]]; then
        printf 'run-job: unexpected argument: %s\n' "$1" >&2
        exit 2
      fi
      JOB="$1"
      shift
      ;;
  esac
done

[[ -n "${JOB}" ]] || { printf 'run-job: missing job name (tick|backup)\n' >&2; exit 2; }

LOCK_FILE="${MMA_WRITER_LOCK:-/run/mma-writer.lock}"
REPO_ROOT="${MMA_REPO_ROOT:-/opt/mma-model}"
COMPOSE_FILE="${MMA_COMPOSE_FILE:-${REPO_ROOT}/deploy/compose.yaml}"
ENV_FILE="${MMA_ENV_FILE:-/etc/mma-model/mma.env}"
MONITORING_ENV="${MMA_MONITORING_ENV:-/etc/mma-model/monitoring.env}"
LOG_DIR="${MMA_LOG_DIR:-/var/log/mma-model}"
BACKUP_HOOK="${MMA_BACKUP_HOOK:-${REPO_ROOT}/deploy/backup-hook.sh}"
RUNTIME_BOUND_SEC="${MMA_JOB_TIMEOUT_SEC:-1500}"
# Canonical absolute container DB (four slashes). Relative sqlite:///data/mma.db
# remains refused by the CLI guard.
CANONICAL_JOBS_DB_URL="sqlite:////data/mma.db"

mkdir -p "${LOG_DIR}"
chmod 755 "${LOG_DIR}" 2>/dev/null || true

# shellcheck disable=SC1090
if [[ -f "${ENV_FILE}" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${ENV_FILE}"
  set +a
fi

# shellcheck disable=SC1090
if [[ -f "${MONITORING_ENV}" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${MONITORING_ENV}"
  set +a
fi

# Prefer explicit override, then mma.env MMA_DATABASE_URL, then canonical absolute.
JOBS_DATABASE_URL="${MMA_JOBS_DATABASE_URL:-${MMA_DATABASE_URL:-${CANONICAL_JOBS_DB_URL}}}"
# Never silently fall back to a relative live URL from a mis-set env.
if [[ "${JOBS_DATABASE_URL}" == "sqlite:///data/mma.db" \
   || "${JOBS_DATABASE_URL}" == "sqlite:///./data/mma.db" ]]; then
  JOBS_DATABASE_URL="${CANONICAL_JOBS_DB_URL}"
fi

redact() {
  # Redact secrets before they hit journals or log files.
  sed -E \
    -e 's#(hc-ping\.com/)[A-Za-z0-9_-]+#\1[REDACTED]#g' \
    -e 's#(healthchecks\.io/ping/)[A-Za-z0-9_-]+#\1[REDACTED]#g' \
    -e 's#([Tt][Hh][Ee]_[Oo][Dd][Dd][Ss]_[Aa][Pp][Ii]_[Kk][Ee][Yy][[:space:]]*[=:][[:space:]]*)[^[:space:]]+#\1[REDACTED]#g' \
    -e 's#([Aa][Pp][Ii][_-]?[Kk][Ee][Yy][[:space:]]*[=:][[:space:]]*)[^[:space:]]+#\1[REDACTED]#g' \
    -e 's#([Pp][Aa][Ss][Ss][Ww][Oo][Rr][Dd][[:space:]]*[=:][[:space:]]*)[^[:space:]]+#\1[REDACTED]#g' \
    -e 's#([Tt][Oo][Kk][Ee][Nn][[:space:]]*[=:][[:space:]]*)[^[:space:]]+#\1[REDACTED]#g' \
    -e 's#(Bearer[[:space:]]+)[A-Za-z0-9._~+/=-]+#\1[REDACTED]#g' \
    -e 's#://([^:/@]+):([^@/]+)@#://\1:[REDACTED]@#g'
}

log() {
  local line
  line="$(printf '[run-job] %s' "$*" | redact)"
  printf '%s\n' "${line}"
  printf '%s\n' "${line}" >> "${LOG_DIR}/scheduler.log" 2>/dev/null || true
}

die() {
  log "ERROR: $*"
  exit 1
}

heartbeat_base_for_job() {
  case "$1" in
    tick) printf '%s' "${MMA_HC_SCHEDULER_URL:-}" ;;
    backup) printf '%s' "${MMA_HC_BACKUP_URL:-}" ;;
    *) printf '' ;;
  esac
}

ping_heartbeat() {
  # $1 = start|success|fail
  local kind="$1"
  local base
  base="$(heartbeat_base_for_job "${JOB}")"
  if [[ -z "${base}" || "${base}" == *PLACEHOLDER* ]]; then
    log "heartbeat ${kind}: skipped (no ${JOB} Healthchecks URL configured)"
    return 0
  fi
  local url="${base}"
  case "${kind}" in
    start) url="${base%/}/start" ;;
    fail) url="${base%/}/fail" ;;
    success) url="${base}" ;;
    *) return 0 ;;
  esac
  if curl -fsS -m 10 -o /dev/null "${url}"; then
    log "heartbeat ${kind}: ok"
  else
    log "heartbeat ${kind}: transport failed (non-fatal)"
  fi
}

require_compose() {
  [[ -f "${COMPOSE_FILE}" ]] || die "missing compose file ${COMPOSE_FILE}"
  [[ -f "${ENV_FILE}" ]] || die "missing env file ${ENV_FILE}"
  command -v docker >/dev/null 2>&1 || die "docker not installed"
  docker compose version >/dev/null 2>&1 || die "docker compose plugin missing"
}

run_compose_tick() {
  local now
  now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  # Default: image /app + PYTHONPATH=src (working path proven at 17:40Z).
  # Do NOT bind-mount the whole host git tree as tick_root — that causes
  # circular module load failures (compute_code_hash / modeling.artifacts).
  #
  # Until the next digest includes the DWCS-504 DB guard and DWCS-505 backup
  # package, overlay only:
  #   jobs/db_guard.py, cli.py, and (when present) backup/
  # onto /app/src/mma_model/... when those host paths exist.
  # Never mount the whole REPO_ROOT as tick_root.
  local -a volume_args=()
  local host_cli="${REPO_ROOT}/src/mma_model/cli.py"
  local host_guard="${REPO_ROOT}/src/mma_model/jobs/db_guard.py"
  local host_backup="${REPO_ROOT}/src/mma_model/backup"
  if [[ -f "${host_guard}" && -f "${host_cli}" ]]; then
    volume_args=(
      -v "${host_guard}:/app/src/mma_model/jobs/db_guard.py:ro"
      -v "${host_cli}:/app/src/mma_model/cli.py:ro"
    )
    local overlay_note="db_guard.py + cli.py"
    if [[ -d "${host_backup}" ]]; then
      volume_args+=(
        -v "${host_backup}:/app/src/mma_model/backup:ro"
      )
      overlay_note="${overlay_note} + backup/"
    fi
    log "overlay: ${overlay_note} onto /app/src/mma_model"
  else
    log "overlay: skipped (host guard/cli missing; using image sources)"
  fi
  local -a cmd=(
    docker compose -f "${COMPOSE_FILE}" run --rm --no-deps
    "${volume_args[@]}"
    --entrypoint /bin/sh
    worker
    -c
    "cd /app && PYTHONPATH=src python -m mma_model.cli jobs tick --now '${now}' --database-url '${JOBS_DATABASE_URL}' --live --publish-root /public"
  )
  if command -v timeout >/dev/null 2>&1; then
    timeout --signal=TERM --kill-after=30s "${RUNTIME_BOUND_SEC}" "${cmd[@]}"
  else
    "${cmd[@]}"
  fi
}

run_backup() {
  [[ -x "${BACKUP_HOOK}" || -f "${BACKUP_HOOK}" ]] || die "missing backup hook ${BACKUP_HOOK}"
  if command -v timeout >/dev/null 2>&1; then
    timeout --signal=TERM --kill-after=30s "${RUNTIME_BOUND_SEC}" \
      /bin/bash "${BACKUP_HOOK}"
  else
    /bin/bash "${BACKUP_HOOK}"
  fi
}

execute_job() {
  case "${JOB}" in
    tick) run_compose_tick ;;
    backup) run_backup ;;
    *) die "unknown job: ${JOB}" ;;
  esac
}

main() {
  require_compose
  log "starting job=${JOB} force_fail=${FORCE_FAIL} lock=${LOCK_FILE} db=${JOBS_DATABASE_URL}"

  # Non-blocking exclusive lock: concurrent writers are rejected.
  exec 9>"${LOCK_FILE}"
  if ! flock -n 9; then
    log "overlap rejected: another writer holds ${LOCK_FILE}"
    ping_heartbeat fail
    exit 75
  fi

  ping_heartbeat start

  if [[ "${FORCE_FAIL}" -eq 1 ]]; then
    log "forced failure for verification"
    ping_heartbeat fail
    exit 1
  fi

  local status=0
  set +e
  execute_job 2>&1 | redact | tee -a "${LOG_DIR}/scheduler.log"
  status=${PIPESTATUS[0]}
  set -e

  if [[ "${status}" -eq 0 ]]; then
    ping_heartbeat success
    log "job=${JOB} success"
    exit 0
  fi

  ping_heartbeat fail
  log "job=${JOB} failed exit=${status}"
  exit 1
}

main
