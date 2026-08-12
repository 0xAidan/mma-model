"""Alembic migration environment for mma-model."""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, event, pool

from mma_model.db.models import Base
from mma_model.db.session import sqlite_connect_pragmas

# Ensure canonical / provenance tables are registered on Base.metadata.
import mma_model.db.tables.core  # noqa: F401
import mma_model.db.tables.history  # noqa: F401
import mma_model.db.tables.identity  # noqa: F401
import mma_model.db.tables.provenance  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Sentinel in alembic.ini — never a real database target.
URL_SENTINEL = "REQUIRED_EXPLICIT_DATABASE_URL"


def _database_url() -> str:
    """Resolve DB URL only from an explicit operator/test selection.

    Priority:
    1. Non-sentinel ``sqlalchemy.url`` on Alembic Config (tests / ``init_db``)
    2. ``MMA_DATABASE_URL``
    3. Fail closed — never fall through to a misleading disposable or live default
    """
    ini_url = (config.get_main_option("sqlalchemy.url") or "").strip()
    env_url = (os.environ.get("MMA_DATABASE_URL") or "").strip()

    if ini_url and ini_url != URL_SENTINEL:
        return ini_url
    if env_url:
        return env_url
    raise RuntimeError(
        "No explicit Alembic database URL selected. Set MMA_DATABASE_URL to a "
        "temporary sqlite path (recommended), or configure sqlalchemy.url via "
        "Alembic Config (used by tests and mma-model init-db). Refusing to guess "
        "a disposable or live database."
    )


def run_migrations_offline() -> None:
    url = _database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        future=True,
    )
    if connectable.dialect.name == "sqlite":
        event.listen(connectable, "connect", sqlite_connect_pragmas)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()

    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
