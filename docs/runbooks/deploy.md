# Deploy runbook (DWCS-503)

Serve the private static dashboard at `mma.shermandavison.com` using the
**existing host Caddy** on the root-site host. Do not disturb
`shermandavison.com`. Do not install Traefik, Nginx, or a second Caddy.

Companion inventory: [`../deployment/current-state.md`](../deployment/current-state.md).  
Target shape: [`../deployment/target-topology.md`](../deployment/target-topology.md).  
Production snippet: [`../../deploy/Caddyfile.mma`](../../deploy/Caddyfile.mma).  
Examples remain **EXAMPLE ONLY — NOT INSTALLED**: [`../../deploy/examples/`](../../deploy/examples/).

---

## Architecture invariants

| Rule | Detail |
|------|--------|
| Proxy | Existing host Caddy owns `:80` / `:443` only |
| Auth | Hashed basic auth (`basicauth` on Caddy **2.6.2**; not `basic_auth`) |
| Static root | `/srv/mma/public` (`releases/`, `current`, `live/`, web assets) |
| Worker | Digest-pinned Compose image; **no** `ports` / `expose` / host-network |
| Secrets | `/etc/mma-model/mma.env` root-owned `0600` |
| Browser | Loads versioned JSON from `./live/` then `./`; never queries SQLite |

Pinned image (immutable):

```text
ghcr.io/0xaidan/mma-model@sha256:ba2641370382c9418968d2b416966bcf0bec44bb6cb70819d4c4f2d91b01cef7
```

`pull_policy: never` means `docker pull` the digest on the host before
`docker compose run`.

---

## Hard prerequisites (before DNS / Caddy merge)

1. Re-read Phase 0 inventory (`docs/deployment/current-state.md`).
2. Back up `/etc/caddy/Caddyfile` to a dated `Caddyfile.bak-dwcs503-*` on the host.
3. Record root-site fingerprint for `https://shermandavison.com/`:
   HTTP status, body sha256, etag, `server` header.
4. Verify disk, memory, Docker (install Engine + Compose plugin from Docker’s
   official Ubuntu repo if absent — not a second proxy), firewall, paths.
5. **Privately** verify cloud-firewall / exposure posture. Confirm no MMA
   application or database port will be public. Do not publish allowlists, IPs,
   or SSH aliases. If verification is impossible, **stop** public DNS/Caddy
   exposure and record the blocker.
6. Rehearse rollback (`deploy/rollback.sh` + DNS revert + image pin notes)
   before changing Caddy or DNS.

---

## Host paths and ownership

| Path | Owner | Mode | Notes |
|------|-------|------|-------|
| `/srv/mma` | `root:root` | `755` | Top-level |
| `/srv/mma/data` | worker uid `10001` | `750` | SQLite + artifacts |
| `/srv/mma/public` | `10001:<caddy-gid>` (readable by Caddy uid `999`) | dirs `755`/`775`, files `644` | Canonical dashboard tree |
| `/etc/mma-model` | `root:root` | `755` | Config dir |
| `/etc/mma-model/mma.env` | `root:root` | `0600` | Provider keys only |
| `/etc/mma-model/dashboard.basicauth.password` | `root:root` | `0600` | Plaintext basic-auth password (**host only**) |
| `/etc/mma-model/dashboard.basicauth.hash` | `root:root` | `0600` | bcrypt hash for Caddy |
| `/etc/mma-model/ghcr.token` | `root:root` | `0600` | Optional **read-only** GHCR pull token |

Do **not** create a second drifting publish root at `/srv/mma/releases`.
Releases live at `/srv/mma/public/releases`.

---

## DNS

- Registrar: Porkbun (from inventory).
- Create **one A record** for `mma.shermandavison.com` to the **same host** that
  serves `shermandavison.com`.
- **No AAAA** until IPv6 routing and firewalling are verified.
- Never commit DNS credentials.

---

## Install sequence

From a checkout of this repo on the host (or after copying `deploy/`):

```bash
# 1) Paths + Docker + pull + asset sync (no Caddy/DNS yet)
sudo ./deploy/install.sh

# 2) After private firewall sign-off + DNS A record exists:
sudo ./deploy/install.sh --apply-caddy
```

What `install.sh` does:

1. Records a private root-site baseline under `/var/tmp/mma-dwcs503-baseline`.
2. Installs Docker Engine + Compose plugin if missing.
3. Creates `/srv/mma/{data,public}` and `/etc/mma-model` with least privilege.
4. Ensures `mma.env` (`0600`) and generates basic-auth password/hash on the host.
5. Optionally `docker login ghcr.io` using `/etc/mma-model/ghcr.token` (read-only).
6. `docker pull` of the pinned digest (required because `pull_policy: never`).
7. `mma-model public sync-assets --from /opt/mma/web --to /public`.
8. Seeds `live/` from baked web fixtures if empty (atomic candidate swap).
9. With `--apply-caddy`: merges `deploy/Caddyfile.mma` (hash substituted),
   `caddy validate`, graceful `systemctl reload caddy`.

Manual equivalents:

```bash
docker pull ghcr.io/0xaidan/mma-model@sha256:ba2641370382c9418968d2b416966bcf0bec44bb6cb70819d4c4f2d91b01cef7
docker compose -f deploy/compose.yaml run --rm worker \
  mma-model public sync-assets --from /opt/mma/web --to /public
caddy hash-password   # store hash only on host; never commit
caddy validate --config /etc/caddy/Caddyfile
systemctl reload caddy
```

GHCR: if the package is private, place a **read-only** token in
`/etc/mma-model/ghcr.token` (`0600`) or make the package public. Do not put a
write-scoped GitHub token in `mma.env`.

---

## Acceptance checks

```bash
# HTTP -> HTTPS
curl -sSI http://mma.shermandavison.com/ | head -n1
# expect 308/301 to https

# Unauthenticated HTTPS -> 401
curl -sSI https://mma.shermandavison.com/ | head -n1

# Authenticated HTTPS -> 200
PASS=$(sudo tr -d '\n' < /etc/mma-model/dashboard.basicauth.password)
curl -fsS -u "mma_dashboard:${PASS}" -o /dev/null -w '%{http_code}\n' \
  https://mma.shermandavison.com/

# Certificate SAN includes subdomain (redact full PEM in public docs)
echo | openssl s_client -servername mma.shermandavison.com \
  -connect mma.shermandavison.com:443 2>/dev/null | openssl x509 -noout -subject -ext subjectAltName

# Root site unchanged
curl -fsS -o /tmp/root.html -D /tmp/root.hdr https://shermandavison.com/
sha256sum /tmp/root.html
# compare status/etag/sha256 to pre-deploy baseline

# No MMA publish surface
docker compose -f deploy/compose.yaml config | grep -Ei 'ports|expose|network_mode' || true
ss -lntup | grep -E 'docker|mma' || true
```

Public JSON rollback (no database rollback):

```bash
# After two releases exist under /srv/mma/public/releases/<id>/
sudo ./deploy/rollback.sh --public-release <previous-release-id>
```

---

## Rollback

| Surface | Action |
|---------|--------|
| Caddy | `sudo ./deploy/rollback.sh` (restores latest `Caddyfile.bak-dwcs503-*`, validate, reload) |
| DNS | Remove the `mma.shermandavison.com` A record at Porkbun |
| Public JSON | `sudo ./deploy/rollback.sh --public-release <id>` (atomic `live/` swap) |
| Image pin | Retarget `deploy/compose.yaml` + `deploy/image-digest.txt` to `previous=` when non-empty; `docker pull` that digest |
| Paths | `/srv/mma` and `/etc/mma-model` are left intact by default |

Host copy of rollback notes may also live at `/root/mma-dwcs503-rollback.sh`
(private; not committed).

---

## Caddy 2.6.2 notes

- Use `basicauth` (not `basic_auth`, which is Caddy 2.8+).
- Automatic HTTPS + HTTP→HTTPS redirect are provided by the site block address.
- Hashed `/assets/*` get immutable caching; `live/*` and dashboard JSON use
  `no-store` / `no-cache`.
- Always `caddy validate` before reload.

---

## Out of scope

- Changing the root site
- Auto-deploy from PRs

## Scheduler / monitoring (DWCS-504)

Production units: [`../../deploy/systemd/`](../../deploy/systemd/).  
Runbook: [`monitoring.md`](monitoring.md).  
Evidence: [`../deployment/dwcs-504-evidence.md`](../deployment/dwcs-504-evidence.md).

```bash
sudo ./deploy/install.sh --apply-scheduler
```

Examples under `deploy/examples/systemd/` stay **EXAMPLE ONLY — NOT INSTALLED**.

## Backup / restore (DWCS-505)

Runbook: [`backup-restore.md`](backup-restore.md).  
Evidence: [`../deployment/dwcs-505-evidence.md`](../deployment/dwcs-505-evidence.md).

```bash
sudo /opt/mma-model/deploy/run-job.sh backup
sudo /opt/mma-model/deploy/restore.sh --target /tmp/mma-restore-empty
```