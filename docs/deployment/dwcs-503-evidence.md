# DWCS-503 deploy evidence (redacted)

Ticket: **DWCS-503** — private dashboard at `mma.shermandavison.com` via existing
host Caddy. This file records **redacted** acceptance evidence. It must never
include public IPs, SSH aliases, password hashes, plaintext passwords, cloud
firewall allowlists, or forbidden host correlators.

---

## Pre-deploy baseline (`https://shermandavison.com/`)

Observed-at: `2026-08-14T03:12:07Z` (re-verified during host work).

| Fact | Value |
|------|-------|
| HTTP status | `200` |
| Body sha256 | `f02a0caa5f2f2f77139c5cff24dca1d44d661968bc6ae3447042b900384387d0` |
| ETag | `"tjighu98o"` |
| Server | `Caddy` |
| Body size | `11976` bytes |

Caddyfile backup created on host as dated `Caddyfile.bak-dwcs503-*` (contents not
stored in git). Host rollback helper rehearsed before any DNS/Caddy merge attempt.

Post-host-work root recheck: status/sha256/etag **unchanged**.

---

## Image pin

| Field | Value |
|-------|-------|
| Image | `ghcr.io/0xaidan/mma-model@sha256:5f209cfdea78fd29907656aae4618c896443464ff7d71c52a1fe756b4d51d7d6` |
| Release run | https://github.com/0xAidan/mma-model/actions/runs/31765682285 |
| `previous=` | empty (first production pin; do not invent a fake previous) |
| Pull on host | succeeded (temporary read auth; token file removed after pull) |

---

## Host work completed (without public DNS/Caddy cutover)

| Step | Status |
|------|--------|
| Re-verify OS/Caddy 2.6.2 / paths | done |
| Caddyfile dated backup + rollback script | done |
| Root-site baseline fingerprint | done; unchanged after host work |
| Install Docker Engine + Compose plugin | done |
| Create `/srv/mma/{data,public}` + `/etc/mma-model` | done |
| `/etc/mma-model/mma.env` mode `0600` | done (`MMA_DATABASE_URL=sqlite:////data/mma.db`) |
| Basic-auth password/hash generated on host | done (plaintext only at `/etc/mma-model/dashboard.basicauth.password`) |
| Pull pinned digest + sync web assets | done |
| Seed `live/` + second release for rollback demo | done |
| `live/` / `current` rollback without DB rollback | **proved** on host |
| Compose rendered with no ports/expose/host-network | **proved** |
| Merge live Caddyfile | **not applied** (see blockers) |
| Public DNS A record | **not created** (see blockers) |

---

## Acceptance matrix

| Check | Expected | Observed (redacted) |
|-------|----------|---------------------|
| `http://mma.shermandavison.com` → HTTPS | 301/308 redirect | **blocked** — no public DNS/Caddy cutover |
| Unauthenticated HTTPS | `401` | **blocked** — same |
| Authenticated HTTPS | `200` | **blocked** — same |
| Cert SAN includes `mma.shermandavison.com` | valid | **blocked** — same |
| Root site status/sha256/etag | unchanged vs baseline | **pass** |
| MMA app/DB port externally reachable | none | **pass** (compose has no publish surface; no MMA listener) |
| Public release rollback (`live/` / `current`) | prior release restored without DB rollback | **pass** (host demo) |
| Caddy candidate config validates (2.6.2 + `basicauth`) | valid | **pass** (candidate file only; not merged) |

Basic-auth plaintext path on host (password **not** recorded here):
`/etc/mma-model/dashboard.basicauth.password`.

---

## Hard blockers for public exposure

1. **Porkbun DNS credentials / interactive login unavailable** in this environment
   (captcha + no API key). Cannot create the `mma` A record without operator action.
2. **Hetzner Cloud Firewall API unverifiable** (no `hcloud` token). Host UFW is
   inactive; a **pre-existing unrelated** all-interfaces high-port listener remains
   reachable. MMA itself publishes no app/DB ports. Per ticket hard prerequisite,
   public DNS/Caddy exposure was **stopped** until an operator confirms cloud
   firewall posture privately.
3. GHCR package required authenticated pull (token lacked documented
   `read:packages` scope in `gh auth status`, but login+pull succeeded). Prefer
   making the package public or installing a dedicated read-only token at
   `/etc/mma-model/ghcr.token` (`0600`) before the next pull.

---

## Operator cutover (after blockers clear)

```bash
# 1) Create one A record for mma.shermandavison.com (no AAAA) at Porkbun
# 2) Privately confirm cloud firewall / no unintended MMA exposure
# 3) On host:
cd /opt/mma-model   # or a fresh checkout
sudo ./deploy/install.sh --apply-caddy
# 4) Run acceptance curls from docs/runbooks/deploy.md
```

Rollback if needed:

```bash
sudo ./deploy/rollback.sh
# remove DNS A record at registrar
sudo ./deploy/rollback.sh --public-release <prior-release-id>
```

---

## Repo deliverables landed with this ticket

- `deploy/Caddyfile.mma` (Caddy 2.6.2 `basicauth`, headers, cache rules)
- `deploy/install.sh` / `deploy/rollback.sh`
- `docs/runbooks/deploy.md`
- Digest pin update in `deploy/compose.yaml` + `deploy/image-digest.txt`
- Tests for snippet/header/cache/secrets/forbidden tokens
- This redacted evidence file
