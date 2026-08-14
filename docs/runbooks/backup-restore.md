# Backup and restore runbook (DWCS-505)

Recover the MMA worker database, model artifacts, dashboard releases, and
deploy definitions from an encrypted restic repository. Companion monitoring
runbook: [`monitoring.md`](monitoring.md). Deploy runbook: [`deploy.md`](deploy.md).

---

## What gets backed up

Nightly `mma-backup.timer` → `deploy/run-job.sh backup` → `deploy/backup-hook.sh`
→ `deploy/backup.sh`.

Each snapshot includes:

| Item | How |
|------|-----|
| SQLite | **Online backup API** (`sqlite3.Connection.backup`) — never `cp` of the live file |
| Integrity | `PRAGMA integrity_check` must return `ok` |
| Model artifacts | `data/artifacts` payloads + sidecar manifests when present |
| Config hashes | Digests of `config/` YAML/JSON recorded in `hashes.json` |
| Dashboard | `public/current`, `live/`, current + one prior `releases/<id>/`, assets |
| Deploy defs | Compose, systemd units, image digest pin, Caddy snippet, scripts |
| Recovery metadata | Redacted env + `recovery/metadata.json` (no restic password) |

Local staging is temporary. Encrypted bytes live in `RESTIC_REPOSITORY`.

Retention (restic forget): **7 daily / 4 weekly / 6 monthly**.

---

## Operator setup (second failure domain)

1. Install restic on the host (`apt install restic` or upstream binary).
2. Copy the example env and edit on the host only:

```bash
sudo cp /opt/mma-model/deploy/examples/restic.env.example /etc/mma-model/restic.env
sudo chmod 0600 /etc/mma-model/restic.env
sudo nano /etc/mma-model/restic.env
```

3. Point `RESTIC_REPOSITORY` at a **separate failure domain**, for example:

| Target | Example `RESTIC_REPOSITORY` |
|--------|------------------------------|
| Local proof only | `/var/lib/mma-model/restic-repo` |
| S3-compatible | `s3:s3.amazonaws.com/your-bucket/mma` |
| Backblaze B2 | `b2:your-bucket:mma` |
| Another VPS (sftp) | `sftp:user@other-host:/var/backups/mma-restic` |

4. Set `RESTIC_PASSWORD` **or** (preferred) `RESTIC_PASSWORD_FILE=/etc/mma-model/restic.password` (`0600`).
5. Add any cloud keys required by that backend to the same `0600` file (never git).
6. Sync deploy scripts: `sudo ./deploy/install.sh --apply-scheduler` (re-copies hook + units).
7. Manual proof: `sudo /opt/mma-model/deploy/run-job.sh backup`

Until `RESTIC_REPOSITORY` + password exist, the nightly job fails and
`run-job.sh` pings Healthchecks `/fail` (when configured). **Live
`/srv/mma/data` is never deleted on backup failure.**

---

## Manual commands

```bash
# Bundle only (no restic) — disposable output dir must be empty/absent
PYTHONPATH=/opt/mma-model/src python3 -m mma_model.cli backup create \
  --database-path /srv/mma/data/mma.db \
  --output /tmp/mma-backup-bundle \
  --data-dir /srv/mma/data \
  --public-dir /srv/mma/public \
  --deploy-dir /opt/mma-model/deploy \
  --env-file /etc/mma-model/mma.env \
  --repo-root /opt/mma-model

# Full nightly path (online backup + restic + stamp)
sudo /opt/mma-model/deploy/run-job.sh backup

# Empty-target restore drill (does not touch live Caddy or /srv/mma)
sudo rm -rf /tmp/mma-restore-empty
sudo mkdir /tmp/mma-restore-empty
sudo /opt/mma-model/deploy/restore.sh \
  --target /tmp/mma-restore-empty \
  --proof-public /tmp/mma-restore-public
# Measured RTO: cat /tmp/mma-restore-empty/restore.elapsed_sec

# Optional localhost proof server (loopback only)
sudo /opt/mma-model/deploy/restore.sh \
  --target /tmp/mma-restore-empty2 \
  --proof-public /tmp/mma-restore-public2 \
  --serve-port 8765
```

---

## Recovery playbooks

### Corrupted SQLite database

1. Stop writers: `sudo systemctl stop mma-scheduler.timer mma-backup.timer`
2. Move the bad file aside (do not delete until restore verified):
   `sudo mv /srv/mma/data/mma.db /srv/mma/data/mma.db.corrupt-$(date -u +%Y%m%dT%H%M%SZ)`
3. Restore to an empty dir with `deploy/restore.sh --target /tmp/mma-restore-empty`
4. Confirm `PRAGMA integrity_check` = `ok` and alembic upgrade on the disposable copy
5. Install the restored `sqlite/mma.db` to `/srv/mma/data/mma.db` (ownership uid 10001)
6. Start timers; run one `run-job.sh tick`

### Missing model artifact

1. Restore latest snapshot to an empty target
2. Copy `artifacts/` from the bundle over `/srv/mma/data/artifacts/`
3. Confirm champion digest files + sidecars load (`mma-model` / restore verify)
4. If the registry YAML is missing, restore it from the bundle or re-promote

### Failed deployment / bad public tree

1. Prefer release rollback (no DB restore):  
   `sudo ./deploy/rollback.sh --public-release <prior-id>`
2. If releases are gone, restore the bundle and copy `public/releases`, `live/`, `current`

### Lost VPS

1. Provision a replacement host; install Docker, Caddy, restic
2. Restore restic snapshot to an empty tree
3. Recreate `/srv/mma/{data,public}`, `/etc/mma-model/mma.env` (from password manager + redacted hints)
4. Re-pin compose digest; re-apply Caddy site block from `deploy/Caddyfile.mma`
5. Re-install timers via `install.sh --apply-scheduler`

### Lost environment file

1. Recreate `/etc/mma-model/mma.env` (`0600`) from the operator secret store
2. Use `recovery/env.redacted` from a backup only as a **key checklist** (values are `[REDACTED]`)
3. Recreate `/etc/mma-model/restic.env` and basicauth password file the same way

### Unavailable backup repository

1. Alerts: Healthchecks backup check goes silent / `/fail`; monitor backup age >26h
2. Keep serving last-known-good `/srv/mma/public` and local DB
3. Repair repository access or initialize a **new** restic repo and take a fresh backup
4. Do not delete local data while offsite is down

### Rollback previous image + dashboard release

1. Image: set compose + `image-digest.txt` to `previous=`, `docker pull`, re-run worker  
   (`deploy/rollback.sh --image-previous` prints steps)
2. Dashboard JSON: `sudo ./deploy/rollback.sh --public-release <id>`
3. These do **not** require a restic restore when local releases/digests remain

---

## Failure behavior

- Backup failure → non-zero exit → `run-job.sh` heartbeat `/fail`
- Live DB, artifacts, and public tree are **not** deleted
- Staging under `/var/tmp/mma-backup-staging` is removed after the run

---

## Quarterly drill checklist

1. Create production-like backup (local restic repo is OK for the drill)
2. `restic check`
3. Restore into a **completely empty** directory
4. DB integrity + alembic upgrade on a disposable copy
5. Load restored model artifacts
6. Serve restored `public/` on loopback (do not change live Caddy)
7. Confirm live `https://mma.shermandavison.com` still returns **401** unauthenticated
8. Record `restore.elapsed_sec` (RTO) in the evidence doc

---

## Out of scope

- Provider account recovery
- Phase 6 evidence packet
- Changing the root site `shermandavison.com`
