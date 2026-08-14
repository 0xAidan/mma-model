"""Database URL guards for jobs tick (DWCS-504).

Production may pass an *explicit* absolute container URL
``sqlite:////data/mma.db`` (four slashes). Relative/default live forms in
``LIVE_DB_URLS`` remain refused. Implied/default DB use is blocked separately
by requiring ``--database-url`` on the CLI.
"""

from __future__ import annotations

from mma_model.quality.constants import LIVE_DB_URLS

# Explicit absolute container path for the canonical host file /data/mma.db.
# Four slashes = absolute filesystem path inside the worker container.
ALLOWED_JOBS_TICK_ABSOLUTE_MMA_DB_URL = "sqlite:////data/mma.db"


def is_refused_jobs_tick_database_url(db_url: str) -> bool:
    """Return True when ``jobs tick`` must refuse ``db_url``.

    Allowed:
      - ``sqlite:////data/mma.db`` (explicit absolute production URL)
      - other non-live disposable URLs (e.g. ``sqlite:////tmp/.../tick.db``)

    Refused:
      - empty
      - ``LIVE_DB_URLS`` relative forms (``sqlite:///data/mma.db``,
        ``sqlite:///./data/mma.db``)
      - any other URL ending in ``data/mma.db`` that is not the allowed
        absolute production URL
    """
    raw = str(db_url or "").strip()
    if not raw:
        return True
    if raw == ALLOWED_JOBS_TICK_ABSOLUTE_MMA_DB_URL:
        return False
    if raw in LIVE_DB_URLS:
        return True
    if raw.endswith("/data/mma.db") or raw.endswith("data/mma.db"):
        return True
    return False


__all__ = [
    "ALLOWED_JOBS_TICK_ABSOLUTE_MMA_DB_URL",
    "is_refused_jobs_tick_database_url",
]
