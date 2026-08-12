"""SQLite guards for append-only odds quotes and availability (DWCS-201)."""

from __future__ import annotations

from sqlalchemy.engine import Connectable, Connection, Engine

ODDS_QUOTES_NO_UPDATE_TRIGGER = "odds_quotes_no_update"
ODDS_QUOTES_NO_DELETE_TRIGGER = "odds_quotes_no_delete"
ODDS_AVAIL_NO_UPDATE_TRIGGER = "odds_availability_observations_no_update"
ODDS_AVAIL_NO_DELETE_TRIGGER = "odds_availability_observations_no_delete"

_TRIGGER_SPECS = (
    (ODDS_QUOTES_NO_UPDATE_TRIGGER, "odds_quotes", "UPDATE", "odds_quotes is append-only"),
    (ODDS_QUOTES_NO_DELETE_TRIGGER, "odds_quotes", "DELETE", "odds_quotes is append-only"),
    (
        ODDS_AVAIL_NO_UPDATE_TRIGGER,
        "odds_availability_observations",
        "UPDATE",
        "odds_availability_observations is append-only",
    ),
    (
        ODDS_AVAIL_NO_DELETE_TRIGGER,
        "odds_availability_observations",
        "DELETE",
        "odds_availability_observations is append-only",
    ),
)


def _trigger_sql(name: str, table: str, action: str, message: str) -> str:
    return f"""
    CREATE TRIGGER IF NOT EXISTS {name}
    BEFORE {action} ON {table}
    BEGIN
      SELECT RAISE(ABORT, '{message}');
    END
    """


_TRIGGER_STATEMENTS = tuple(
    _trigger_sql(name, table, action, message)
    for name, table, action, message in _TRIGGER_SPECS
)
_DROP_STATEMENTS = tuple(
    f"DROP TRIGGER IF EXISTS {name}" for name, *_rest in _TRIGGER_SPECS
)


def _table_exists(connection: Connection, table: str) -> bool:
    row = connection.exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


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
    """Install UPDATE/DELETE abort triggers when owned tables exist."""
    dialect = getattr(getattr(bind, "dialect", None), "name", None)
    if dialect != "sqlite":
        return

    def _install(connection: Connection) -> None:
        for name, table, action, message in _TRIGGER_SPECS:
            if _table_exists(connection, table):
                connection.exec_driver_sql(_trigger_sql(name, table, action, message).strip())

    if isinstance(bind, Engine):
        with bind.begin() as conn:
            _install(conn)
        return
    _install(bind)  # type: ignore[arg-type]


def drop_odds_sqlite_guards(bind: Connectable | Engine) -> None:
    """Drop only DWCS-201 owned odds quote/availability triggers."""
    _run_sqlite_statements(bind, _DROP_STATEMENTS)
