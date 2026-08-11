"""Alembic migration environment for mma-model."""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, event, pool

from mma_model.config import get_settings
from mma_model.db.models import Base
from mma_model.db.session import sqlite_connect_pragmas

# Ensure canonical tables are registered on Base.metadata.
import mma_model.db.tables.core  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    """Resolve DB URL with disposable-first defaults.

    Priority:
    1. Non-placeholder ``sqlalchemy.url`` from Alembic Config (tests / ``init_db``)
    2. ``MMA_DATABASE_URL`` (operator override for stock alembic.ini)
    3. Placeholder disposable URL from alembic.ini
    4. Application settings
    """
    placeholder = "sqlite:///data/mma_alembic_disposable.db"
    ini_url = config.get_main_option("sqlalchemy.url") or ""
    env_url = os.environ.get("MMA_DATABASE_URL")
    if ini_url and ini_url != placeholder:
        return ini_url
    if env_url:
        return env_url
    if ini_url:
        return ini_url
    return get_settings().mma_database_url


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
