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

## Host install (filled during verification)

| Step | Result |
|------|--------|
| `sudo ./deploy/install.sh --apply-scheduler` | _pending host run_ |
| `systemd-analyze verify` on four units | _pending_ |
| Timers enabled | _pending_ |
| Concurrent flock rejection (exit 75) | _pending_ |
| `--force-fail` heartbeat path | _pending_ |
| Secret-redacted logs | _pending_ |
| `Persistent=true` missed-run proof | _pending_ |
| Root-site fingerprint (if reboot) | _pending_ |
| Healthchecks real ping | _pending / blocker if no account_ |
| External HTTPS uptime monitor | _pending / blocker if no account_ |

---

## Operator blockers (exact)

Record here if free Healthchecks / uptime accounts are unavailable at verify time:

1. Healthchecks.io account / three ping URLs not configured → placeholders remain in
   `/etc/mma-model/monitoring.env`; heartbeats log `skipped`.
2. External HTTPS uptime SaaS not configured → rely on `monitor-check.sh`
   loopback dashboard probe until an operator adds UptimeRobot (or equivalent).

---

## Leftover risks

- Pinned DWCS-503 image console script still resolves alembic poorly; `run-job.sh`
  uses `/app` + `PYTHONPATH=src` workaround until a later digest.
- CLI refuses `*data/mma.db` URLs; jobs use `sqlite:////data/dwcs.db` by default.
- Backup hook is a stamp-only stub until DWCS-505.
- Host UFW remains inactive (unchanged from DWCS-503); cloud firewall + no compose
  publish surface remain the exposure controls.
