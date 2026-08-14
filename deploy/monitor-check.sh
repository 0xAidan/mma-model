#!/usr/bin/env bash
# DWCS-504 host monitor checks (read-only; does not take the writer flock).
# Evaluates disk, backup age, published health.json signals, TLS expiry,
# repeated container failures, and dashboard HTTPS reachability (loopback SNI).
# Results are written to /var/lib/mma-model/monitor-status.json (secrets redacted).
# Optionally pings MMA_HC_MONITOR_URL (success) or /fail when any alert fires.
#
# Usage:
#   deploy/monitor-check.sh
#   deploy/monitor-check.sh --json
#
# Exit 0 when rollup is green/yellow; exit 1 when rollup is red.

set -euo pipefail

JSON_ONLY=0
[[ "${1:-}" == "--json" ]] && JSON_ONLY=1

MONITORING_ENV="${MMA_MONITORING_ENV:-/etc/mma-model/monitoring.env}"
PUBLIC_ROOT="${MMA_PUBLIC_DIR_HOST:-/srv/mma/public}"
DATA_ROOT="${MMA_DATA_DIR_HOST:-/srv/mma/data}"
STATE_DIR="${MMA_MONITOR_STATE_DIR:-/var/lib/mma-model}"
STAMP_FILE="${MMA_BACKUP_STAMP:-${DATA_ROOT}/backup.last_ok}"
DISK_PATH="${MMA_DISK_PATH:-/srv/mma}"
DISK_ALERT_PCT="${MMA_DISK_ALERT_PCT:-80}"
BACKUP_MAX_AGE_HOURS="${MMA_BACKUP_MAX_AGE_HOURS:-26}"
TLS_HOST="${MMA_TLS_HOST:-mma.shermandavison.com}"
DASHBOARD_HOST="${MMA_DASHBOARD_HOST:-mma.shermandavison.com}"
HEALTH_JSON="${MMA_HEALTH_JSON:-${PUBLIC_ROOT}/live/health.json}"
CONTAINER_FAIL_WINDOW_HOURS="${MMA_CONTAINER_FAIL_WINDOW_HOURS:-6}"
CONTAINER_FAIL_THRESHOLD="${MMA_CONTAINER_FAIL_THRESHOLD:-3}"

# shellcheck disable=SC1090
if [[ -f "${MONITORING_ENV}" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${MONITORING_ENV}"
  set +a
fi

mkdir -p "${STATE_DIR}"
chmod 755 "${STATE_DIR}" 2>/dev/null || true

NOW_EPOCH="$(date -u +%s)"
NOW_ISO="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
ALERTS=()
WARNINGS=()

add_alert() { ALERTS+=("$1"); }
add_warn() { WARNINGS+=("$1"); }

redact() {
  sed -E \
    -e 's#(hc-ping\.com/)[A-Za-z0-9_-]+#\1[REDACTED]#g' \
    -e 's#(healthchecks\.io/ping/)[A-Za-z0-9_-]+#\1[REDACTED]#g' \
    -e 's#([Aa][Pp][Ii][_-]?[Kk][Ee][Yy][[:space:]]*[=:][[:space:]]*)[^[:space:]]+#\1[REDACTED]#g' \
    -e 's#([Pp][Aa][Ss][Ss][Ww][Oo][Rr][Dd][[:space:]]*[=:][[:space:]]*)[^[:space:]]+#\1[REDACTED]#g' \
    -e 's#://([^:/@]+):([^@/]+)@#://\1:[REDACTED]@#g'
}

check_disk() {
  local pct
  pct="$(df -P "${DISK_PATH}" 2>/dev/null | awk 'NR==2 {gsub(/%/,"",$5); print $5}')"
  if [[ -z "${pct}" ]]; then
    add_warn "disk_usage_unknown"
    return
  fi
  if (( pct >= DISK_ALERT_PCT )); then
    add_alert "disk_usage_${pct}_gte_${DISK_ALERT_PCT}"
  fi
  DISK_PCT="${pct}"
}

check_backup_age() {
  if [[ ! -f "${STAMP_FILE}" ]]; then
    add_warn "backup_stamp_missing"
    BACKUP_AGE_HOURS="missing"
    return
  fi
  local stamp_epoch age_h
  stamp_epoch="$(date -u -d "$(tr -d '[:space:]' < "${STAMP_FILE}")" +%s 2>/dev/null || true)"
  if [[ -z "${stamp_epoch}" ]]; then
    # macOS/BSD date fallback (local tests); production host is GNU date.
    stamp_epoch="$(stat -c %Y "${STAMP_FILE}" 2>/dev/null || stat -f %m "${STAMP_FILE}" 2>/dev/null || echo "")"
  fi
  if [[ -z "${stamp_epoch}" ]]; then
    add_warn "backup_stamp_unreadable"
    BACKUP_AGE_HOURS="unknown"
    return
  fi
  age_h=$(( (NOW_EPOCH - stamp_epoch) / 3600 ))
  BACKUP_AGE_HOURS="${age_h}"
  if (( age_h > BACKUP_MAX_AGE_HOURS )); then
    add_alert "backup_age_${age_h}h_gt_${BACKUP_MAX_AGE_HOURS}h"
  fi
}

check_health_json() {
  if [[ ! -f "${HEALTH_JSON}" ]]; then
    add_warn "health_json_missing"
    return
  fi
  # Prefer python3 for stable JSON reads; fall back to grep heuristics.
  if command -v python3 >/dev/null 2>&1; then
    local eval_out
    eval_out="$(python3 - "${HEALTH_JSON}" <<'PY'
import json, sys
path = sys.argv[1]
try:
    data = json.load(open(path, encoding="utf-8"))
except Exception as exc:
    print(f"parse_error:{exc}")
    sys.exit(0)
comps = {c.get("name"): c for c in data.get("components") or [] if isinstance(c, dict)}
signals = {
    "data_freshness": ("data", "freshness", "staleness", "sources", "publish"),
    "odds_freshness": ("odds",),
    "model_age": ("model",),
    "grading": ("grading", "grade"),
    "quota": ("quota",),
    "identity": ("identity",),
}
for label, names in signals.items():
    hit = None
    for n in names:
        if n in comps:
            hit = comps[n]
            break
    if hit is None:
        print(f"warn:{label}_component_missing")
        continue
    status = str(hit.get("status") or "").lower()
    if status in {"failed", "blocked"}:
        print(f"alert:{label}_{status}")
    elif status in {"stale", "missing"}:
        # missing/stale for production-critical components escalate.
        if label in {"identity", "model_age", "grading", "data_freshness"} and status == "missing":
            print(f"warn:{label}_{status}")
        elif status == "stale":
            print(f"warn:{label}_stale")
        else:
            print(f"warn:{label}_{status}")
    elif status == "healthy":
        print(f"ok:{label}")
    else:
        print(f"warn:{label}_{status or 'unknown'}")
PY
)"
    while IFS= read -r line; do
      [[ -z "${line}" ]] && continue
      case "${line}" in
        alert:*) add_alert "${line#alert:}" ;;
        warn:*) add_warn "${line#warn:}" ;;
        ok:*) ;;
        parse_error:*) add_warn "health_json_parse_error" ;;
      esac
    done <<< "${eval_out}"
  else
    add_warn "python3_missing_for_health_json"
  fi
}

check_tls() {
  local enddate epoch_end days
  enddate="$(echo | openssl s_client -servername "${TLS_HOST}" \
    -connect "127.0.0.1:443" 2>/dev/null | openssl x509 -noout -enddate 2>/dev/null \
    | cut -d= -f2 || true)"
  if [[ -z "${enddate}" ]]; then
    # Public hostname fallback (may fail on hairpin).
    enddate="$(echo | openssl s_client -servername "${TLS_HOST}" \
      -connect "${TLS_HOST}:443" 2>/dev/null | openssl x509 -noout -enddate 2>/dev/null \
      | cut -d= -f2 || true)"
  fi
  if [[ -z "${enddate}" ]]; then
    add_warn "tls_enddate_unreadable"
    TLS_DAYS_LEFT="unknown"
    return
  fi
  epoch_end="$(date -u -d "${enddate}" +%s 2>/dev/null || true)"
  if [[ -z "${epoch_end}" ]]; then
    add_warn "tls_enddate_parse_failed"
    TLS_DAYS_LEFT="unknown"
    return
  fi
  days=$(( (epoch_end - NOW_EPOCH) / 86400 ))
  TLS_DAYS_LEFT="${days}"
  if (( days < 0 )); then
    add_alert "tls_certificate_expired"
  elif (( days < 14 )); then
    add_warn "tls_certificate_expires_in_${days}d"
  fi
}

check_dashboard_https() {
  local code
  code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 \
    --resolve "${DASHBOARD_HOST}:443:127.0.0.1" \
    "https://${DASHBOARD_HOST}/" 2>/dev/null || echo "000")"
  DASHBOARD_HTTP_CODE="${code}"
  # Unauthenticated must be 401 (basicauth). 000/5xx/404 are alerts.
  if [[ "${code}" == "401" || "${code}" == "200" ]]; then
    return
  fi
  add_alert "dashboard_https_${code}"
}

check_container_failures() {
  local count
  count="$(journalctl -u mma-scheduler.service --since "${CONTAINER_FAIL_WINDOW_HOURS} hours ago" \
    -o cat 2>/dev/null | grep -c 'job=tick failed' || true)"
  CONTAINER_FAIL_COUNT="${count:-0}"
  if (( CONTAINER_FAIL_COUNT >= CONTAINER_FAIL_THRESHOLD )); then
    add_alert "repeated_scheduler_failures_${CONTAINER_FAIL_COUNT}"
  fi
}

ping_monitor() {
  local base="${MMA_HC_MONITOR_URL:-}"
  local kind="$1"
  if [[ -z "${base}" || "${base}" == *PLACEHOLDER* ]]; then
    return 0
  fi
  local url="${base}"
  [[ "${kind}" == "fail" ]] && url="${base%/}/fail"
  curl -fsS -m 10 -o /dev/null "${url}" 2>/dev/null || true
}

DISK_PCT="unknown"
BACKUP_AGE_HOURS="unknown"
TLS_DAYS_LEFT="unknown"
DASHBOARD_HTTP_CODE="unknown"
CONTAINER_FAIL_COUNT="0"

check_disk
check_backup_age
check_health_json
check_tls
check_dashboard_https
check_container_failures

ROLLUP="green"
if ((${#ALERTS[@]} > 0)); then
  ROLLUP="red"
elif ((${#WARNINGS[@]} > 0)); then
  ROLLUP="yellow"
fi

# Build JSON without requiring jq.
alerts_json="[]"
warns_json="[]"
if ((${#ALERTS[@]} > 0)); then
  alerts_json="$(printf '"%s",' "${ALERTS[@]}" | sed 's/,$//')"
  alerts_json="[${alerts_json}]"
fi
if ((${#WARNINGS[@]} > 0)); then
  warns_json="$(printf '"%s",' "${WARNINGS[@]}" | sed 's/,$//')"
  warns_json="[${warns_json}]"
fi

STATUS_JSON="$(cat <<EOF
{
  "as_of": "${NOW_ISO}",
  "rollup": "${ROLLUP}",
  "ticket": "DWCS-504",
  "checks": {
    "disk_usage_pct": "${DISK_PCT}",
    "backup_age_hours": "${BACKUP_AGE_HOURS}",
    "tls_days_left": "${TLS_DAYS_LEFT}",
    "dashboard_http_code": "${DASHBOARD_HTTP_CODE}",
    "scheduler_failures_${CONTAINER_FAIL_WINDOW_HOURS}h": "${CONTAINER_FAIL_COUNT}"
  },
  "alerts": ${alerts_json},
  "warnings": ${warns_json}
}
EOF
)"

printf '%s\n' "${STATUS_JSON}" > "${STATE_DIR}/monitor-status.json.tmp"
mv "${STATE_DIR}/monitor-status.json.tmp" "${STATE_DIR}/monitor-status.json"
chmod 644 "${STATE_DIR}/monitor-status.json" 2>/dev/null || true

if [[ "${JSON_ONLY}" -eq 1 ]]; then
  printf '%s\n' "${STATUS_JSON}"
else
  printf '%s\n' "${STATUS_JSON}" | redact
fi

if [[ "${ROLLUP}" == "red" ]]; then
  ping_monitor fail
  exit 1
fi
ping_monitor success
exit 0
