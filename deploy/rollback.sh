#!/usr/bin/env bash
# DWCS-503 rollback helper for the private MMA dashboard subdomain.
# Restores the pre-merge Caddyfile backup and reloads Caddy.
# Does not delete /srv/mma by default (leaves last-known-good public tree).
#
# Usage (as root):
#   ./deploy/rollback.sh              # restore latest dwcs503 Caddy backup + reload
#   ./deploy/rollback.sh --list
#   ./deploy/rollback.sh --public-release <release-id>
#   ./deploy/rollback.sh --image-previous   # print pin revert instructions

set -euo pipefail

CADDYFILE="/etc/caddy/Caddyfile"
PUBLIC_ROOT="/srv/mma/public"
BACKUP_GLOB="/etc/caddy/Caddyfile.bak-dwcs503-*"

die() { printf '[rollback] ERROR: %s\n' "$*" >&2; exit 1; }
log() { printf '[rollback] %s\n' "$*"; }

require_root() {
  [[ "${EUID}" -eq 0 ]] || die "run as root"
}

list_backups() {
  # Prefer filename timestamp (cp -a preserves source mtime, so ls -t is unreliable).
  # shellcheck disable=SC2086
  ls -1 ${BACKUP_GLOB} 2>/dev/null | sort -r || true
}

restore_caddy() {
  require_root
  local bak
  bak="$(list_backups | head -1 || true)"
  [[ -n "${bak}" ]] || die "no ${BACKUP_GLOB} found"
  cp -a "${bak}" "${CADDYFILE}"
  caddy validate --config "${CADDYFILE}"
  systemctl reload caddy
  log "restored ${bak} -> ${CADDYFILE} and reloaded caddy"
  log "DNS: remove the mma.shermandavison.com A record at the registrar if exposing should stop"
  log "Paths left intact: /srv/mma and /etc/mma-model (delete manually only if intentional)"
}

restore_public_release() {
  require_root
  local release_id="$1"
  [[ -n "${release_id}" ]] || die "release id required"
  local src="${PUBLIC_ROOT}/releases/${release_id}"
  [[ -d "${src}" ]] || die "missing release directory ${src}"
  local live="${PUBLIC_ROOT}/live"
  local candidate="${PUBLIC_ROOT}/live.candidate"
  rm -rf "${candidate}"
  mkdir -p "${candidate}"
  local required=(
    release.json manifest.json current-event.json matchups.json
    performance.json history.json health.json
  )
  local f
  for f in "${required[@]}"; do
    [[ -f "${src}/${f}" ]] || die "release ${release_id} missing ${f}"
    cp -f "${src}/${f}" "${candidate}/${f}"
  done
  # Atomic directory swap: prior live/ remains intact until replace succeeds.
  if [[ -d "${live}" ]]; then
    local old="${PUBLIC_ROOT}/live.rollback-old-$$"
    mv "${live}" "${old}"
    if mv "${candidate}" "${live}"; then
      rm -rf "${old}"
    else
      mv "${old}" "${live}"
      die "failed to swap live/; prior live/ restored"
    fi
  else
    mv "${candidate}" "${live}"
  fi
  printf '%s\n' "${release_id}" > "${PUBLIC_ROOT}/current"
  log "pointed live/ + current at release ${release_id} (no database rollback)"
}

print_image_previous_help() {
  cat <<'EOF'
Image pin rollback (repo + host copy of compose):

1. Read deploy/image-digest.txt
2. Set compose image to image_repo@previous (if previous is non-empty)
3. On the host: docker pull <previous digest> && docker compose -f deploy/compose.yaml run --rm worker ...
4. Re-sync assets if the previous image web tree differs
5. Public JSON rollback is independent: use --public-release <id>

If previous= is empty (first production pin), image rollback means rebuilding
or re-pinning a known-good digest from GHCR — do not invent a fake previous.
EOF
}

case "${1:-}" in
  "" )
    restore_caddy
    ;;
  --list )
    list_backups
    ;;
  --public-release )
    restore_public_release "${2:-}"
    ;;
  --image-previous )
    print_image_previous_help
    ;;
  -h|--help )
    sed -n '1,20p' "$0"
    ;;
  * )
    die "unknown argument: $1"
    ;;
esac
