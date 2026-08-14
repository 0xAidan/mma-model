# Packaging and deploy notes (DWCS-502 / DWCS-503)

This directory holds the **production-shaped** Compose file, digest pin, Caddy
snippet, and host install/rollback helpers. Scripts mutate a live host only when
an operator runs them deliberately (see `docs/runbooks/deploy.md`).

| Path | Role |
|------|------|
| `compose.yaml` | Single `worker` service: digest-pinned image, no ports/expose/host-net, read-only + tmpfs, non-root, capped logs, data/public mounts, env_file secrets |
| `compose.ci.yaml` + `ci-mma.env` | CI/local `docker compose config` overlay; uses `env_file: !override` so runners need no `/etc/mma-model/mma.env` |
| `image-digest.txt` | `current` + `previous` digests for rollback |
| `Caddyfile.mma` | Production site block for existing host Caddy **2.6.2** (`basicauth`, CSP, cache rules) |
| `install.sh` / `rollback.sh` | Host path/image/Caddy helpers (no second proxy; no compose ports) |
| `examples/` | **EXAMPLE ONLY — NOT INSTALLED** (DWCS-004); keep for topology docs/tests |

## Image

- Built from the repo-root `Dockerfile` (Node web build → Python 3.11 worker).
- Published to `ghcr.io/0xaidan/mma-model` **by digest only after merge** (see `.github/workflows/release.yml`).
  OCI requires a lowercase repository name.
- PRs build the image but **never push** and **never deploy**.
- Current production pin:
  `ghcr.io/0xaidan/mma-model@sha256:5f209cfdea78fd29907656aae4618c896443464ff7d71c52a1fe756b4d51d7d6`
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
- systemd timers / restic remain DWCS-504 / DWCS-505.
