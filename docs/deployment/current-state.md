# DWCS-004 current-state inventory

Ticket: **DWCS-004** — inventory VPS and deployment seam without changing
production.

Evidence statuses used below:

| Status | Meaning |
|--------|---------|
| `observed` | Directly measured in this ticket |
| `blocked` | Could not be completed; exact blocker recorded |
| `proposed` | Intended future state (not applied here) |
| `unknown` | Not measured; do not invent |

Redaction policy for this document: public IPv4/IPv6 addresses, usernames,
password hashes, live secrets, unrelated site configs, and sensitive firewall/SSH
allowlists are redacted. Topology facts needed for later implementation are kept.

**Production mutation check:** DNS, Caddy, systemd, firewall, and persistent host
paths were **not** created or modified by this ticket. The only remote process
started was a temporary localhost-only static canary that was fully removed.

---

## 1. Public root-site baseline (`shermandavison.com`)

Observed-at: `2026-08-11T21:57:02Z` (DNS/TLS/HTTP) and rechecked
`2026-08-11T21:57:56Z` / `2026-08-11T22:01:16Z` (before/after canary).

| Fact | Status | Evidence |
|------|--------|----------|
| DNS A record present | `observed` | `dig` → `NOERROR`, one A; AAAA none; CNAME none |
| DNS NS | `observed` | Porkbun NS set (`*.ns.porkbun.com`) |
| HTTPS GET `/` | `observed` | `curl -fsS https://shermandavison.com/` → HTTP **200** |
| TLS | `observed` | CN=`shermandavison.com`, issuer Let's Encrypt (YE1), SAN DNS:shermandavison.com; notBefore 2026-08-09; notAfter 2026-11-07 |
| Server header | `observed` | `server: Caddy` |
| Body fingerprint | `observed` | size **11976** bytes; sha256 `f02a0caa5f2f2f77139c5cff24dca1d44d661968bc6ae3447042b900384387d0`; etag `"tjighu98o"` |
| Full site content stored | n/a | **Not retained** (fingerprint only) |

### Root-site before / after canary invariant

| Checkpoint | Observed-at | HTTP | Body sha256 | ETag |
|------------|-------------|------|-------------|------|
| Before canary | `2026-08-11T21:57:56Z` | 200 | `f02a0caa…384387d0` | `"tjighu98o"` |
| After canary cleanup | `2026-08-11T22:01:16Z` | 200 | `f02a0caa…384387d0` | `"tjighu98o"` |

**Invariant:** root fingerprint **unchanged** across the temporary canary.

---

## 2. Subdomain DNS intent (`mma.shermandavison.com`)

Observed-at: `2026-08-11T21:57:02Z`.

| Fact | Status | Evidence |
|------|--------|----------|
| Current DNS | `observed` | `dig` status **NXDOMAIN** (no A/AAAA/CNAME) |
| HTTP probe | `observed` | `curl` → could not resolve host |
| Intended later record | `proposed` | Single A (or AAAA if enabled) to the **same host that serves** `shermandavison.com`, added only in a later deploy ticket after recording baseline |
| DNS changed by this ticket | `observed` | **No** |

---

## 3. Authorized SSH targets discovered locally

Local SSH config was inspected for configured hosts (aliases + connectivity only).
Private keys, usernames, and addresses are redacted.

| SSH alias | Connect (BatchMode) | Role vs root site | Status |
|-----------|---------------------|-------------------|--------|
| `golf-vps` | success | **Serves** `shermandavison.com` (DNS A equals this host) | `observed` |
| `ubuntu-8gb-hel1-1` / `hetzner-bot` | success | Separate Hetzner VM; does **not** serve root site | `observed` |

Both reachable hosts expose Hetzner `hel1-dc2` instance metadata → treated as
Hetzner Cloud VMs. Root-site inventory below is from **`golf-vps`** only.

---

## 4. Root-site host inventory (`golf-vps`)

Observed-at: `2026-08-11T22:00:23Z` (inventory) and `2026-08-11T22:01:14Z` (canary).

### 4.1 OS / resources

| Fact | Status | Value (redacted) |
|------|--------|------------------|
| OS | `observed` | Ubuntu 24.04.3 LTS (noble), Linux 6.8.x, `x86_64` |
| Machine id (prefix) | `observed` | sha256(`/etc/machine-id`) prefix `243c7c05a76c` |
| Disk `/` | `observed` | 75G total, ~54% used (~34G avail) |
| Memory | `observed` | ~7.6 GiB RAM; swap **0** |
| CPUs | `observed` | 4 |
| Provider metadata | `observed` | Hetzner region `eu-central`, AZ `hel1-dc2` |

### 4.2 Docker Engine / Compose

| Fact | Status | Evidence |
|------|--------|----------|
| `docker` CLI | `observed` | **Not installed** (`command not found`; no `docker*` packages) |
| `docker compose` | `observed` | **Unavailable** (depends on Docker) |
| Implication | `proposed` | Later tickets must install Docker Engine + Compose plugin before worker units; out of scope for DWCS-004 |

### 4.3 Caddy

| Fact | Status | Evidence |
|------|--------|----------|
| Service | `observed` | `active` + `enabled` |
| Binary | `observed` | `/usr/bin/caddy` version **2.6.2** |
| Unit | `observed` | `/usr/lib/systemd/system/caddy.service`; `caddy run --environ --config /etc/caddy/Caddyfile` |
| Config path | `observed` | `/etc/caddy/Caddyfile` (plus local `.bak.*` dated 2026-08-09) |
| Validate | `observed` | `caddy validate --config /etc/caddy/Caddyfile` → **Valid configuration** |
| Full config dumped | n/a | **Not stored** |
| `shermandavison.com` site | `observed` | Present; static `root * /srv/shermandavison` + `file_server` + `encode`; additional `/api/golf/*` reverse_proxy handle (upstream redacted) |
| `www` | `observed` | Redirects to `https://shermandavison.com{uri}` 308 |
| `mma.shermandavison.com` in Caddy | `observed` | **0** mentions |
| `basic_auth` on root site | `observed` | Count **0** in current Caddyfile (dashboard auth is proposed for subdomain only) |
| Unrelated vhosts | `observed` | Present; contents **redacted** |

### 4.4 Paths (existence only — nothing created)

| Path | Exists | Mode / owners (ids) | Status |
|------|--------|---------------------|--------|
| `/srv` | yes | `755`, uid 0 / gid 0 | `observed` |
| `/srv/shermandavison` | yes | `755`, uid 501 / gid 50 | `observed` (current root static tree) |
| `/srv/mma` | **no** | — | `observed` |
| `/srv/mma/public` | **no** | — | `observed` |
| `/srv/mma/data` | **no** | — | `observed` |
| `/etc/mma-model` | **no** | — | `observed` |
| `/etc/caddy` | yes | `755`, uid 0 / gid 0 | `observed` |
| `/etc/systemd/system` | yes | `755`, uid 0 / gid 0 | `observed` |
| `/var/lib/caddy` | yes | `750`, uid 999 / gid 988 (`caddy` account) | `observed` |

Writable as the inventory SSH principal (uid 0): `/srv`, `/etc`, `/etc/caddy`,
`/etc/systemd/system`, `/tmp` → `observed` yes. **No persistent directories were
created.**

### 4.5 Firewall / listeners

| Fact | Status | Evidence |
|------|--------|----------|
| UFW | `observed` | Installed; **Status: inactive** |
| Public listeners relevant to conflicts | `observed` | `:80` and `:443` (Caddy); `:22` (sshd); Caddy admin `:2019` on localhost-ish bind; unrelated `:8000` listener present (not for MMA) |
| App/DB ports for MMA | `observed` | None dedicated |
| Detailed allowlists / raw iptables dump | `blocked` / omitted | Intentionally not published (attack-surface minimization) |

### 4.6 Backups / monitoring (host today)

| Fact | Status | Evidence |
|------|--------|----------|
| `restic` | `observed` | Not installed |
| `rclone` | `observed` | Not installed |
| Existing timers | `observed` | Unrelated golf-* maintenance/backup timers present; **no** `mma-*` units |
| MMA backup | `proposed` | See `target-topology.md` (later tickets) |

### 4.7 Local root health via SNI

| Fact | Status | Evidence |
|------|--------|----------|
| `https://shermandavison.com/` via `127.0.0.1` SNI | `observed` | HTTP 200, size 11976, sha256 matches public fingerprint |

---

## 5. Secondary authorized host (not root site)

Alias: `ubuntu-8gb-hel1-1` / `hetzner-bot`. Observed-at: `2026-08-11T21:58:28Z`.

| Fact | Status | Notes |
|------|--------|-------|
| OS | `observed` | Ubuntu 24.04.3, x86_64, 8 CPU, ~30 GiB RAM, 75G disk ~22% used |
| Docker | `observed` | **Not installed** |
| Caddy | `observed` | active/enabled, version **2.11.2**, validates; site labels are **unrelated** domains (redacted); **0** `sherman` mentions |
| Serves `shermandavison.com`? | `observed` | **No** (DNS A ≠ this host; Host/SNI probes do not serve the root fingerprint) |
| Use for MMA? | `proposed` / caution | Plan assumes root-site host Caddy reuse. Do **not** deploy MMA here without an explicit topology change ticket. |

---

## 6. Temporary localhost-only static canary

Observed-at: `2026-08-11T22:01:14Z` on `golf-vps`.

| Step | Status | Evidence |
|------|--------|----------|
| Pre-check unused high port `18765` | `observed` | free |
| Bind | `observed` | `127.0.0.1:18765` only (Python `http.server`) |
| Public / non-loopback reachability | `observed` | connect to primary NIC:`18765` **failed** (curl exit 7) |
| HTTP body | `observed` | 200; sha256 `8480e6235e5e07c79eec56b7c173bf68bfc2f18b222e8093e61b1ca69cdc6240` |
| Cleanup | `observed` | process gone; port free; temp dir removed |
| Root site unchanged | `observed` | before/after sha256 identical (section 1) |
| Bound 80/443 or changed Caddy? | `observed` | **No** |

Command shape (illustrative; already cleaned up):

```bash
# localhost-only; never all-interfaces binds / :80 / :443
python3 - <<'PY'
import http.server, socketserver
# TCPServer(("127.0.0.1", 18765), Handler serving a temp index.html)
PY
# prove, then kill + rm tempdir + re-check public root fingerprint
```

---

## 7. Proposed ownership (not applied)

| Path | Proposed owner | Proposed mode | Status |
|------|----------------|---------------|--------|
| `/srv/mma` | `root:root` (or dedicated deploy uid) | `755` | `proposed` |
| `/srv/mma/public` | readable by Caddy uid `999` | `755` dirs / `644` files | `proposed` |
| `/srv/mma/data` | writer uid used by compose worker | `750` | `proposed` |
| `/etc/mma-model` | `root:root` | `755` | `proposed` |
| `/etc/mma-model/mma.env` | `root:root` | **`0600`** | `proposed` |
| Caddy snippet merge | edit existing `/etc/caddy/Caddyfile` only | keep backups | `proposed` |

---

## 8. Blockers / unknowns for later tickets

1. **Docker absent** on the root-site host → worker/systemd examples cannot run until Engine+Compose are installed (`blocked` for runtime; docs/examples still valid).
2. **`mma.shermandavison.com` NXDOMAIN** → DNS A record not created (intentional for this ticket).
3. **UFW inactive** → host firewall hardening is a later ops concern; do not assume cloud firewall rules without verifying in the deploy ticket (`unknown` for cloud firewall details; intentionally not dumped).
4. **Swap is zero** on root-site host → monitor memory under Docker workloads later (`observed` risk note).
5. Unrelated services already share this Caddy and `/srv` tree — MMA must use **isolated** `/srv/mma` paths and a **separate site block** only.

---

## 9. Handoff

DWCS-503+ should use:

- Host: SSH alias `golf-vps` (root-site Hetzner VM).
- Existing Caddy at `/etc/caddy/Caddyfile` (validate before reload).
- Static root `/srv/mma/public`; secrets `/etc/mma-model/mma.env` mode `0600`.
- Examples under `deploy/examples/` (**not installed**).
- Root-site invariant: public fingerprint of `https://shermandavison.com/` must remain stable across MMA changes.
