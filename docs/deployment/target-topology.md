# DWCS-004 target topology

Ticket: **DWCS-004**. This describes the **intended** production shape for later
deploy tickets. Nothing here was applied to the live host.

Companion inventory: [`current-state.md`](./current-state.md).  
Safe snippets: [`../../deploy/examples/`](../../deploy/examples/) (**EXAMPLE ONLY —
NOT INSTALLED**).

---

## Root-site invariants (must never regress)

1. `https://shermandavison.com/` keeps serving the existing static tree from
   `/srv/shermandavison` via the **existing host Caddy** process.
2. No second reverse proxy (no nginx/Traefik/HAProxy/Envoy alongside Caddy).
3. No MMA container publishes ports; no public SQLite/DB port.
4. MMA uses subdomain `mma.shermandavison.com` only (path-prefix on the root site
   is rejected by architecture decision §12).
5. Before/after any MMA change, capture a fingerprint-safe root check:
   HTTP status + body sha256 + etag/server headers (not full HTML archives).
6. Target topology docs and examples must not embed live public addresses,
   usernames, or credential hashes.

---

## Host and process model

```text
Internet
   |  public HTTP/HTTPS only on existing Caddy (:80/:443)
   v
[ existing host Caddy ]
   |-- shermandavison.com      -> file_server /srv/shermandavison
   |-- golf.shermandavison.com -> (existing unrelated app; do not disturb)
   |-- mma.shermandavison.com  -> file_server /srv/mma/public + basic_auth
   |
[ docker compose run --rm worker ]  (no published ports)
   |-- mounts /srv/mma/data (SQLite WAL, one writer)
   |-- mounts /srv/mma/public (versioned dashboard JSON + static assets)
   |-- reads /etc/mma-model/mma.env (0600)
   |
[ systemd timers + flock ]
   |-- mma-worker.timer  every 5 minutes Persistent=true
   |-- mma-backup.timer  nightly
```

---

## Caddy reuse

| Item | Target |
|------|--------|
| Process | Keep the running host `caddy.service` |
| Config | Add **one** site block to `/etc/caddy/Caddyfile` (see example snippet) |
| Static root | `/srv/mma/public` |
| Auth | `basic_auth` with a strong **hashed** password (never plaintext in git) |
| TLS | Automatic HTTPS on the subdomain after DNS exists |
| Reload | `caddy validate` then graceful reload only in a later ticket |

---

## Filesystem ownership (proposed)

| Path | Owner | Mode | Notes |
|------|-------|------|-------|
| `/srv/mma` | root:root | `755` | create in deploy ticket |
| `/srv/mma/public` | root:caddy (or ACL readable by uid 999) | `755`/`644` | Caddy read-only |
| `/srv/mma/data` | root:root or worker uid | `750` | SQLite + artifacts |
| `/etc/mma-model` | root:root | `755` | |
| `/etc/mma-model/mma.env` | root:root | **`0600`** | secrets; never world-readable |
| compose file | `/srv/mma/docker-compose.yml` | `644` | digest-pinned image |

DWCS-004 did **not** create these directories.

---

## Image digest and rollback

1. CI builds an immutable GHCR image and records its **sha256 digest**.
2. Production compose pins `image: …@sha256:<digest>` (never mutable `:latest` alone).
3. On deploy, retain the **previous** digest.
4. Rollback = retarget compose to the previous digest and re-publish static JSON if
   needed; leave last-known-good `/srv/mma/public` if publish fails.
5. Failed train/publish must not delete incumbent artifacts (plan failure behavior).

---

## Secrets

- Path: `/etc/mma-model/mma.env`
- Mode: `0600`, root-owned
- Contents: provider API keys and runtime settings only
- Never: sportsbook login, auto-betting credentials, browser-bundled secrets,
  or plaintext basic_auth passwords in git

---

## Network / exposure

| Surface | Policy |
|---------|--------|
| Public | Existing Caddy HTTP/HTTPS only |
| SSH | Keep current admin access model; do not widen in MMA tickets |
| Worker / SQLite | Local mounts only; **no** `ports:` in compose |
| Canaries | Localhost high ports only (127.0.0.1); never all-interfaces binds |

---

## Backups and monitoring (target)

| Item | Target |
|------|--------|
| Snapshot | SQLite backup API + `PRAGMA integrity_check` |
| Off-host | restic (or equivalent) to a second failure domain |
| Retention | 7 daily / 4 weekly / 6 monthly (plan §8) |
| Heartbeats | Job start/success/failure pings (e.g. Healthchecks.io) |
| Alerts | Missed jobs, stale dashboard, disk >80%, backup age >26h |

Host today: no `restic`; unrelated golf backup timers exist — MMA should add
**separate** `mma-*` units later, not overload unrelated timers.

---

## Explicit non-goals for DWCS-004

- No DNS changes
- No Caddyfile edits on the server
- No systemd install/enable
- No Docker install
- No persistent `/srv/mma` creation
- No firewall/UFW activation changes

Those belong to later Phase 5 deploy tickets using this inventory.
