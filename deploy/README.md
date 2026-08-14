# Packaging and deploy notes (DWCS-502 / DWCS-503 / DWCS-504)

This directory holds the **production-shaped** Compose file, digest pin, Caddy
snippet, host install/rollback helpers, and production systemd timers. Scripts
mutate a live host only when an operator runs them deliberately (see
`docs/runbooks/deploy.md` and `docs/runbooks/monitoring.md`).

| Path | Role |
|------|------|
| `compose.yaml` | Single `worker` service: digest-pinned image, no ports/expose/host-net, read-only + tmpfs, non-root, capped logs, data/public mounts, env_file secrets |
| `compose.ci.yaml` + `ci-mma.env` | CI/local `docker compose config` overlay; uses `env_file: !override` so runners need no `/etc/mma-model/mma.env` |
| `image-digest.txt` | `current` + `previous` digests for rollback |
| `Caddyfile.mma` | Production site block for existing host Caddy **2.6.2** (`basicauth`, CSP, cache rules) |
| `install.sh` / `rollback.sh` | Host path/image/Caddy helpers; `--apply-scheduler` installs DWCS-504 timers |
| `systemd/` | **Production** scheduler + backup units (DWCS-504) |
| `run-job.sh` / `backup-hook.sh` / `backup.sh` / `restore.sh` / `monitor-check.sh` | Flock runner, SQLite online + restic backup, empty-target restore, host monitors |
| `logrotate/mma-model` | File log rotation for `/var/log/mma-model` |
| `examples/` | **EXAMPLE ONLY — NOT INSTALLED** (DWCS-004); keep for topology docs/tests |

## Image

- Built from the repo-root `Dockerfile` (Node web build → Python 3.11 worker).
- Published to `ghcr.io/0xaidan/mma-model` **by digest only after merge** (see `.github/workflows/release.yml`).
  OCI requires a lowercase repository name.
- PRs build the image but **never push** and **never deploy**.
- Current production pin:
  `ghcr.io/0xaidan/mma-model@sha256:99d0aeb4c3af1ad4d733a793ac4e0407fed0fcf84d3a8c1f3a4f0fc6b943a5ae`
- `pull_policy: never` → `docker pull` that digest on the host before compose run.

## Public root coexistence

`/srv/mma/public` (container `/public`) holds web assets + `releases/` + `current` +
an atomic `live/` directory with the SPA-facing dashboard JSON. Use:

```bash
mma-model public sync-assets --from /opt/mma/web --to /public
```

Successful `mma-model publish --output /public ...` promotes release JSON into
`live/` via `live.candidate/` → `live/` directory swap. If promote fails, prior
`live/` stays complete; `releases/` + `current` may already point at the new
validated release (rollback source). The dashboard loads `./live/*.json` first,
then falls back to `./` for local Vite fixtures.

## DWCS-503 subdomain

- Serve `mma.shermandavison.com` from `/srv/mma/public` via the **existing** host Caddy.
- Runbook: `docs/runbooks/deploy.md`
- Redacted evidence: `docs/deployment/dwcs-503-evidence.md`

## DWCS-504 scheduler + monitoring

- Production units: `deploy/systemd/` (not `examples/systemd/`).
- Install: `sudo ./deploy/install.sh --apply-scheduler`
- Runbook: `docs/runbooks/monitoring.md`
- Evidence: `docs/deployment/dwcs-504-evidence.md`
- Backup / restore: `docs/runbooks/backup-restore.md` (DWCS-505)
