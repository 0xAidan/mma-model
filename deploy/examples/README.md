# Deploy examples (DWCS-004)

**EXAMPLE ONLY — NOT INSTALLED.**

These snippets document the intended production seam for later tickets
(DWCS-503+). They must not be copied onto a live host as part of DWCS-004.

## Invariants encoded here

- Reuse the **existing host Caddy** process. Do not add a competing reverse proxy.
- Serve the dashboard as static files from `/srv/mma/public` at `mma.shermandavison.com`.
- Worker runs via `docker compose run --rm worker ...` under `flock`. Never publish
  compose ports, never use Compose `expose`, never attach host networking, and
  never open a public app/DB listener.
- Secrets live in `/etc/mma-model/mma.env` with mode `0600` (root-owned).
- Image references are digest-pinned (`@sha256:...`) for rollback to the prior digest.
- Existing host Caddy continues to own public HTTP/HTTPS; this ticket does not
  change DNS, Caddy, systemd, or firewall on the server.

## Files

| File | Purpose |
|------|---------|
| `Caddyfile.mma.snippet` | Separate site block to merge into the existing Caddyfile later |
| `docker-compose.yml` | Immutable worker image, mounts only, no ports |
| `mma.env.example` | Placeholder env keys (never real credentials) |
| `monitoring.env.example` | Healthchecks URL placeholders (DWCS-504) |
| `restic.env.example` | restic repository/password placeholders (DWCS-505) |
| `systemd/*.service` / `*.timer` | Scheduler + backup timer shape with flock (**EXAMPLE ONLY**) |

Production DWCS-504 units live in [`../systemd/`](../systemd/), not here.

## Rollback (documented for later tickets)

1. Keep the previous image digest when deploying a new digest.
2. Point compose/image pin back to the previous digest and re-run publish if needed.
3. Leave last-known-good files under `/srv/mma/public` if publish fails.
4. Never roll back by installing a second reverse proxy.

## Validation

```bash
python deploy/validate_examples.py
pytest tests/deploy/test_deploy_examples.py -q
```
