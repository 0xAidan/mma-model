# DWCS-502 multi-stage worker image.
# Never bake .env, data/, *.db, model artifacts, or licensed raw payloads.
# Runtime secrets come from /etc/mma-model/mma.env via Compose env_file.

# -----------------------------------------------------------------------------
# Stage A: Node — production web build
# -----------------------------------------------------------------------------
FROM node:20-bookworm-slim AS web-build
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# -----------------------------------------------------------------------------
# Stage B: Python 3.11 worker runtime
# -----------------------------------------------------------------------------
FROM python:3.11-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MMA_WEB_ASSETS=/opt/mma/web \
    PATH="/home/mma/.local/bin:${PATH}"

# Non-root runtime user (uid/gid stable for volume ownership docs).
RUN groupadd --gid 10001 mma \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin mma \
    && mkdir -p /opt/mma/web /app /data /public /tmp \
    && chown -R mma:mma /opt/mma /app /data /public

WORKDIR /app

# Install locked runtime dependencies only (no dev tools in image).
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Application source + package install (no editable .env / data).
COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
COPY migrations ./migrations
COPY alembic.ini ./
COPY profiles.yaml feature_flags.yaml ./
RUN pip install --no-cache-dir --no-deps . \
    && chown -R mma:mma /app

# Built dashboard assets live at a fixed path inside the image.
COPY --from=web-build --chown=mma:mma /web/dist /opt/mma/web

USER mma:mma

# No published EXPOSE of app/DB ports (Caddy on the host serves /public).
# Health does not require a public port.
HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
    CMD mma-model health --json >/dev/null || exit 1

WORKDIR /app
CMD ["mma-model", "health", "--json"]
