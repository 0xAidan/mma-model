"""SQLite guards for append-only prediction / grading ledgers (DWCS-400)."""

from __future__ import annotations

from sqlalchemy.engine import Connectable, Connection, Engine

LEDGER_TABLES: tuple[str, ...] = (
    "model_runs",
    "predictions",
    "price_targets",
    "official_publications",
    "recommendation_state_events",
    "observed_prices",
    "prediction_grades",
    "recommendation_settlements",
)


def _trigger_name(table: str, action: str) -> str:
    return f"{table}_no_{action.lower()}"


def _trigger_sql(name: str, table: str, action: str) -> str:
    return f"""
    CREATE TRIGGER IF NOT EXISTS {name}
    BEFORE {action} ON {table}
    BEGIN
      SELECT RAISE(ABORT, '{table} is append-only');
    END
    """


_TRIGGER_SPECS: tuple[tuple[str, str, str], ...] = tuple(
    (_trigger_name(table, action), table, action)
    for table in LEDGER_TABLES
    for action in ("UPDATE", "DELETE")
)
_TRIGGER_STATEMENTS = tuple(
    _trigger_sql(name, table, action) for name, table, action in _TRIGGER_SPECS
)
_DROP_STATEMENTS = tuple(f"DROP TRIGGER IF EXISTS {name}" for name, *_rest in _TRIGGER_SPECS)


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


def install_grade_sqlite_guards(bind: Connectable | Engine) -> None:
    """Install UPDATE/DELETE abort triggers on DWCS-400 ledger tables."""
    dialect = getattr(getattr(bind, "dialect", None), "name", None)
    if dialect != "sqlite":
        return

    def _install(connection: Connection) -> None:
        for name, table, action in _TRIGGER_SPECS:
            if _table_exists(connection, table):
                connection.exec_driver_sql(_trigger_sql(name, table, action).strip())

    if isinstance(bind, Engine):
        with bind.begin() as conn:
            _install(conn)
        return
    _install(bind)  # type: ignore[arg-type]


def drop_grade_sqlite_guards(bind: Connectable | Engine) -> None:
    """Drop only DWCS-400 owned ledger triggers."""
    _run_sqlite_statements(bind, _DROP_STATEMENTS)


__all__ = [
    "LEDGER_TABLES",
    "drop_grade_sqlite_guards",
    "install_grade_sqlite_guards",
]
