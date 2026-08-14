# Packaging and release notes (DWCS-502)

This directory holds the **production-shaped** Compose file and digest pin used by
later deploy tickets. It does **not** install or mutate the live VPS.

| Path | Role |
|------|------|
| `compose.yaml` | Single `worker` service: digest-pinned image, no ports/expose/host-net, read-only + tmpfs, non-root, capped logs, data/public mounts, env_file secrets |
| `compose.ci.yaml` + `ci-mma.env` | CI/local `docker compose config` overlay; uses `env_file: !override` so runners need no `/etc/mma-model/mma.env` |
| `image-digest.txt` | `current` + `previous` digests for rollback |
| `examples/` | **EXAMPLE ONLY — NOT INSTALLED** (DWCS-004); keep for topology docs/tests |

## Image

- Built from the repo-root `Dockerfile` (Node web build → Python 3.11 worker).
- Published to `ghcr.io/0xaidan/mma-model` **by digest only after merge** (see `.github/workflows/release.yml`).
  OCI requires a lowercase repository name.
- PRs build the image but **never push** and **never deploy**.

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

## Out of scope here

Caddy site blocks, DNS, systemd timers, restic, and live host changes belong to
DWCS-503–505.
