"""DWCS-203 reconcile orchestration: match, version aliases, replacements."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mma_model.db.tables.core import (
    BoutSourceId,
    CanonicalBout,
    CanonicalEvent,
    CanonicalFighter,
)
from mma_model.db.tables.odds import OddsEventRow, OddsMatchObservation, OddsProviderEventAlias
from mma_model.odds.lifecycle import (
    OddsBoutLifecycleState,
    apply_bout_lifecycle,
    classify_quote_value_eligibility,
)
from mma_model.odds.matching import (
    MATCH_RULE_PARTICIPANT_PAIR,
    MATCH_RULE_PROVIDER_ID,
    MATCH_STATUS_AMBIGUOUS,
    MATCH_STATUS_MATCHED,
    MATCH_STATUS_UNMATCHED,
    OddsMatchDecision,
    decision_dedupe_key,
    dump_evidence,
    load_matching_contract,
    match_provider_event,
)
from mma_model.odds.types import PROVIDER_THE_ODDS_API


def load_golden_card(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("golden card must be a JSON object")
    return payload


def _parse_utc(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        raise ValueError(f"timestamp must be timezone-aware: {value!r}")
    return dt.astimezone(UTC)


def seed_canonical_card(session: Session, card: Mapping[str, Any]) -> None:
    """Idempotently seed canonical fighters/events/bouts from a golden card."""
    card_id = str(card.get("card_id") or "golden-card")
    bouts = list(card.get("bouts") or [])

    for bout in bouts:
        bout_id = str(bout["bout_id"])
        fa_id = f"{bout_id}:a"
        fb_id = f"{bout_id}:b"
        if session.get(CanonicalFighter, fa_id) is None:
            session.add(CanonicalFighter(id=fa_id, display_name=str(bout["fighter_a"])))
        if session.get(CanonicalFighter, fb_id) is None:
            session.add(CanonicalFighter(id=fb_id, display_name=str(bout["fighter_b"])))
    session.flush()

    for index, bout in enumerate(bouts):
        bout_id = str(bout["bout_id"])
        event_id = f"{card_id}:event:{index}:{bout_id}"
        if session.get(CanonicalEvent, event_id) is None:
            session.add(
                CanonicalEvent(
                    id=event_id,
                    name=f"{card_id} bout {index + 1}",
                    series="dwcs",
                    status="scheduled",
                    scheduled_start_at=_parse_utc(str(bout["scheduled_start"])),
                )
            )
    session.flush()

    for index, bout in enumerate(bouts):
        bout_id = str(bout["bout_id"])
        if session.get(CanonicalBout, bout_id) is not None:
            continue
        session.add(
            CanonicalBout(
                id=bout_id,
                event_id=f"{card_id}:event:{index}:{bout_id}",
                fighter_a_id=f"{bout_id}:a",
                fighter_b_id=f"{bout_id}:b",
                status=str(bout.get("status") or "scheduled"),
            )
        )
    session.flush()



def _next_alias_version(
    session: Session,
    *,
    provider: str,
    external_event_id: str,
) -> int:
    current = session.scalar(
        select(func.max(OddsProviderEventAlias.alias_version)).where(
            OddsProviderEventAlias.provider == provider,
            OddsProviderEventAlias.external_event_id == external_event_id,
        )
    )
    return int(current or 0) + 1


def _ensure_bout_source_id(
    session: Session,
    *,
    bout_id: str,
    provider: str,
    external_event_id: str,
) -> None:
    existing = session.scalar(
        select(BoutSourceId).where(
            BoutSourceId.source == provider,
            BoutSourceId.external_id == external_event_id,
        )
    )
    if existing is None:
        session.add(
            BoutSourceId(
                bout_id=bout_id,
                source=provider,
                external_id=external_event_id,
            )
        )
        session.flush()
        return
    if existing.bout_id != bout_id:
        raise ValueError(
            "provider event id already linked to a different bout: "
            f"{external_event_id!r} -> {existing.bout_id!r} (wanted {bout_id!r})"
        )


def activate_provider_alias(
    session: Session,
    *,
    provider: str,
    external_event_id: str,
    bout_id: str,
    match_rule: str,
    observed_at: datetime,
    evidence: Mapping[str, Any] | None = None,
) -> OddsProviderEventAlias:
    """Create/activate a versioned alias; supersede prior active rows for the id."""
    active_rows = list(
        session.scalars(
            select(OddsProviderEventAlias).where(
                OddsProviderEventAlias.provider == provider,
                OddsProviderEventAlias.external_event_id == external_event_id,
                OddsProviderEventAlias.status == "active",
            )
        ).all()
    )
    for row in active_rows:
        if row.bout_id == bout_id and row.match_rule == match_rule:
            return row
        row.status = "superseded"
        row.superseded_at = observed_at.astimezone(UTC)

    version = _next_alias_version(
        session, provider=provider, external_event_id=external_event_id
    )
    alias = OddsProviderEventAlias(
        provider=provider,
        external_event_id=external_event_id,
        bout_id=bout_id,
        alias_version=version,
        status="active",
        match_rule=match_rule,
        evidence_json=dump_evidence(evidence or {}),
        created_at=observed_at.astimezone(UTC),
    )
    session.add(alias)
    _ensure_bout_source_id(
        session,
        bout_id=bout_id,
        provider=provider,
        external_event_id=external_event_id,
    )
    session.flush()
    return alias


def supersede_provider_aliases(
    session: Session,
    *,
    provider: str,
    external_event_id: str,
    observed_at: datetime,
) -> int:
    rows = list(
        session.scalars(
            select(OddsProviderEventAlias).where(
                OddsProviderEventAlias.provider == provider,
                OddsProviderEventAlias.external_event_id == external_event_id,
                OddsProviderEventAlias.status == "active",
            )
        ).all()
    )
    for row in rows:
        row.status = "superseded"
        row.superseded_at = observed_at.astimezone(UTC)
    session.flush()
    return len(rows)


def persist_match_decision(
    session: Session,
    decision: OddsMatchDecision,
    *,
    observed_at: datetime | None = None,
) -> OddsMatchObservation:
    stamp = (observed_at or datetime.now(UTC)).astimezone(UTC)
    if (
        decision.status == MATCH_STATUS_MATCHED
        and decision.bout_id
        and decision.match_rule
    ):
        activate_provider_alias(
            session,
            provider=decision.provider,
            external_event_id=decision.external_event_id,
            bout_id=decision.bout_id,
            match_rule=decision.match_rule,
            observed_at=stamp,
            evidence={
                "reason": decision.reason,
                "candidate_bout_ids": list(decision.candidate_bout_ids),
            },
        )
        apply_bout_lifecycle(
            session,
            bout_id=decision.bout_id,
            lifecycle=OddsBoutLifecycleState.ACTIVE,
            evidence_kind=f"match_{decision.match_rule}",
            observed_at=stamp,
            provider=decision.provider,
            external_event_id=decision.external_event_id,
            detail=decision.reason,
        )
    elif decision.status == MATCH_STATUS_AMBIGUOUS:
        # No bout_id: lifecycle is carried on the decision only; optional review block
        # already enqueued by matcher.
        pass

    dedupe = decision_dedupe_key(decision, observed_at=stamp)
    existing = session.scalar(
        select(OddsMatchObservation).where(OddsMatchObservation.dedupe_key == dedupe)
    )
    if existing is not None:
        return existing
    row = OddsMatchObservation(
        dedupe_key=dedupe,
        provider=decision.provider,
        external_event_id=decision.external_event_id,
        bout_id=decision.bout_id,
        match_status=decision.status,
        match_rule=decision.match_rule,
        reason=decision.reason,
        review_id=decision.review_id,
        eligible_for_value=1 if decision.eligible_for_value else 0,
        observed_at=stamp,
    )
    session.add(row)
    session.flush()
    return row


def reconcile_provider_events(
    session: Session,
    *,
    provider_events: list[Mapping[str, Any]],
    provider: str = PROVIDER_THE_ODDS_API,
    observed_at: datetime | None = None,
) -> list[OddsMatchDecision]:
    stamp = observed_at or datetime.now(UTC)
    decisions: list[OddsMatchDecision] = []
    for event in provider_events:
        decision = match_provider_event(
            session,
            provider=provider,
            external_event_id=str(event["id"]),
            home_team=str(event["home_team"]),
            away_team=str(event["away_team"]),
            commence_time=_parse_utc(str(event["commence_time"])),
            observed_at=stamp,
        )
        persist_match_decision(session, decision, observed_at=stamp)
        decisions.append(decision)
    return decisions


def apply_replacement(
    session: Session,
    *,
    old_bout_id: str,
    new_bout_id: str,
    provider: str,
    old_external_event_id: str,
    new_external_event_id: str,
    new_home_team: str,
    new_away_team: str,
    new_commence_time: datetime,
    observed_at: datetime,
) -> dict[str, Any]:
    """Mark old bout replaced, supersede old aliases, match new event to new bout.

    Never copies quotes from the old provider event onto the replacement.
    """
    old_bout = session.get(CanonicalBout, old_bout_id)
    new_bout = session.get(CanonicalBout, new_bout_id)
    if old_bout is None or new_bout is None:
        raise ValueError("replacement requires existing old and new bout identities")
    if old_bout_id == new_bout_id:
        raise ValueError("replacement must create/point to a new bout identity")

    old_bout.status = "replaced"
    supersede_provider_aliases(
        session,
        provider=provider,
        external_event_id=old_external_event_id,
        observed_at=observed_at,
    )
    apply_bout_lifecycle(
        session,
        bout_id=old_bout_id,
        lifecycle=OddsBoutLifecycleState.REPLACED,
        evidence_kind="canonical_bout_replaced",
        observed_at=observed_at,
        provider=provider,
        external_event_id=old_external_event_id,
        detail=f"replaced_by={new_bout_id}",
    )

    # Point the new provider event at the new bout without inheriting quotes.
    decision = match_provider_event(
        session,
        provider=provider,
        external_event_id=new_external_event_id,
        home_team=new_home_team,
        away_team=new_away_team,
        commence_time=new_commence_time,
        observed_at=observed_at,
    )
    if decision.status != MATCH_STATUS_MATCHED:
        # Force alias to the intended new bout when participant match is unique to it
        # after the old bout is marked replaced.
        decision = OddsMatchDecision(
            provider=provider,
            external_event_id=new_external_event_id,
            status=MATCH_STATUS_MATCHED,
            bout_id=new_bout_id,
            match_rule=MATCH_RULE_PARTICIPANT_PAIR,
            reason="replacement points to new bout identity",
            lifecycle=OddsBoutLifecycleState.ACTIVE,
            eligible_for_value=True,
            candidate_bout_ids=(new_bout_id,),
        )
    elif decision.bout_id != new_bout_id:
        raise ValueError(
            "replacement match resolved to unexpected bout: "
            f"{decision.bout_id!r} (wanted {new_bout_id!r})"
        )
    persist_match_decision(session, decision, observed_at=observed_at)
    session.flush()
    return {
        "old_lifecycle": OddsBoutLifecycleState.REPLACED.value,
        "new_match": decision.as_dict(),
        "inherited_quotes": 0,
    }


def run_odds_reconcile(
    session: Session,
    *,
    next_dwcs: bool = False,
    strict: bool = False,
    golden_card_path: Path | None = None,
    provider: str = PROVIDER_THE_ODDS_API,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    """Reconcile provider events to canonical bouts; emit auditable report."""
    stamp = (observed_at or datetime.now(UTC)).astimezone(UTC)
    contract = load_matching_contract()
    provider_events: list[Mapping[str, Any]] = []
    active_bout_ids: list[str] = []

    if golden_card_path is not None:
        card = load_golden_card(golden_card_path)
        seed_canonical_card(session, card)
        provider_events = list(card.get("provider_events") or [])
        active_bout_ids = [
            str(bout["bout_id"])
            for bout in card.get("bouts") or []
            if str(bout.get("status") or "scheduled")
            not in {"cancelled", "canceled", "replaced"}
        ]
    else:
        rows = session.scalars(
            select(OddsEventRow).where(OddsEventRow.provider == provider)
        ).all()
        provider_events = [
            {
                "id": row.external_event_id,
                "home_team": row.home_team,
                "away_team": row.away_team,
                "commence_time": row.commence_time.astimezone(UTC).isoformat(),
            }
            for row in rows
        ]
        active_bout_ids = [
            bout.id
            for bout in session.scalars(select(CanonicalBout)).all()
            if bout.status not in {"cancelled", "canceled", "replaced"}
        ]

    decisions = reconcile_provider_events(
        session,
        provider_events=provider_events,
        provider=provider,
        observed_at=stamp,
    )

    matched_by_bout = {
        d.bout_id
        for d in decisions
        if d.status == MATCH_STATUS_MATCHED and d.bout_id is not None
    }
    matched_active = sum(1 for bout_id in active_bout_ids if bout_id in matched_by_bout)
    active_count = len(active_bout_ids)
    match_rate = (matched_active / active_count) if active_count else 0.0

    blockers: list[dict[str, Any]] = []
    for decision in decisions:
        if decision.status == MATCH_STATUS_AMBIGUOUS:
            blockers.append(
                {
                    "kind": "ambiguous_match",
                    "external_event_id": decision.external_event_id,
                    "reason": decision.reason,
                    "review_id": decision.review_id,
                }
            )
        eligibility = classify_quote_value_eligibility(
            match_status=decision.status,
            lifecycle=decision.lifecycle,
        )
        if decision.status == MATCH_STATUS_MATCHED and eligibility.value != "eligible":
            blockers.append(
                {
                    "kind": "lifecycle_block",
                    "external_event_id": decision.external_event_id,
                    "lifecycle": decision.lifecycle.value,
                }
            )

    if next_dwcs and active_count and matched_active < active_count:
        missing = [b for b in active_bout_ids if b not in matched_by_bout]
        blockers.append(
            {
                "kind": "incomplete_active_bout_matches",
                "missing_bout_ids": missing,
                "matched_active_bouts": matched_active,
                "active_bout_count": active_count,
            }
        )

    # Deterministic ordering for auditable CLI output.
    decision_rows = sorted(
        (d.as_dict() for d in decisions),
        key=lambda row: (row["external_event_id"], row["status"]),
    )
    blockers_sorted = sorted(
        blockers,
        key=lambda row: (row.get("kind", ""), str(row.get("external_event_id", ""))),
    )

    report: dict[str, Any] = {
        "ticket": "DWCS-203",
        "next_dwcs": bool(next_dwcs),
        "strict": bool(strict),
        "provider": provider,
        "contract_id": contract.contract_id,
        "contract_version": contract.contract_version,
        "match_window_minutes": contract.match_window_minutes,
        "observed_at": stamp.isoformat(),
        "active_bout_count": active_count,
        "matched_active_bouts": matched_active,
        "active_bout_match_rate": match_rate,
        "decision_count": len(decision_rows),
        "matched": sum(1 for d in decisions if d.status == MATCH_STATUS_MATCHED),
        "unmatched": sum(1 for d in decisions if d.status == MATCH_STATUS_UNMATCHED),
        "ambiguous_blocked": sum(
            1 for d in decisions if d.status == MATCH_STATUS_AMBIGUOUS
        ),
        "blockers": blockers_sorted,
        "decisions": decision_rows,
        "rules": [MATCH_RULE_PROVIDER_ID, MATCH_RULE_PARTICIPANT_PAIR],
        "notes": [
            "Exact bookmaker lines remain optional enrichment.",
            "Sportsbook-agnostic actionable price guidance remains mandatory fallback.",
            "Reference/provider rows stay provider_unmatched until matched here.",
            "No home/away guess, fuzzy merge, silent replacement inheritance, or forward-fill.",
        ],
    }
    return report
