# DWCS-503 deploy evidence (redacted)

Ticket: **DWCS-503** — private dashboard at `mma.shermandavison.com` via existing
host Caddy. This file records **redacted** acceptance evidence. It must never
include public IPs, SSH aliases, password hashes, plaintext passwords, cloud
firewall allowlists, or forbidden host correlators.

---

## Pre-cutover baseline (`https://shermandavison.com/`)

Observed-at: `2026-08-14T16:01:54Z` (localhost SNI on root-site host).

| Fact | Value |
|------|-------|
| HTTP status | `200` |
| Body sha256 | `f02a0caa5f2f2f77139c5cff24dca1d44d661968bc6ae3447042b900384387d0` |
| ETag | `"tjighu98o"` |
| Server | `Caddy` |

Hairpin curl from the VPS to its own public hostname times out; baselines use
loopback SNI (`--resolve …:127.0.0.1`). Cursor-environment public curls may also
time out and are not treated as site-down proof.

---

## Image pin

| Field | Value |
|-------|-------|
| Image | `ghcr.io/0xaidan/mma-model@sha256:5f209cfdea78fd29907656aae4618c896443464ff7d71c52a1fe756b4d51d7d6` |
| Release run | https://github.com/0xAidan/mma-model/actions/runs/31765682285 |
| `previous=` | empty (first production pin) |

---

## Cutover attempt (`2026-08-14T16:06:40Z`)

| Step | Result |
|------|--------|
| DNS: one A for `mma`; A equals root A; AAAA=0 | observed |
| `sudo ./deploy/install.sh --apply-caddy` | Caddyfile merged, `caddy validate` OK, graceful reload OK |
| HTTP `mma` → HTTPS | **308** to `https://mma.shermandavison.com/` (loopback) |
| ACME / certificate | **FAILED** (see blocker) |
| Caddy rollback | **applied** — live Caddyfile restored; `mma` mentions = 0 |
| Root site after rollback | unchanged (`200` / same sha256 / same etag) |

### Exact ACME errors (IPs redacted)

1. Let’s Encrypt `http-01`:  
   `challenge failed` / `urn:ietf:params:acme:error:connection` —  
   `Fetching http://mma.shermandavison.com/.well-known/acme-challenge/…: Timeout during connect (likely firewall problem)`
2. Let’s Encrypt `tls-alpn-01`: same connection timeout.
3. Fallback ZeroSSL issuer:  
   `failed getting EAB credentials: HTTP 422: caddy_legacy_user_removed (code 2977)`

### Private diagnosis (not publishing addresses)

- `mma` A equals `shermandavison.com` A (count 1 each; AAAA 0).
- That A address is **not** this host’s `eth0` global IPv4 and **not** this
  host’s egress IPv4 (`eth0` does equal egress).
- Therefore ACME validators reach a different address than the root-site host
  that runs Caddy / holds `/srv/mma/public`.

**Operator action required:** point the `mma` (and likely root) A record at
**this host’s actual public IPv4** (the address on `eth0` / egress), keep AAAA
absent, ensure inbound **80/443** from the internet (incl. Let’s Encrypt) to
that address, then re-run `sudo ./deploy/install.sh --apply-caddy`.

---

## Acceptance matrix

| Check | Expected | Observed |
|-------|----------|----------|
| `http://mma…` → HTTPS | 301/308 | **308** during cutover; site block **rolled back** after ACME failure |
| Unauthenticated HTTPS | 401 | **blocked** — no cert (TLS alert internal error) |
| Authenticated HTTPS | 200 | **blocked** — same |
| Cert SAN includes `mma.shermandavison.com` | valid | **fail** — ACME connect timeout |
| Root site status/sha256/etag | unchanged | **pass** before, during SNI checks, and after rollback |
| MMA app/DB port public | none | **pass** (compose no publish surface; no MMA listener) |
| `live/` release rollback without DB | works | **pass** (prior host demo; paths intact) |

Basic-auth plaintext path (password not recorded):  
`/etc/mma-model/dashboard.basicauth.password`.

---

## Host assets retained after rollback

- `/srv/mma/public` (index + `live/` + releases) intact
- `/etc/mma-model/mma.env` + basicauth files intact
- Pinned image still present locally
- Dated `Caddyfile.bak-dwcs503-*` retained for future cutover

---

## Repo follow-ups in this iteration

- `deploy/install.sh`: loopback-SNI baseline with timeouts; skip `docker pull` when
  digest already local; backup touch so rollback can order by filename time
- `deploy/rollback.sh`: list backups by filename timestamp (not `cp -a` mtime)
