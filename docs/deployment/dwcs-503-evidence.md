# DWCS-503 deploy evidence (redacted)

Ticket: **DWCS-503** — private dashboard at `mma.shermandavison.com` via existing
host Caddy. This file records **redacted** acceptance evidence. It must never
include public IPs, SSH aliases, password hashes, plaintext passwords, cloud
firewall allowlists, or forbidden host correlators.

---

## Pre-cutover baseline (`https://shermandavison.com/`)

Observed-at: `2026-08-14T17:11:00Z` (localhost SNI **and** public hostname).

| Fact | Value |
|------|-------|
| HTTP status | `200` |
| Body sha256 | `f02a0caa5f2f2f77139c5cff24dca1d44d661968bc6ae3447042b900384387d0` |
| ETag | `"tjighu98o"` |
| Server | `Caddy` |

DNS (private check, addresses not recorded): one A each for apex / `www` /
`mma`; A equals this host’s public IPv4; AAAA count 0; TCP 80/443 reachable.

---

## Image pin

| Field | Value |
|-------|-------|
| Image | `ghcr.io/0xaidan/mma-model@sha256:5f209cfdea78fd29907656aae4618c896443464ff7d71c52a1fe756b4d51d7d6` |
| Release run | https://github.com/0xAidan/mma-model/actions/runs/31765682285 |
| `previous=` | empty (first production pin) |

---

## Successful cutover (`2026-08-14T17:11:12Z`)

| Step | Result |
|------|--------|
| `sudo ./deploy/install.sh --apply-caddy` | Caddyfile merged (`basicauth`), validate OK, graceful reload OK |
| Backup | `Caddyfile.bak-dwcs503-20260814T171112Z` |
| HTTP → HTTPS | **308** → `https://mma.shermandavison.com/` |
| Unauthenticated HTTPS | **401** |
| Authenticated HTTPS (`mma_dashboard`) | **200** (HTML dashboard) |
| Certificate | Let’s Encrypt; CN=`mma.shermandavison.com`; SAN `DNS:mma.shermandavison.com` |
| Cert validity window | notBefore `2026-08-14`; notAfter `2026-11-12` (issuer YE1) |
| Root site after | **unchanged** (same status/sha256/etag) |
| Compose publish surface | none (no ports / expose / host-network) |
| MMA DB port | not listening |
| Pre-existing unrelated `:8000` | still present (not MMA; not closed) |

Basic-auth plaintext path on host (password **not** recorded here):
`/etc/mma-model/dashboard.basicauth.password`.

---

## Acceptance matrix

| Check | Expected | Observed |
|-------|----------|----------|
| `http://mma.shermandavison.com` → HTTPS | 301/308 | **308** |
| Unauthenticated HTTPS | 401 | **401** |
| Authenticated HTTPS | 200 | **200** |
| Cert SAN includes `mma.shermandavison.com` | valid | **pass** (LE YE1) |
| Root site status/sha256/etag | unchanged | **pass** (`200` / `f02a0caa…` / `"tjighu98o"`) |
| MMA app/DB port public | none | **pass** |
| `live/` release rollback without DB | works | prior host demo; paths intact |

---

## Prior failed attempt (superseded)

An earlier cutover failed ACME because DNS briefly pointed at a different
address (connect timeout). That Caddy merge was rolled back. DNS now addresses
this host; this evidence records the successful re-cutover above.

---

## Leftover risks

- Host UFW remains inactive; rely on cloud firewall + no compose publish surface.
- Pre-existing unrelated high-port listener (`:8000`) is still reachable.
- Swap is still 0 on the root-site host (monitor memory under Docker jobs).
- GHCR package may still require authenticated pull for future digests; current
  pin is already local.
- systemd timers / backups remain DWCS-504 / DWCS-505.
