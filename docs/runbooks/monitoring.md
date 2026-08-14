# Monitoring and scheduler runbook (DWCS-504)

Operate the five-minute scheduler and nightly backup timers on the **existing**
root-site host. Do not install a second reverse proxy. Do not publish Compose
ports. Production units live in [`../../deploy/systemd/`](../../deploy/systemd/)
— **not** in `deploy/examples/systemd/` (those remain EXAMPLE ONLY).

Companion deploy runbook: [`deploy.md`](deploy.md).

---

## What gets installed

| Unit | Role |
|------|------|
| `mma-scheduler.timer` / `.service` | Every 5 minutes, `Persistent=true`; runs `deploy/run-job.sh tick` under one global `flock` |
| `mma-backup.timer` / `.service` | Nightly `03:15` UTC, `Persistent=true`; runs `deploy/run-job.sh backup` (calls the DWCS-505 stub hook) |
| `/etc/logrotate.d/mma-model` | Rotates `/var/log/mma-model/*.log` |
| `/etc/mma-model/monitoring.env` | Root-owned `0600` Healthchecks URL placeholders |

Scripts (under `/opt/mma-model/deploy/` after install):

| Script | Role |
|--------|------|
| `run-job.sh` | Global `/run/mma-writer.lock`, Compose worker invoke, start/success/fail heartbeats, secret redaction, bounded runtime |
| `backup-hook.sh` | **Stub** — stamps `/srv/mma/data/backup.last_ok`; DWCS-505 replaces with SQLite + restic |
| `monitor-check.sh` | Disk >80%, backup age >26h, published `health.json` signals, TLS days left, dashboard HTTPS, repeated scheduler failures |

---

## Install

From a checkout that includes DWCS-504 files (or after syncing `deploy/` to the host):

```bash
sudo ./deploy/install.sh --apply-scheduler
```

What this does:

1. Syncs `deploy/` (+ monitoring docs) to `/opt/mma-model`.
2. Ensures `/etc/mma-model/monitoring.env` exists (`0600`, placeholders OK).
3. Installs units + logrotate, runs `systemd-analyze verify`.
4. `systemctl enable --now mma-scheduler.timer mma-backup.timer`.

Does **not** change the live Caddy MMA site block.

---

## Heartbeats (Healthchecks.io)

Put write URLs only in `/etc/mma-model/monitoring.env` (`0600`). Never commit real
UUIDs — they are secrets.

| Variable | Purpose |
|----------|---------|
| `MMA_HC_SCHEDULER_URL` | Scheduler tick start (`/start`), success (base), fail (`/fail`) |
| `MMA_HC_BACKUP_URL` | Backup hook heartbeats |
| `MMA_HC_MONITOR_URL` | Host monitor rollup success / `/fail` |

Create three free checks at [Healthchecks.io](https://healthchecks.io/) (or
compatible), paste the ping URLs into `monitoring.env`, then:

```bash
sudo systemctl start mma-scheduler.service
# Confirm journal shows "heartbeat ... ok" (URLs themselves are redacted)
```

If accounts are unavailable, leave placeholders. Timers still run; heartbeats
log `skipped` until URLs are set. Record that as an operator blocker.

### External HTTPS uptime

Configure a free uptime monitor (UptimeRobot, Better Stack, etc.) against:

- URL: `https://mma.shermandavison.com/`
- Expect: **401** (basicauth challenge) or authenticated **200**
- Alert on timeout / 5xx / connection failure

Do not put uptime API tokens in git. TLS expiry is also checked locally by
`monitor-check.sh` (warn <14 days, alert if expired).

---

## Monitor signals

`deploy/monitor-check.sh` writes `/var/lib/mma-model/monitor-status.json` and
exits `1` when rollup is `red`.

| Signal | Source |
|--------|--------|
| Dashboard HTTPS | Loopback SNI to `mma.shermandavison.com` (expects 401/200) |
| Scheduler heartbeat | Healthchecks ping from `run-job.sh` |
| Data / odds / model / grade / quota / identity | `/srv/mma/public/live/health.json` components |
| Disk >80% | `df` on `/srv/mma` |
| Backup age >26h | `/srv/mma/data/backup.last_ok` stamp from backup hook |
| Repeated container/job failures | journal `mma-scheduler` failure lines in last 6h |
| TLS problems | `openssl` enddate via loopback SNI |

---

## Overlap lock

One global lock: `/run/mma-writer.lock` (`flock -n`). A second overlapping
`run-job.sh` exits **75** and pings `/fail`.

```bash
# Proof (expect one success, one overlap rejection):
sudo /opt/mma-model/deploy/run-job.sh tick &
sudo /opt/mma-model/deploy/run-job.sh tick
# Second should log "overlap rejected" and exit 75
```

---

## Verification checklist

```bash
# Unit validation
systemd-analyze verify \
  /etc/systemd/system/mma-scheduler.service \
  /etc/systemd/system/mma-scheduler.timer \
  /etc/systemd/system/mma-backup.service \
  /etc/systemd/system/mma-backup.timer

systemctl list-timers 'mma-*'
systemctl status mma-scheduler.timer mma-backup.timer --no-pager

# Forced failure alert path
sudo /opt/mma-model/deploy/run-job.sh --force-fail tick
# Expect exit 1 + heartbeat fail (or skipped if placeholder)

# Secret redaction (must not print live Healthchecks UUIDs or API keys)
sudo grep -E 'hc-ping|API_KEY|password' /var/log/mma-model/scheduler.log || true
# Expect only [REDACTED] or no secret material

# Monitor
sudo /opt/mma-model/deploy/monitor-check.sh --json

# Stopped-dashboard proof (controlled, then restore)
# Prefer a local proof that does not take down the root site:
#   - temporarily move /srv/mma/public/index.html aside, reload is unnecessary
#     for static 404, OR
#   - point monitor expectation at a wrong path and confirm alert, then restore.
# If you stop Caddy briefly: record root-site fingerprint before/after and keep
# downtime short. Caddy must come back. Prefer not stopping Caddy.

# Reboot persistence (optional, keep short):
# 1) Record root-site sha256/etag/status
# 2) reboot
# 3) Confirm caddy active, mma timers enabled, root fingerprint unchanged
# 4) Confirm Persistent=true catch-up if a calendar slot was missed
```

Missed-run proof without full reboot: temporarily `systemctl stop mma-scheduler.timer`,
wait past a `*:0/5` boundary, `systemctl start mma-scheduler.timer`, then confirm
the service ran (journal + `systemctl status`) because `Persistent=true`.

---

## Pinned worker invoke

`run-job.sh tick` calls the digest-pinned Compose worker under flock. The current
DWCS-503 image console script resolves alembic paths incorrectly, so the runner
executes:

```text
docker compose -f /opt/mma-model/deploy/compose.yaml run --rm --no-deps \
  --entrypoint /bin/sh worker -c \
  "cd /app && PYTHONPATH=src python -m mma_model.cli jobs tick --now <utc> \
   --database-url sqlite:////data/dwcs.db"
```

Jobs DB defaults to `sqlite:////data/dwcs.db` because the CLI refuses URLs ending
in `data/mma.db`. Override with `MMA_JOBS_DATABASE_URL` in `monitoring.env` if needed.

Compose remains unpublished (no ports / expose / host-network).

---

## Backup stub vs DWCS-505

`backup-hook.sh` only stamps `backup.last_ok` and logs. It does **not** run
restic or SQLite backup APIs. DWCS-505 replaces the hook body; keep the same
systemd timer / `run-job.sh backup` / flock / heartbeat wiring.

---

## Rollback

```bash
sudo systemctl disable --now mma-scheduler.timer mma-backup.timer
sudo rm -f /etc/systemd/system/mma-scheduler.service \
  /etc/systemd/system/mma-scheduler.timer \
  /etc/systemd/system/mma-backup.service \
  /etc/systemd/system/mma-backup.timer \
  /etc/logrotate.d/mma-model
sudo systemctl daemon-reload
```

Leave `/srv/mma`, Caddy, and `/etc/mma-model/mma.env` intact.

---

## Out of scope

- restic / SQLite backup internals → DWCS-505
- Changing root site or MMA Caddy site block
- Phase 6 evidence / go-live
