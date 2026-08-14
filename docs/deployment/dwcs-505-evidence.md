# DWCS-505 backup / restore evidence (redacted)

Ticket: **DWCS-505** — SQLite online backup, encrypted restic, empty-target
restore, and disaster runbooks. No Phase 6 work. Live Caddy MMA site block
unchanged by restore drills.

This file must never include public IPs, SSH aliases, restic passwords, cloud
keys, Healthchecks write UUIDs, password hashes, or forbidden host correlators.

---

## Artifacts landed in git

| Path | Role |
|------|------|
| `src/mma_model/backup/service.py` | Online backup API + bundle assembly |
| `mma-model backup create` CLI | Local consistent bundle |
| `deploy/backup-hook.sh` | Timer entrypoint (delegates) |
| `deploy/backup.sh` | Bundle + restic backup/forget/check + stamp |
| `deploy/restore.sh` | Empty-target restore + verify + migrate check |
| `deploy/examples/restic.env.example` | EXAMPLE ONLY placeholders |
| `docs/runbooks/backup-restore.md` | Operator recovery playbooks |
| `tests/backup/` | Online backup + restic empty-target tests |

---

## Verification (local / CI shape)

| Check | Result |
|-------|--------|
| Live DB never raw-copied | **pass** — `sqlite3.Connection.backup` only |
| Bundle integrity_check | **pass** — `ok` |
| restic check | **pass** — local repo in tests / host proof |
| Empty-target restore | **pass** — restore into vacant directory |
| Alembic on restored copy | **pass** — disposable migrate-check DB |
| Artifact load | **pass** when artifacts present in fixture |
| Live dashboard auth | **pass** — unauthenticated HTTPS still **401** |
| Backup failure leaves data | **pass** — failure exits non-zero; no live delete |

### Measured recovery time (RTO)

| Metric | Value | Notes |
|--------|-------|-------|
| `restore.elapsed_sec` (local proof) | **4s** | restic restore + integrity + alembic + proof public tree |
| Wall backup+restore (local) | **12s** | includes first `restic init` |
| `restore.elapsed_sec` (host proof) | **4s** | production DB snapshot restore into empty `/tmp` |
| Live auth after drill | **401** | `mma.shermandavison.com` unauthenticated |

Scope is empty-target restore verification, not a full replacement-VPS rebuild.

---

## Host proof notes (redacted)

- Nightly unit path unchanged: `run-job.sh backup` → `backup-hook.sh` → `backup.sh`.
- Host has a **local** restic repository at `/var/lib/mma-model/restic-repo` for
  first proof (`/etc/mma-model/restic.env` mode `0600`).
- Operator must retarget `RESTIC_REPOSITORY` to a second failure domain
  (B2/S3/other VPS) — see runbook. Do not commit passwords.
- Empty-target restore drills used `/tmp/mma-restore-*` and did not rewrite live
  `/srv/mma/public` or the Caddyfile.
- Production online backup of `/srv/mma/data/mma.db` succeeded; live DB left intact.

### Scheduler tick overlay hotfix (post-PR-49)

After DWCS-505 merged, host-overlaid `cli.py` imported `mma_model.backup` at
module load while the pinned worker digest still lacked that package. Tick
failed every 5 minutes with `ModuleNotFoundError: No module named
'mma_model.backup'`.

Fix (no image repin):

- `deploy/run-job.sh` also overlays host `src/mma_model/backup/` →
  `/app/src/mma_model/backup:ro` when present (still with `db_guard.py` +
  `cli.py`; never whole-repo `tick_root`).
- `cli.py` imports backup symbols only inside the `backup create` handler.

Host apply proof (redacted):

| Check | Result |
|-------|--------|
| `run-job.sh tick` exit | **0** |
| Overlay log line | `overlay: db_guard.py + cli.py + backup/ onto /app/src/mma_model` |
| `systemctl start mma-scheduler.service` Result | **success** |
| ExecMainStatus | **0** |
| Journal after apply | `job=tick success`; no new `mma_model.backup` ModuleNotFoundError |
| Live MMA unauthenticated | **401** |
| Root site body sha256 | `f02a0caa5f2f2f77139c5cff24dca1d44d661968bc6ae3447042b900384387d0` |
| Root site etag | `"tjighu98o"` |

Caddy and `/srv/mma/public` were not rewritten for this hotfix.

---

## Leftover blockers

- Production offsite `RESTIC_REPOSITORY` still needs a second failure domain
  (current host proof uses a local repo only).
- Healthchecks backup URL may still be a placeholder (DWCS-504).
- Host Python lacks full worker deps; backup uses the stdlib service path, and
  restore migrate verification falls back to the Compose worker image.