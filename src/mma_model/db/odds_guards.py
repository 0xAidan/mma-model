"""SQLite guards for append-only odds quotes (DWCS-201)."""

from __future__ import annotations

from sqlalchemy.engine import Connectable, Connection, Engine

ODDS_QUOTES_NO_UPDATE_TRIGGER = "odds_quotes_no_update"
ODDS_QUOTES_NO_DELETE_TRIGGER = "odds_quotes_no_delete"

_TRIGGER_STATEMENTS = (
    f"""
    CREATE TRIGGER IF NOT EXISTS {ODDS_QUOTES_NO_UPDATE_TRIGGER}
    BEFORE UPDATE ON odds_quotes
    BEGIN
      SELECT RAISE(ABORT, 'odds_quotes is append-only');
    END
    """,
    f"""
    CREATE TRIGGER IF NOT EXISTS {ODDS_QUOTES_NO_DELETE_TRIGGER}
    BEFORE DELETE ON odds_quotes
    BEGIN
      SELECT RAISE(ABORT, 'odds_quotes is append-only');
    END
    """,
)
_DROP_STATEMENTS = (
    f"DROP TRIGGER IF EXISTS {ODDS_QUOTES_NO_UPDATE_TRIGGER}",
    f"DROP TRIGGER IF EXISTS {ODDS_QUOTES_NO_DELETE_TRIGGER}",
)


def _run_sqlite_statements(bind: Connectable | Engine, statements: tuple[str, ...]) -> None:
    dialect = getattr(getattr(bind, "dialect", None), "name", None)
    if dialect != "sqlite":
        return

    def _execute(connection: Connection) -> None:
        for statement in statements:
            connection.exec_driver_sql(statement.strip())

    if isinstance(bind, Engine):
        with bind.begin() as conn:
            _execute(conn)
        return
    _execute(bind)  # type: ignore[arg-type]


def install_odds_sqlite_guards(bind: Connectable | Engine) -> None:
    """Install UPDATE/DELETE abort triggers on odds_quotes when the table exists."""
    dialect = getattr(getattr(bind, "dialect", None), "name", None)
    if dialect != "sqlite":
        return

    def _table_exists(connection: Connection) -> bool:
        row = connection.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='odds_quotes'"
        ).fetchone()
        return row is not None

    if isinstance(bind, Engine):
        with bind.begin() as conn:
            if _table_exists(conn):
                for statement in _TRIGGER_STATEMENTS:
                    conn.exec_driver_sql(statement.strip())
        return
    if _table_exists(bind):  # type: ignore[arg-type]
        _run_sqlite_statements(bind, _TRIGGER_STATEMENTS)


def drop_odds_sqlite_guards(bind: Connectable | Engine) -> None:
    """Drop only DWCS-201 owned odds quote triggers."""
    _run_sqlite_statements(bind, _DROP_STATEMENTS)
