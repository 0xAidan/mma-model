# DWCS-504 deploy evidence (redacted)

Ticket: **DWCS-504** — host systemd scheduler timers, global flock, heartbeats,
log rotation, and external/host monitoring seams. No Caddy MMA site-block
changes. No restic/SQLite backup internals (DWCS-505).

This file must never include public IPs, SSH aliases, Healthchecks write UUIDs,
password hashes, plaintext passwords, or forbidden host correlators.

---

## Artifacts landed in git

| Path | Role |
|------|------|
| `deploy/systemd/mma-scheduler.{service,timer}` | Five-minute `Persistent=true` scheduler |
| `deploy/systemd/mma-backup.{service,timer}` | Nightly `Persistent=true` backup stub |
| `deploy/run-job.sh` | Global flock + Compose worker + heartbeats + redaction |
| `deploy/backup-hook.sh` | DWCS-505 stub (stamps `backup.last_ok`) |
| `deploy/monitor-check.sh` | Disk / backup age / health.json / TLS / HTTPS / failures |
| `deploy/logrotate/mma-model` | `/var/log/mma-model/*.log` rotation |
| `deploy/examples/monitoring.env.example` | Placeholder Healthchecks env (not secrets) |
| `docs/runbooks/monitoring.md` | Operator runbook |

`deploy/examples/systemd/` remains **EXAMPLE ONLY — NOT INSTALLED**.

---

## Host install

Observed-at: `2026-08-14T17:26:38Z` install; reboot verify `2026-08-14T17:37:15Z`.

| Step | Result |
|------|--------|
| `sudo ./deploy/install.sh --apply-scheduler` | **pass** — units + logrotate installed; timers enabled |
| `systemd-analyze verify` on four units | **pass** (unrelated sibling unit warnings ignored) |
| Timers enabled | **pass** — `mma-scheduler.timer`, `mma-backup.timer` enabled/active after reboot |
| Concurrent flock rejection | **pass** — first tick exit `0`, overlapping tick exit `75` (`overlap rejected`) |
| `--force-fail` path | **pass** — exit `1`; heartbeat fail attempted (skipped: placeholder URL) |
| Backup stub | **pass** — stamped `/srv/mma/data/backup.last_ok` |
| Secret redaction | **pass** — `API_KEY` / `hc-ping` / `password` → `[REDACTED]` |
| Monitor rollup (normal) | **pass** — `yellow` (fixture `health.json` components still `missing`); disk 56%; TLS ~89d; dashboard `401` |
| Stopped-dashboard alert | **pass** — forced `MMA_DASHBOARD_HOST=invalid.invalid` → rollup `red` / `dashboard_https_000000`; restored to `yellow`/`401` |
| `Persistent=true` | **pass** — both timers declare it; after intentional stop across a `:0/5` boundary the service ran on catch-up (`17:35:14Z` activation) |
| Reboot persistence | **pass** — Caddy `active`; both MMA timers `enabled` and scheduled; downtime short |
| Root-site fingerprint | **unchanged** — status `200`, sha256 `f02a0caa5f2f2f77139c5cff24dca1d44d661968bc6ae3447042b900384387d0`, etag `"tjighu98o"` (before and after reboot) |
| MMA HTTPS after reboot | **401** (basicauth challenge intact) |
| Healthchecks real ping | **blocker** — no Healthchecks.io account/URLs configured; placeholders remain in `/etc/mma-model/monitoring.env` |
| External HTTPS uptime SaaS | **blocker** — not configured; host loopback probe covers local availability |

---

## Operator blockers (exact)

1. **Healthchecks.io**: no account / ping URLs on the host. `/etc/mma-model/monitoring.env` still has `PLACEHOLDER_*` values. Heartbeats log `skipped` until the operator pastes three `hc-ping.com/<uuid>` URLs (mode `0600`). Do not commit those UUIDs.
2. **External HTTPS uptime SaaS** (UptimeRobot / Better Stack / etc.): not configured. Until then, rely on `monitor-check.sh` loopback dashboard probe (`401` expected).

---

## Leftover risks

- Pinned DWCS-503 image console script still resolves alembic poorly; `run-job.sh`
  uses `/app` + `PYTHONPATH=src` workaround until a later digest.
- CLI refuses `*data/mma.db` URLs; jobs use `sqlite:////data/dwcs.db` by default.
- Backup hook is a stamp-only stub until DWCS-505.
- Published `live/health.json` is still bootstrap fixture (`missing` components) →
  monitor stays `yellow` until pipeline health is published.
- Host UFW remains inactive (unchanged from DWCS-503); cloud firewall + no compose
  publish surface remain the exposure controls.
