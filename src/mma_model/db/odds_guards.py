"""SQLite guards for append-only odds quotes, availability, and manual prices."""

from __future__ import annotations

from sqlalchemy.engine import Connectable, Connection, Engine

ODDS_QUOTES_NO_UPDATE_TRIGGER = "odds_quotes_no_update"
ODDS_QUOTES_NO_DELETE_TRIGGER = "odds_quotes_no_delete"
ODDS_AVAIL_NO_UPDATE_TRIGGER = "odds_availability_observations_no_update"
ODDS_AVAIL_NO_DELETE_TRIGGER = "odds_availability_observations_no_delete"
ODDS_MANUAL_NO_UPDATE_TRIGGER = "odds_manual_price_observations_no_update"
ODDS_MANUAL_NO_DELETE_TRIGGER = "odds_manual_price_observations_no_delete"
ODDS_MATCH_NO_UPDATE_TRIGGER = "odds_match_observations_no_update"
ODDS_MATCH_NO_DELETE_TRIGGER = "odds_match_observations_no_delete"
ODDS_LIFECYCLE_NO_UPDATE_TRIGGER = "odds_bout_lifecycle_observations_no_update"
ODDS_LIFECYCLE_NO_DELETE_TRIGGER = "odds_bout_lifecycle_observations_no_delete"

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
    (
        ODDS_MANUAL_NO_UPDATE_TRIGGER,
        "odds_manual_price_observations",
        "UPDATE",
        "odds_manual_price_observations is append-only",
    ),
    (
        ODDS_MANUAL_NO_DELETE_TRIGGER,
        "odds_manual_price_observations",
        "DELETE",
        "odds_manual_price_observations is append-only",
    ),
    (
        ODDS_MATCH_NO_UPDATE_TRIGGER,
        "odds_match_observations",
        "UPDATE",
        "odds_match_observations is append-only",
    ),
    (
        ODDS_MATCH_NO_DELETE_TRIGGER,
        "odds_match_observations",
        "DELETE",
        "odds_match_observations is append-only",
    ),
    (
        ODDS_LIFECYCLE_NO_UPDATE_TRIGGER,
        "odds_bout_lifecycle_observations",
        "UPDATE",
        "odds_bout_lifecycle_observations is append-only",
    ),
    (
        ODDS_LIFECYCLE_NO_DELETE_TRIGGER,
        "odds_bout_lifecycle_observations",
        "DELETE",
        "odds_bout_lifecycle_observations is append-only",
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


_PARTIAL_UNIQUE_INDEXES = (
    (
        "uq_odds_provider_event_alias_active",
        "odds_provider_event_aliases",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_odds_provider_event_alias_active "
        "ON odds_provider_event_aliases (provider, external_event_id) "
        "WHERE status = 'active'",
    ),
    (
        "uq_odds_bout_match_reviews_pending_provider_ext",
        "odds_bout_match_reviews",
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "uq_odds_bout_match_reviews_pending_provider_ext "
        "ON odds_bout_match_reviews (provider, external_event_id) "
        "WHERE status = 'pending'",
    ),
)


def install_odds_sqlite_guards(bind: Connectable | Engine) -> None:
    """Install UPDATE/DELETE abort triggers when owned tables exist."""
    dialect = getattr(getattr(bind, "dialect", None), "name", None)
    if dialect != "sqlite":
        return

    def _install(connection: Connection) -> None:
        for name, table, action, message in _TRIGGER_SPECS:
            if _table_exists(connection, table):
                connection.exec_driver_sql(_trigger_sql(name, table, action, message).strip())
        for _index_name, table, ddl in _PARTIAL_UNIQUE_INDEXES:
            if _table_exists(connection, table):
                connection.exec_driver_sql(ddl)

    if isinstance(bind, Engine):
        with bind.begin() as conn:
            _install(conn)
        return
    _install(bind)  # type: ignore[arg-type]


def drop_odds_sqlite_guards(bind: Connectable | Engine) -> None:
    """Drop owned odds quote/availability/manual-price triggers."""
    _run_sqlite_statements(bind, _DROP_STATEMENTS)
