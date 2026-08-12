"""Import legacy UFCStats IDs into canonical source-ID maps.

Preserves existing CLI lookups on legacy fighters/events/fights tables while
exposing the same external IDs through fighter_source_ids / event_source_ids /
bout_source_ids with source='ufcstats'.

Revision ID: 0003_legacy_import
Revises: 0002_canonical_core
Create Date: 2026-08-11
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "0003_legacy_import"
down_revision: Union[str, Sequence[str], None] = "0002_canonical_core"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SOURCE = "ufcstats"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def upgrade() -> None:
    conn = op.get_bind()
    now = _utc_now_iso()

    # Fighters
    fighters = conn.execute(text("SELECT id, name, nickname FROM fighters")).mappings().all()
    fighter_map: dict[str, str] = {}
    for row in fighters:
        existing = conn.execute(
            text(
                "SELECT fighter_id FROM fighter_source_ids "
                "WHERE source = :source AND external_id = :external_id"
            ),
            {"source": SOURCE, "external_id": row["id"]},
        ).scalar()
        if existing:
            fighter_map[row["id"]] = existing
            continue
        canonical_id = str(uuid.uuid4())
        fighter_map[row["id"]] = canonical_id
        conn.execute(
            text(
                "INSERT INTO canonical_fighters (id, display_name, created_at, updated_at) "
                "VALUES (:id, :name, :created_at, :updated_at)"
            ),
            {
                "id": canonical_id,
                "name": row["name"],
                "created_at": now,
                "updated_at": now,
            },
        )
        conn.execute(
            text(
                "INSERT INTO fighter_source_ids "
                "(fighter_id, source, external_id, created_at) "
                "VALUES (:fighter_id, :source, :external_id, :created_at)"
            ),
            {
                "fighter_id": canonical_id,
                "source": SOURCE,
                "external_id": row["id"],
                "created_at": now,
            },
        )
        conn.execute(
            text(
                "INSERT INTO fighter_aliases (fighter_id, alias, source, created_at) "
                "VALUES (:fighter_id, :alias, :source, :created_at)"
            ),
            {
                "fighter_id": canonical_id,
                "alias": row["name"],
                "source": SOURCE,
                "created_at": now,
            },
        )
        if row["nickname"]:
            conn.execute(
                text(
                    "INSERT INTO fighter_aliases (fighter_id, alias, source, created_at) "
                    "VALUES (:fighter_id, :alias, :source, :created_at)"
                ),
                {
                    "fighter_id": canonical_id,
                    "alias": row["nickname"],
                    "source": SOURCE,
                    "created_at": now,
                },
            )

    # Events
    events = conn.execute(
        text("SELECT id, name, event_date, location FROM events")
    ).mappings().all()
    event_map: dict[str, str] = {}
    for row in events:
        existing = conn.execute(
            text(
                "SELECT event_id FROM event_source_ids "
                "WHERE source = :source AND external_id = :external_id"
            ),
            {"source": SOURCE, "external_id": row["id"]},
        ).scalar()
        if existing:
            event_map[row["id"]] = existing
            continue
        canonical_id = str(uuid.uuid4())
        event_map[row["id"]] = canonical_id
        status = "completed" if row["event_date"] is not None else "scheduled"
        conn.execute(
            text(
                "INSERT INTO canonical_events "
                "(id, name, series, status, scheduled_start_at, event_date, location, "
                "created_at, updated_at) "
                "VALUES (:id, :name, :series, :status, NULL, :event_date, :location, "
                ":created_at, :updated_at)"
            ),
            {
                "id": canonical_id,
                "name": row["name"],
                "series": "ufc",
                "status": status,
                "event_date": row["event_date"],
                "location": row["location"],
                "created_at": now,
                "updated_at": now,
            },
        )
        conn.execute(
            text(
                "INSERT INTO event_source_ids (event_id, source, external_id, created_at) "
                "VALUES (:event_id, :source, :external_id, :created_at)"
            ),
            {
                "event_id": canonical_id,
                "source": SOURCE,
                "external_id": row["id"],
                "created_at": now,
            },
        )

    # Fights → bouts (fail closed on dirty/orphan rows; never silent skip).
    fights = conn.execute(
        text(
            "SELECT id, event_id, fighter_a_id, fighter_b_id, winner_id, "
            "weight_class, method, fight_round, time_str FROM fights"
        )
    ).mappings().all()
    unresolved: list[str] = []
    for row in fights:
        existing = conn.execute(
            text(
                "SELECT bout_id FROM bout_source_ids "
                "WHERE source = :source AND external_id = :external_id"
            ),
            {"source": SOURCE, "external_id": row["id"]},
        ).scalar()
        if existing:
            continue
        event_id = event_map.get(row["event_id"])
        fighter_a = fighter_map.get(row["fighter_a_id"])
        fighter_b = fighter_map.get(row["fighter_b_id"])
        problems: list[str] = []
        if not event_id:
            problems.append(f"missing_event={row['event_id']!r}")
        if not fighter_a:
            problems.append(f"missing_fighter_a={row['fighter_a_id']!r}")
        if not fighter_b:
            problems.append(f"missing_fighter_b={row['fighter_b_id']!r}")
        if fighter_a and fighter_b and fighter_a == fighter_b:
            problems.append("fighters_not_distinct")
        winner = None
        if row["winner_id"]:
            winner = fighter_map.get(row["winner_id"])
            if winner is None:
                problems.append(f"missing_winner={row['winner_id']!r}")
            elif fighter_a and fighter_b and winner not in (fighter_a, fighter_b):
                problems.append(
                    f"winner_not_participant={row['winner_id']!r}"
                )
        if problems:
            unresolved.append(f"fight_id={row['id']}: " + ", ".join(problems))
            continue
        assert event_id is not None and fighter_a is not None and fighter_b is not None
        bout_id = str(uuid.uuid4())
        status = "completed" if row["winner_id"] or row["method"] else "scheduled"
        conn.execute(
            text(
                "INSERT INTO canonical_bouts "
                "(id, event_id, fighter_a_id, fighter_b_id, scheduled_rounds, "
                "weight_class, status, created_at, updated_at) "
                "VALUES (:id, :event_id, :fighter_a_id, :fighter_b_id, 3, "
                ":weight_class, :status, :created_at, :updated_at)"
            ),
            {
                "id": bout_id,
                "event_id": event_id,
                "fighter_a_id": fighter_a,
                "fighter_b_id": fighter_b,
                "weight_class": row["weight_class"],
                "status": status,
                "created_at": now,
                "updated_at": now,
            },
        )
        conn.execute(
            text(
                "INSERT INTO bout_source_ids (bout_id, source, external_id, created_at) "
                "VALUES (:bout_id, :source, :external_id, :created_at)"
            ),
            {
                "bout_id": bout_id,
                "source": SOURCE,
                "external_id": row["id"],
                "created_at": now,
            },
        )
        for corner, fighter_id in (("a", fighter_a), ("b", fighter_b)):
            conn.execute(
                text(
                    "INSERT INTO bout_participants "
                    "(bout_id, fighter_id, corner, created_at) "
                    "VALUES (:bout_id, :fighter_id, :corner, :created_at)"
                ),
                {
                    "bout_id": bout_id,
                    "fighter_id": fighter_id,
                    "corner": corner,
                    "created_at": now,
                },
            )
        result_type = None
        if winner:
            result_type = "win"
        elif row["method"] and "draw" in str(row["method"]).lower():
            result_type = "draw"
        elif row["method"] and "nc" in str(row["method"]).lower().replace("-", ""):
            result_type = "no_contest"
        for version_kind in ("event_night", "current"):
            conn.execute(
                text(
                    "INSERT INTO bout_result_versions "
                    "(bout_id, version_kind, fighter_a_id, fighter_b_id, winner_fighter_id, "
                    "result_type, method, ending_round, time_str, effective_at, observed_at, "
                    "created_at) "
                    "VALUES (:bout_id, :version_kind, :fighter_a_id, :fighter_b_id, :winner, "
                    ":result_type, :method, :ending_round, :time_str, :effective_at, "
                    ":observed_at, :created_at)"
                ),
                {
                    "bout_id": bout_id,
                    "version_kind": version_kind,
                    "fighter_a_id": fighter_a,
                    "fighter_b_id": fighter_b,
                    "winner": winner,
                    "result_type": result_type,
                    "method": row["method"],
                    "ending_round": row["fight_round"],
                    "time_str": row["time_str"],
                    "effective_at": now,
                    "observed_at": now,
                    "created_at": now,
                },
            )
    if unresolved:
        sample = "; ".join(unresolved[:20])
        more = f" (+{len(unresolved) - 20} more)" if len(unresolved) > 20 else ""
        raise RuntimeError(
            "Legacy UFCStats import failed closed: unresolved fights cannot be "
            f"mapped without inventing identities ({len(unresolved)} total). "
            f"Evidence: {sample}{more}. Repair orphan event/fighter FKs or remove "
            "dirty fights, then re-run alembic upgrade."
        )


def downgrade() -> None:
    """Remove only rows introduced from the ufcstats legacy import."""
    conn = op.get_bind()
    bout_ids = [
        r[0]
        for r in conn.execute(
            text("SELECT bout_id FROM bout_source_ids WHERE source = :source"),
            {"source": SOURCE},
        )
    ]
    for bout_id in bout_ids:
        conn.execute(
            text("DELETE FROM bout_result_versions WHERE bout_id = :bout_id"),
            {"bout_id": bout_id},
        )
        conn.execute(
            text("DELETE FROM bout_participants WHERE bout_id = :bout_id"),
            {"bout_id": bout_id},
        )
        conn.execute(
            text("DELETE FROM fighter_stat_observations WHERE bout_id = :bout_id"),
            {"bout_id": bout_id},
        )
    conn.execute(
        text("DELETE FROM bout_source_ids WHERE source = :source"),
        {"source": SOURCE},
    )
    for bout_id in bout_ids:
        conn.execute(
            text("DELETE FROM canonical_bouts WHERE id = :bout_id"),
            {"bout_id": bout_id},
        )

    event_ids = [
        r[0]
        for r in conn.execute(
            text("SELECT event_id FROM event_source_ids WHERE source = :source"),
            {"source": SOURCE},
        )
    ]
    conn.execute(
        text("DELETE FROM event_source_ids WHERE source = :source"),
        {"source": SOURCE},
    )
    for event_id in event_ids:
        remaining = conn.execute(
            text("SELECT COUNT(*) FROM event_source_ids WHERE event_id = :event_id"),
            {"event_id": event_id},
        ).scalar()
        if remaining == 0:
            conn.execute(
                text("DELETE FROM canonical_events WHERE id = :event_id"),
                {"event_id": event_id},
            )

    fighter_ids = [
        r[0]
        for r in conn.execute(
            text("SELECT fighter_id FROM fighter_source_ids WHERE source = :source"),
            {"source": SOURCE},
        )
    ]
    # Delete fighter-linked observations first (including bout_id IS NULL rows).
    for fighter_id in fighter_ids:
        conn.execute(
            text("DELETE FROM fighter_stat_observations WHERE fighter_id = :fighter_id"),
            {"fighter_id": fighter_id},
        )
        conn.execute(
            text(
                "DELETE FROM fighter_profile_observations WHERE fighter_id = :fighter_id"
            ),
            {"fighter_id": fighter_id},
        )
        conn.execute(
            text("DELETE FROM fighter_aliases WHERE fighter_id = :fighter_id"),
            {"fighter_id": fighter_id},
        )
    conn.execute(
        text("DELETE FROM fighter_source_ids WHERE source = :source"),
        {"source": SOURCE},
    )
    for fighter_id in fighter_ids:
        remaining = conn.execute(
            text("SELECT COUNT(*) FROM fighter_source_ids WHERE fighter_id = :fighter_id"),
            {"fighter_id": fighter_id},
        ).scalar()
        if remaining == 0:
            conn.execute(
                text("DELETE FROM canonical_fighters WHERE id = :fighter_id"),
                {"fighter_id": fighter_id},
            )
