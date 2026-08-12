"""SQLite guards for append-only identity evidence (DWCS-104)."""

from __future__ import annotations

from sqlalchemy.engine import Connectable, Connection, Engine

IDENTITY_EVIDENCE_NO_UPDATE_TRIGGER = "identity_match_evidence_no_update"
IDENTITY_EVIDENCE_NO_DELETE_TRIGGER = "identity_match_evidence_no_delete"

_TRIGGER_STATEMENTS = (
    f"""
    CREATE TRIGGER IF NOT EXISTS {IDENTITY_EVIDENCE_NO_UPDATE_TRIGGER}
    BEFORE UPDATE ON identity_match_evidence
    BEGIN
      SELECT RAISE(ABORT, 'identity_match_evidence is append-only');
    END
    """,
    f"""
    CREATE TRIGGER IF NOT EXISTS {IDENTITY_EVIDENCE_NO_DELETE_TRIGGER}
    BEFORE DELETE ON identity_match_evidence
    BEGIN
      SELECT RAISE(ABORT, 'identity_match_evidence is append-only');
    END
    """,
)
_DROP_STATEMENTS = (
    f"DROP TRIGGER IF EXISTS {IDENTITY_EVIDENCE_NO_UPDATE_TRIGGER}",
    f"DROP TRIGGER IF EXISTS {IDENTITY_EVIDENCE_NO_DELETE_TRIGGER}",
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


def install_identity_sqlite_guards(bind: Connectable | Engine) -> None:
    """Install UPDATE/DELETE abort triggers on identity_match_evidence."""
    _run_sqlite_statements(bind, _TRIGGER_STATEMENTS)


def drop_identity_sqlite_guards(bind: Connectable | Engine) -> None:
    """Drop only DWCS-104 owned evidence triggers."""
    _run_sqlite_statements(bind, _DROP_STATEMENTS)
