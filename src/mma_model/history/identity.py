"""Identity linking for regional bouts (DWCS-105).

Exact source IDs and Wikidata auto-link. Name-only, fuzzy, transliteration,
and nickname candidates are queued; only affected bouts are scoring-blocked.
Never silently auto-merge.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mma_model.db.tables.core import FighterSourceId
from mma_model.db.tables.history import HistoryConflict, HistorySourceBout
from mma_model.db.tables.identity import IdentityReviewQueue, IdentityScoringBlock
from mma_model.identity.models import ResolveResult
from mma_model.identity.resolver import IdentityResolver


def resolve_regional_fighter(
    session: Session,
    *,
    source: str,
    external_id: str,
    display_name: str,
    wikidata_id: str | None = None,
    opponent_normalized_name: str | None = None,
    event_date: date | None = None,
    bout_id: str | None = None,
    bout_status: str | None = None,
    actor: str = "system",
    now: datetime | None = None,
    candidate_hints: tuple[str, ...] = (),
) -> ResolveResult:
    resolver = IdentityResolver(session, actor=actor, now=now)
    return resolver.resolve_fighter(
        source=source,
        external_id=external_id,
        display_name=display_name,
        wikidata_id=wikidata_id,
        opponent_normalized_name=opponent_normalized_name,
        event_date=event_date,
        bout_id=bout_id,
        bout_status=bout_status,
        candidate_hints=candidate_hints,
        create_if_absent=True,
    )


def identity_status_from_result(result: ResolveResult) -> str:
    if result.kind in {"linked", "created"}:
        return "linked"
    if result.kind == "queued":
        return "queued"
    if result.kind == "blocked":
        return "blocked"
    raise ValueError(f"unhandled resolve kind: {result.kind}")


def identity_summary(
    results: list[ResolveResult],
    session: Session | None = None,
) -> dict[str, Any]:
    exact = 0
    queued = 0
    blocked = 0
    created = 0
    for row in results:
        if row.kind == "linked":
            exact += 1
        elif row.kind == "created":
            created += 1
        elif row.kind == "queued":
            queued += 1
        elif row.kind == "blocked":
            blocked += 1
        else:
            raise ValueError(f"unhandled resolve kind: {row.kind}")
    conflations = compute_identity_conflations(session) if session is not None else queued
    return {
        "exact_links": exact,
        "created": created,
        "queued": queued,
        "blocks": blocked,
        "unresolved": queued + blocked,
        "conflations": conflations,
    }


def compute_identity_conflations(session: Session) -> int:
    """Count actual identity conflicts, not a hardcoded zero.

    Includes pending same-name / duplicate-external review rows, identity
    history conflicts, active scoring blocks, and duplicate (source, external_id)
    mappings if any exist.
    """
    pending = session.scalar(
        select(func.count()).select_from(IdentityReviewQueue).where(
            IdentityReviewQueue.status == "pending"
        )
    ) or 0
    identity_conflicts = session.scalar(
        select(func.count()).select_from(HistoryConflict).where(
            HistoryConflict.conflict_type.in_(
                ("identity", "canonical_collision", "duplicate_external")
            )
        )
    ) or 0
    blocks = session.scalar(
        select(func.count()).select_from(IdentityScoringBlock).where(
            IdentityScoringBlock.active.is_(True)
        )
    ) or 0
    dup_external = session.scalar(
        select(func.count()).select_from(
            select(FighterSourceId.source, FighterSourceId.external_id)
            .group_by(FighterSourceId.source, FighterSourceId.external_id)
            .having(func.count(func.distinct(FighterSourceId.fighter_id)) > 1)
            .subquery()
        )
    ) or 0
    return int(pending) + int(identity_conflicts) + int(blocks) + int(dup_external)


def count_exact_source_id_links(session: Session) -> int:
    """Count unique exact source-ID / crosswalk rows, not bout observations."""
    n = session.scalar(select(func.count()).select_from(FighterSourceId))
    return int(n or 0)


def count_unique_identity_status(session: Session, status: str) -> int:
    rows = session.scalars(
        select(HistorySourceBout).where(HistorySourceBout.identity_status == status)
    ).all()
    keys = {
        row.fighter_canonical_id or row.fighter_external_id or row.id for row in rows
    }
    return len(keys)


def count_unique_unresolved_identities(session: Session) -> int:
    rows = session.scalars(
        select(HistorySourceBout).where(
            HistorySourceBout.identity_status.in_(("blocked", "queued", "unresolved"))
        )
    ).all()
    return len(
        {row.fighter_canonical_id or row.fighter_external_id or row.id for row in rows}
    )
