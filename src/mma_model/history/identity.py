"""Identity linking for regional bouts (DWCS-105).

Exact source IDs and Wikidata auto-link. Name-only, fuzzy, transliteration,
and nickname candidates are queued; only affected bouts are scoring-blocked.
Never silently auto-merge.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session

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


def identity_summary(results: list[ResolveResult]) -> dict[str, Any]:
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
    return {
        "exact_links": exact,
        "created": created,
        "queued": queued,
        "blocks": blocked,
        "conflations": 0,
    }
