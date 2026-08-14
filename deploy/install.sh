#!/usr/bin/env bash
# DWCS-503 host install helper for the private MMA dashboard subdomain.
# Intended for the root-site host that already serves shermandavison.com via Caddy.
# Does NOT install a second reverse proxy. Does NOT publish compose ports.
#
# Usage (as root on the host, after copying this repo tree or the deploy/ files):
#   ./deploy/install.sh [--skip-docker-install] [--skip-dns-check] [--apply-caddy]
#
# Secrets:
#   - /etc/mma-model/mma.env          (0600) runtime env for the worker
#   - /etc/mma-model/dashboard.basicauth.password  (0600) plaintext basic-auth password
#   - /etc/mma-model/ghcr.token        (0600) optional read-only GHCR pull token
# Never commit those files.

set -euo pipefail

SKIP_DOCKER_INSTALL=0
SKIP_DNS_CHECK=0
APPLY_CADDY=0

DIGEST="sha256:5f209cfdea78fd29907656aae4618c896443464ff7d71c52a1fe756b4d51d7d6"
IMAGE="ghcr.io/0xaidan/mma-model@${DIGEST}"
COMPOSE_FILE="${COMPOSE_FILE:-deploy/compose.yaml}"
CADDY_SNIPPET="${CADDY_SNIPPET:-deploy/Caddyfile.mma}"
PUBLIC_ROOT="/srv/mma/public"
DATA_ROOT="/srv/mma/data"
ETC_ROOT="/etc/mma-model"
CADDY_UID="${CADDY_UID:-999}"
WORKER_UID="${WORKER_UID:-10001}"
SUBDOMAIN="mma.shermandavison.com"
ROOT_SITE="https://shermandavison.com/"

usage() {
  cat <<'EOF'
Usage: deploy/install.sh [options]

  --skip-docker-install   Assume Docker Engine + Compose plugin already present
  --skip-dns-check        Do not require subdomain A record resolution
  --apply-caddy           Merge Caddyfile.mma into /etc/caddy/Caddyfile and reload
  -h, --help              Show this help

Prerequisites (operator must confirm privately before --apply-caddy):
  - Cloud firewall / exposure verified; no MMA app/DB port will be public
  - Rollback commands rehearsed (see docs/runbooks/deploy.md)
  - Root-site baseline fingerprint recorded
EOF
}

log() { printf '[install] %s\n' "$*"; }
die() { printf '[install] ERROR: %s\n' "$*" >&2; exit 1; }

require_root() {
  [[ "${EUID}" -eq 0 ]] || die "run as root"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-docker-install) SKIP_DOCKER_INSTALL=1; shift ;;
    --skip-dns-check) SKIP_DNS_CHECK=1; shift ;;
    --apply-caddy) APPLY_CADDY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

require_root

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

record_root_baseline() {
  local out_dir="/var/tmp/mma-dwcs503-baseline"
  mkdir -p "${out_dir}"
  local body="${out_dir}/root-body.html"
  local headers="${out_dir}/root-headers.txt"
  local meta="${out_dir}/root-meta.txt"
  curl -fsS -D "${headers}" -o "${body}" "${ROOT_SITE}" >/dev/null
  {
    echo "observed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo -n "http_code="
    awk 'BEGIN{IGNORECASE=1} /^HTTP\//{code=$2} END{print code+0}' "${headers}"
    echo -n "sha256="
    sha256sum "${body}" | awk '{print $1}'
    echo -n "etag="
    awk 'BEGIN{IGNORECASE=1} /^etag:/{print $2}' "${headers}" | tr -d '\r'
    echo -n "server="
    awk 'BEGIN{IGNORECASE=1} /^server:/{print $2}' "${headers}" | tr -d '\r'
  } > "${meta}"
  chmod 600 "${headers}" "${meta}"
  log "recorded root-site baseline under ${out_dir} (private; do not commit)"
}

install_docker_if_needed() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    log "Docker Engine + Compose plugin already present"
    return
  fi
  if [[ "${SKIP_DOCKER_INSTALL}" -eq 1 ]]; then
    die "Docker/Compose missing and --skip-docker-install was set"
  fi
  log "installing Docker Engine + Compose plugin from Docker's official Ubuntu repo"
  apt-get update -y
  apt-get install -y ca-certificates curl
  install -m 0755 -d /etc/apt/keyrings
  if [[ ! -f /etc/apt/keyrings/docker.asc ]]; then
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
  fi
  # shellcheck disable=SC1091
  . /etc/os-release
  echo \
    "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -y
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker
  docker compose version >/dev/null
}

ensure_paths() {
  log "creating persistent paths under /srv/mma and ${ETC_ROOT}"
  mkdir -p "${DATA_ROOT}" "${PUBLIC_ROOT}/releases" "${PUBLIC_ROOT}/live" "${ETC_ROOT}"
  # Canonical dashboard tree is /srv/mma/public (releases/ + current + live/ + web assets).
  # Do not create a second drifting publish root at /srv/mma/releases.
  chown root:root /srv/mma "${ETC_ROOT}"
  chmod 755 /srv/mma "${ETC_ROOT}"
  chown -R "${WORKER_UID}:${WORKER_UID}" "${DATA_ROOT}"
  chmod 750 "${DATA_ROOT}"
  # Public tree readable by Caddy uid; worker must write releases/live during publish.
  chown -R "${WORKER_UID}:${CADDY_UID}" "${PUBLIC_ROOT}"
  chmod 755 "${PUBLIC_ROOT}"
  find "${PUBLIC_ROOT}" -type d -exec chmod 755 {} \;
  find "${PUBLIC_ROOT}" -type f -exec chmod 644 {} \;
  # Group-writable for worker publishes while remaining Caddy-readable.
  chmod 775 "${PUBLIC_ROOT}" "${PUBLIC_ROOT}/releases" "${PUBLIC_ROOT}/live" || true
}

ensure_env_file() {
  local env_file="${ETC_ROOT}/mma.env"
  if [[ ! -f "${env_file}" ]]; then
    log "creating placeholder ${env_file} (0600); fill provider keys as needed"
    cat > "${env_file}" <<'EOF'
# Production worker env (DWCS-503). Root-owned mode 0600.
# No sportsbook login fields. No write-scoped GitHub tokens.
THE_ODDS_API_KEY=
SPORTSDATAIO_API_KEY=
MMA_DATA_DIR=/data
MMA_PUBLIC_DIR=/public
# Absolute SQLite path on the data volume (container is read-only elsewhere).
MMA_DATABASE_URL=sqlite:////data/mma.db
EOF
  fi
  chown root:root "${env_file}"
  chmod 0600 "${env_file}"
}

ensure_basicauth_password() {
  local pw_file="${ETC_ROOT}/dashboard.basicauth.password"
  local hash_file="${ETC_ROOT}/dashboard.basicauth.hash"
  if [[ ! -f "${pw_file}" ]]; then
    log "generating basic-auth password at ${pw_file} (0600)"
    # 24 bytes -> ~32 chars url-safe
    python3 - <<'PY' > "${pw_file}"
import secrets
print(secrets.token_urlsafe(24))
PY
  fi
  chown root:root "${pw_file}"
  chmod 0600 "${pw_file}"
  local password
  password="$(tr -d '\n' < "${pw_file}")"
  # Prefer caddy hash-password when available.
  if command -v caddy >/dev/null 2>&1; then
    caddy hash-password --plaintext "${password}" > "${hash_file}"
  else
    die "caddy binary required to hash basic-auth password"
  fi
  chown root:root "${hash_file}"
  chmod 0600 "${hash_file}"
  log "basic-auth hash written to ${hash_file}; plaintext only at ${pw_file}"
}

maybe_docker_login_ghcr() {
  local token_file="${ETC_ROOT}/ghcr.token"
  if [[ -f "${token_file}" ]]; then
    log "docker login ghcr.io using read-only token file ${token_file}"
    chmod 0600 "${token_file}"
    chown root:root "${token_file}"
    # Username may be any non-empty string for PAT auth; use github actor placeholder.
    local user="${GHCR_USERNAME:-mma-pull}"
    tr -d '\n' < "${token_file}" | docker login ghcr.io -u "${user}" --password-stdin
    return
  fi
  log "no ${token_file}; attempting anonymous pull (package must be public)"
}

pull_pinned_image() {
  log "pulling pinned image ${IMAGE}"
  docker pull "${IMAGE}"
}

compose() {
  docker compose -f "${COMPOSE_FILE}" "$@"
}

sync_assets_and_seed() {
  log "syncing baked web assets into ${PUBLIC_ROOT}"
  compose run --rm worker \
    mma-model public sync-assets --from /opt/mma/web --to /public

  if [[ ! -f "${PUBLIC_ROOT}/live/current-event.json" ]]; then
    log "writing bootstrap live/ JSON from image web fixtures (sync-assets skips JSON)"
    # Vite may bake web/public/*.json into /opt/mma/web; copy into live/ + releases/.
    # Failed/partial bootstrap must not wipe an existing complete live/ tree.
    compose run --rm --entrypoint /bin/sh worker -c '
      set -e
      mkdir -p /public/live.candidate /public/releases/bootstrap-dwcs503
      missing=0
      for f in release.json manifest.json current-event.json matchups.json performance.json history.json health.json; do
        if [ -f "/opt/mma/web/$f" ]; then
          cp -f "/opt/mma/web/$f" "/public/releases/bootstrap-dwcs503/$f"
          cp -f "/opt/mma/web/$f" "/public/live.candidate/$f"
        else
          missing=1
        fi
      done
      if [ "$missing" -ne 0 ]; then
        rm -rf /public/live.candidate
        echo "bootstrap fixtures incomplete in /opt/mma/web" >&2
        exit 1
      fi
      if [ -d /public/live ]; then
        mv /public/live /public/live.bootstrap-old
        if mv /public/live.candidate /public/live; then
          rm -rf /public/live.bootstrap-old
        else
          mv /public/live.bootstrap-old /public/live
          rm -rf /public/live.candidate
          exit 1
        fi
      else
        mv /public/live.candidate /public/live
      fi
      printf "%s\n" "bootstrap-dwcs503" > /public/current
    '
  fi

  [[ -f "${PUBLIC_ROOT}/index.html" ]] || die "index.html missing after asset sync"
  [[ -f "${PUBLIC_ROOT}/live/current-event.json" ]] || die "live/current-event.json missing after seed"
  # Re-assert ownership for Caddy readability after container writes.
  chown -R "${WORKER_UID}:${CADDY_UID}" "${PUBLIC_ROOT}"
  find "${PUBLIC_ROOT}" -type d -exec chmod 755 {} \;
  find "${PUBLIC_ROOT}" -type f -exec chmod 644 {} \;
  chmod 775 "${PUBLIC_ROOT}" "${PUBLIC_ROOT}/releases" "${PUBLIC_ROOT}/live" || true
}

render_caddy_site_block() {
  local hash
  hash="$(tr -d '[:space:]' < "${ETC_ROOT}/dashboard.basicauth.hash")"
  [[ -n "${hash}" ]] || die "missing basic-auth hash"
  [[ "${hash}" != *PLACEHOLDER* ]] || die "basic-auth hash still placeholder"
  sed "s|REDACTED_HASH_PLACEHOLDER|${hash}|g" "${CADDY_SNIPPET}"
}

apply_caddy_merge() {
  local caddyfile="/etc/caddy/Caddyfile"
  [[ -f "${caddyfile}" ]] || die "missing ${caddyfile}"
  if grep -q "mma.shermandavison.com" "${caddyfile}"; then
    log "Caddyfile already mentions mma.shermandavison.com; not auto-merging"
    log "replace the site block manually from ${CADDY_SNIPPET} if needed, then validate+reload"
    return
  fi
  local ts
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  cp -a "${caddyfile}" "${caddyfile}.bak-dwcs503-${ts}"
  log "backed up Caddyfile to ${caddyfile}.bak-dwcs503-${ts}"
  {
    echo
    echo "# --- BEGIN DWCS-503 mma.shermandavison.com (managed) ---"
    render_caddy_site_block
    echo "# --- END DWCS-503 mma.shermandavison.com ---"
  } >> "${caddyfile}"
  caddy validate --config "${caddyfile}"
  systemctl reload caddy
  log "Caddy validated and reloaded"
}

check_dns() {
  if [[ "${SKIP_DNS_CHECK}" -eq 1 ]]; then
    log "skipping DNS check"
    return
  fi
  if getent hosts "${SUBDOMAIN}" >/dev/null 2>&1 || dig +short A "${SUBDOMAIN}" | grep -q .; then
    log "DNS A record for ${SUBDOMAIN} resolves"
    if dig +short AAAA "${SUBDOMAIN}" | grep -q .; then
      die "AAAA present for ${SUBDOMAIN}; remove until IPv6 routing+firewall verified"
    fi
  else
    die "${SUBDOMAIN} does not resolve; create a single A record (no AAAA) before --apply-caddy"
  fi
}

assert_compose_no_publish() {
  if grep -E '^[[:space:]]*ports[[:space:]]*:' "${COMPOSE_FILE}" >/dev/null; then
    die "${COMPOSE_FILE} must not declare ports"
  fi
  if grep -E '^[[:space:]]*expose[[:space:]]*:' "${COMPOSE_FILE}" >/dev/null; then
    die "${COMPOSE_FILE} must not declare expose"
  fi
  if grep -Ei 'network[_-]?mode[[:space:]]*:[[:space:]]*["'\'']?host' "${COMPOSE_FILE}" >/dev/null; then
    die "${COMPOSE_FILE} must not use host networking"
  fi
}

main() {
  log "DWCS-503 install starting"
  record_root_baseline
  assert_compose_no_publish
  install_docker_if_needed
  ensure_paths
  ensure_env_file
  ensure_basicauth_password
  maybe_docker_login_ghcr
  pull_pinned_image
  sync_assets_and_seed
  if [[ "${APPLY_CADDY}" -eq 1 ]]; then
    check_dns
    apply_caddy_merge
  else
    log "paths/image/assets ready; re-run with --apply-caddy after DNS + firewall sign-off"
  fi
  log "done"
}

main "$@"
