"""DWCS-203 reconcile orchestration: match, version aliases, replacements."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
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
from mma_model.odds.match_review import enqueue_bout_match_review
from mma_model.odds.matching import (
    MATCH_RULE_PARTICIPANT_PAIR,
    MATCH_RULE_PROVIDER_ID,
    MATCH_STATUS_AMBIGUOUS,
    MATCH_STATUS_MATCHED,
    MATCH_STATUS_UNMATCHED,
    OddsMatchDecision,
    as_utc_sqlite,
    decision_dedupe_key,
    dump_evidence,
    load_matching_contract,
    match_provider_event,
    require_aware_utc,
)
from mma_model.odds.snapshot import OddsOfflineModeError, require_disposable_database_url
from mma_model.odds.types import PROVIDER_THE_ODDS_API

_DWCS_SERIES = frozenset({"dwcs", "dwcs_brazil"})
_INACTIVE_BOUT = frozenset({"cancelled", "canceled", "replaced"})
_UPCOMING_EVENT_STATUS = frozenset({"scheduled", "upcoming"})


class OddsReconcileError(ValueError):
    """Fail-closed reconcile configuration / scope error."""


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
    """Idempotently seed canonical fighters/events/bouts from a golden card.

    Test/offline only. Production CLI must not call this against a live DB.
    Bouts share a card-anchor DWCS event when their start equals the card
    minimum; otherwise each bout gets its own DWCS event (same series) so
    participant+time matching stays exact. ``--next-dwcs`` expands the
    nearest upcoming event to the full fight-night cluster.
    """
    card_id = str(card.get("card_id") or "golden-card")
    bouts = list(card.get("bouts") or [])
    if not bouts:
        return

    for bout in bouts:
        bout_id = str(bout["bout_id"])
        fa_id = f"{bout_id}:a"
        fb_id = f"{bout_id}:b"
        if session.get(CanonicalFighter, fa_id) is None:
            session.add(CanonicalFighter(id=fa_id, display_name=str(bout["fighter_a"])))
        if session.get(CanonicalFighter, fb_id) is None:
            session.add(CanonicalFighter(id=fb_id, display_name=str(bout["fighter_b"])))
    session.flush()

    starts = [_parse_utc(str(bout["scheduled_start"])) for bout in bouts]
    card_start = min(starts)
    event_id = f"{card_id}:event"
    if session.get(CanonicalEvent, event_id) is None:
        session.add(
            CanonicalEvent(
                id=event_id,
                name=str(card.get("label") or card_id),
                series="dwcs",
                status="scheduled",
                scheduled_start_at=card_start,
            )
        )
        session.flush()

    # Per-bout scheduled starts: create lightweight child events when a bout's
    # start differs from the card anchor so participant+time matching stays exact.
    for index, bout in enumerate(bouts):
        bout_id = str(bout["bout_id"])
        bout_start = _parse_utc(str(bout["scheduled_start"]))
        if bout_start == card_start:
            bout_event_id = event_id
        else:
            bout_event_id = f"{card_id}:event:{index}:{bout_id}"
            if session.get(CanonicalEvent, bout_event_id) is None:
                session.add(
                    CanonicalEvent(
                        id=bout_event_id,
                        name=f"{card_id} bout {index + 1}",
                        series="dwcs",
                        status="scheduled",
                        scheduled_start_at=bout_start,
                    )
                )
        if session.get(CanonicalBout, bout_id) is not None:
            continue
        session.add(
            CanonicalBout(
                id=bout_id,
                event_id=bout_event_id,
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


def _maybe_write_immutable_bout_source_id(
    session: Session,
    *,
    bout_id: str,
    provider: str,
    external_event_id: str,
) -> None:
    """Record BoutSourceId only on first sight; never reassign (immutable)."""
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
    # Honest semantics: keep the original immutable binding even when aliases
    # version to a replacement bout for the same external id.


def activate_provider_alias(
    session: Session,
    *,
    provider: str,
    external_event_id: str,
    bout_id: str,
    match_rule: str,
    observed_at: datetime,
    evidence: Mapping[str, Any] | None = None,
    write_immutable_source_id: bool = True,
) -> OddsProviderEventAlias:
    """Create/activate a versioned alias; supersede prior active rows for the id."""
    stamp = require_aware_utc(observed_at, field="observed_at")
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
        row.superseded_at = stamp

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
        created_at=stamp,
        superseded_at=None,
    )
    session.add(alias)
    if write_immutable_source_id:
        _maybe_write_immutable_bout_source_id(
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
    stamp = require_aware_utc(observed_at, field="observed_at")
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
        row.superseded_at = stamp
    session.flush()
    return len(rows)


def persist_match_decision(
    session: Session,
    decision: OddsMatchDecision,
    *,
    observed_at: datetime | None = None,
) -> OddsMatchObservation:
    stamp = require_aware_utc(observed_at or datetime.now(UTC), field="observed_at")
    if (
        decision.status == MATCH_STATUS_MATCHED
        and decision.bout_id
        and decision.match_rule
        and decision.eligible_for_value
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
            allow_terminal_override=False,
        )
    elif (
        decision.status == MATCH_STATUS_MATCHED
        and decision.bout_id
        and decision.match_rule
        and not decision.eligible_for_value
    ):
        # Persist alias for identity continuity when matched but blocked by lifecycle,
        # without writing an ACTIVE lifecycle override.
        activate_provider_alias(
            session,
            provider=decision.provider,
            external_event_id=decision.external_event_id,
            bout_id=decision.bout_id,
            match_rule=decision.match_rule,
            observed_at=stamp,
            evidence={
                "reason": decision.reason,
                "lifecycle": decision.lifecycle.value,
                "eligible_for_value": False,
            },
        )
        if decision.lifecycle is OddsBoutLifecycleState.STALE:
            apply_bout_lifecycle(
                session,
                bout_id=decision.bout_id,
                lifecycle=OddsBoutLifecycleState.STALE,
                evidence_kind="quote_age_exceeds_stale_after_minutes",
                observed_at=stamp,
                provider=decision.provider,
                external_event_id=decision.external_event_id,
            )

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
    require_dwcs: bool = True,
    event_ids: Sequence[str] | None = None,
) -> list[OddsMatchDecision]:
    stamp = require_aware_utc(observed_at or datetime.now(UTC), field="observed_at")
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
            require_dwcs=require_dwcs,
            event_ids=event_ids,
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
    """Mark old bout replaced and match the new event independently.

    Never fabricates MATCHED. Never copies quotes onto the replacement identity.
    Same external ID reuse is supported via alias versioning without mutating
    immutable BoutSourceId rows.
    """
    stamp = require_aware_utc(observed_at, field="observed_at")
    commence = require_aware_utc(new_commence_time, field="new_commence_time")
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
        observed_at=stamp,
    )
    # Same-ID replacement: also supersede any active alias on the reused id before rematch.
    if new_external_event_id == old_external_event_id:
        supersede_provider_aliases(
            session,
            provider=provider,
            external_event_id=new_external_event_id,
            observed_at=stamp,
        )
    apply_bout_lifecycle(
        session,
        bout_id=old_bout_id,
        lifecycle=OddsBoutLifecycleState.REPLACED,
        evidence_kind="canonical_bout_replaced",
        observed_at=stamp,
        provider=provider,
        external_event_id=old_external_event_id,
        detail=f"replaced_by={new_bout_id}",
    )

    decision = match_provider_event(
        session,
        provider=provider,
        external_event_id=new_external_event_id,
        home_team=new_home_team,
        away_team=new_away_team,
        commence_time=commence,
        observed_at=stamp,
    )
    if decision.status != MATCH_STATUS_MATCHED or decision.bout_id != new_bout_id:
        # Do not activate any alias / eligibility for a failed independent match.
        if decision.status == MATCH_STATUS_MATCHED and decision.bout_id != new_bout_id:
            reason = (
                "replacement match resolved to unexpected bout: "
                f"{decision.bout_id!r} (wanted {new_bout_id!r})"
            )
            review_id = enqueue_bout_match_review(
                session,
                provider=provider,
                external_event_id=new_external_event_id,
                home_team=new_home_team,
                away_team=new_away_team,
                commence_time=commence,
                candidate_bout_ids=tuple(
                    x for x in (decision.bout_id, new_bout_id) if x
                ),
                reason=reason,
                observed_at=stamp,
            )
            decision = OddsMatchDecision(
                provider=provider,
                external_event_id=new_external_event_id,
                status=MATCH_STATUS_AMBIGUOUS,
                bout_id=None,
                match_rule=None,
                reason=reason,
                lifecycle=OddsBoutLifecycleState.REVIEW_BLOCKED,
                eligible_for_value=False,
                review_id=review_id,
                candidate_bout_ids=tuple(
                    x for x in (decision.bout_id, new_bout_id) if x
                ),
            )
        elif decision.status == MATCH_STATUS_UNMATCHED:
            review_id = enqueue_bout_match_review(
                session,
                provider=provider,
                external_event_id=new_external_event_id,
                home_team=new_home_team,
                away_team=new_away_team,
                commence_time=commence,
                candidate_bout_ids=(new_bout_id,),
                reason="replacement unmatched to new bout",
                observed_at=stamp,
            )
            decision = OddsMatchDecision(
                provider=provider,
                external_event_id=new_external_event_id,
                status=MATCH_STATUS_AMBIGUOUS,
                bout_id=None,
                match_rule=None,
                reason="replacement unmatched to new bout",
                lifecycle=OddsBoutLifecycleState.REVIEW_BLOCKED,
                eligible_for_value=False,
                review_id=review_id or decision.review_id,
                candidate_bout_ids=(new_bout_id,),
            )
        persist_match_decision(session, decision, observed_at=stamp)
        session.flush()
        return {
            "old_lifecycle": OddsBoutLifecycleState.REPLACED.value,
            "new_match": decision.as_dict(),
            "inherited_quotes": 0,
            "activated": False,
        }

    persist_match_decision(session, decision, observed_at=stamp)
    session.flush()
    return {
        "old_lifecycle": OddsBoutLifecycleState.REPLACED.value,
        "new_match": decision.as_dict(),
        "inherited_quotes": 0,
        "activated": True,
    }


def select_next_dwcs_event(
    session: Session,
    *,
    as_of: datetime,
) -> CanonicalEvent | None:
    """Nearest upcoming DWCS card event at/after as_of (deterministic)."""
    stamp = require_aware_utc(as_of, field="as_of")
    events = session.scalars(
        select(CanonicalEvent)
        .where(CanonicalEvent.series.in_(tuple(_DWCS_SERIES)))
        .where(CanonicalEvent.status.in_(tuple(_UPCOMING_EVENT_STATUS)))
        .where(CanonicalEvent.scheduled_start_at.is_not(None))
        .order_by(CanonicalEvent.scheduled_start_at.asc(), CanonicalEvent.id.asc())
    ).all()
    for event in events:
        start = as_utc_sqlite(event.scheduled_start_at)  # type: ignore[arg-type]
        if start >= stamp:
            return event
    return None


# Fight-night cluster: keep undercard / main-card starts on one next-DWCS card.
_CARD_CLUSTER_HOURS = 18


def expand_next_dwcs_card_events(
    session: Session,
    *,
    anchor: CanonicalEvent,
) -> list[CanonicalEvent]:
    """Expand an anchor event to nearby DWCS events on the same fight night."""
    if anchor.scheduled_start_at is None:
        return [anchor]
    anchor_start = as_utc_sqlite(anchor.scheduled_start_at)
    events = session.scalars(
        select(CanonicalEvent)
        .where(CanonicalEvent.series.in_(tuple(_DWCS_SERIES)))
        .where(CanonicalEvent.status.in_(tuple(_UPCOMING_EVENT_STATUS)))
        .where(CanonicalEvent.scheduled_start_at.is_not(None))
        .order_by(CanonicalEvent.scheduled_start_at.asc(), CanonicalEvent.id.asc())
    ).all()
    clustered = [
        event
        for event in events
        if event.scheduled_start_at is not None
        and abs(
            (as_utc_sqlite(event.scheduled_start_at) - anchor_start).total_seconds()
        )
        <= _CARD_CLUSTER_HOURS * 3600
    ]
    return clustered or [anchor]


def _active_bouts_for_events(
    session: Session, event_ids: Sequence[str]
) -> list[CanonicalBout]:
    if not event_ids:
        return []
    return list(
        session.scalars(
            select(CanonicalBout)
            .where(CanonicalBout.event_id.in_(tuple(event_ids)))
            .where(CanonicalBout.status.notin_(tuple(_INACTIVE_BOUT)))
            .order_by(CanonicalBout.id.asc())
        ).all()
    )


def _provider_events_in_scope(
    session: Session,
    *,
    provider: str,
    bouts: Sequence[CanonicalBout],
    match_window_minutes: int,
    fixture_events: Sequence[Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    starts: list[datetime] = []
    for bout in bouts:
        event = session.get(CanonicalEvent, bout.event_id)
        if event is None or event.scheduled_start_at is None:
            continue
        starts.append(as_utc_sqlite(event.scheduled_start_at))
    if not starts:
        return []
    low = min(starts) - timedelta(minutes=match_window_minutes)
    high = max(starts) + timedelta(minutes=match_window_minutes)

    if fixture_events is not None:
        rows = list(fixture_events)
    else:
        db_rows = session.scalars(
            select(OddsEventRow).where(OddsEventRow.provider == provider)
        ).all()
        rows = [
            {
                "id": row.external_event_id,
                "home_team": row.home_team,
                "away_team": row.away_team,
                "commence_time": as_utc_sqlite(row.commence_time).isoformat(),
            }
            for row in db_rows
        ]

    scoped: list[dict[str, Any]] = []
    for row in rows:
        commence = _parse_utc(str(row["commence_time"]))
        if low <= commence <= high:
            scoped.append(
                {
                    "id": str(row["id"]),
                    "home_team": str(row["home_team"]),
                    "away_team": str(row["away_team"]),
                    "commence_time": commence.isoformat(),
                }
            )
    scoped.sort(key=lambda item: (item["commence_time"], item["id"]))
    return scoped


def run_odds_reconcile(
    session: Session,
    *,
    next_dwcs: bool = False,
    strict: bool = False,
    golden_card_path: Path | None = None,
    provider: str = PROVIDER_THE_ODDS_API,
    observed_at: datetime | None = None,
    as_of: datetime | None = None,
    offline_fixtures: bool = False,
    database_url: str | None = None,
    allow_golden_seed: bool = False,
) -> dict[str, Any]:
    """Reconcile provider events to canonical bouts; emit auditable report."""
    stamp = require_aware_utc(observed_at or datetime.now(UTC), field="observed_at")
    as_of_stamp = require_aware_utc(as_of or stamp, field="as_of")
    contract = load_matching_contract()

    fixture_events: list[Mapping[str, Any]] | None = None
    if golden_card_path is not None:
        if not (offline_fixtures and allow_golden_seed):
            raise OddsReconcileError(
                "--golden-card is test/offline only; require --offline-fixtures "
                "with an explicit disposable --database-url"
            )
        # Defense in depth: refuse live/default DB URLs.
        require_disposable_database_url(database_url)
        card = load_golden_card(golden_card_path)
        seed_canonical_card(session, card)
        fixture_events = list(card.get("provider_events") or [])

    blockers: list[dict[str, Any]] = []
    scoped_event: CanonicalEvent | None = None
    active_bouts: list[CanonicalBout] = []
    provider_events: list[dict[str, Any]] = []

    if next_dwcs:
        scoped_event = select_next_dwcs_event(session, as_of=as_of_stamp)
        if scoped_event is None:
            blockers.append(
                {
                    "kind": "zero_next_dwcs_event",
                    "as_of": as_of_stamp.isoformat(),
                    "reason": "no upcoming DWCS event at/after as_of",
                }
            )
        else:
            card_events = expand_next_dwcs_card_events(session, anchor=scoped_event)
            active_bouts = _active_bouts_for_events(
                session, [event.id for event in card_events]
            )
            if not active_bouts:
                blockers.append(
                    {
                        "kind": "zero_canonical_bouts",
                        "event_id": scoped_event.id,
                        "reason": "next DWCS card has zero active bouts",
                    }
                )
            provider_events = _provider_events_in_scope(
                session,
                provider=provider,
                bouts=active_bouts,
                match_window_minutes=contract.match_window_minutes,
                fixture_events=fixture_events,
            )
            if active_bouts and not provider_events:
                blockers.append(
                    {
                        "kind": "zero_provider_events",
                        "event_id": scoped_event.id,
                        "reason": "no provider events in next-DWCS time scope",
                    }
                )
    elif golden_card_path is not None:
        # Offline golden path without --next-dwcs: reconcile fixture events against
        # seeded card bouts only.
        card = load_golden_card(golden_card_path)
        active_bouts = [
            bout
            for bout_id in (
                str(b["bout_id"])
                for b in card.get("bouts") or []
                if str(b.get("status") or "scheduled") not in _INACTIVE_BOUT
            )
            if (bout := session.get(CanonicalBout, bout_id)) is not None
        ]
        provider_events = _provider_events_in_scope(
            session,
            provider=provider,
            bouts=active_bouts,
            match_window_minutes=contract.match_window_minutes,
            fixture_events=fixture_events,
        )
        if not active_bouts:
            blockers.append({"kind": "zero_canonical_bouts", "reason": "golden card empty"})
        if not provider_events:
            blockers.append(
                {"kind": "zero_provider_events", "reason": "golden card has no events"}
            )
    else:
        # Non-next-dwcs production reconcile: all stored provider events against
        # active DWCS bouts (still series-scoped for safety).
        active_bouts = [
            bout
            for bout in session.scalars(select(CanonicalBout)).all()
            if bout.status not in _INACTIVE_BOUT
            and _event_is_dwcs(session.get(CanonicalEvent, bout.event_id))
        ]
        provider_events = _provider_events_in_scope(
            session,
            provider=provider,
            bouts=active_bouts,
            match_window_minutes=contract.match_window_minutes,
            fixture_events=None,
        )

    event_ids = tuple({bout.event_id for bout in active_bouts})
    decisions: list[OddsMatchDecision] = []
    if provider_events and active_bouts:
        decisions = reconcile_provider_events(
            session,
            provider_events=provider_events,
            provider=provider,
            observed_at=stamp,
            require_dwcs=True,
            event_ids=event_ids or None,
        )

    matched_by_bout = {
        d.bout_id
        for d in decisions
        if d.status == MATCH_STATUS_MATCHED and d.bout_id is not None
    }
    active_bout_ids = [bout.id for bout in active_bouts]
    matched_active = sum(1 for bout_id in active_bout_ids if bout_id in matched_by_bout)
    active_count = len(active_bout_ids)
    match_rate = (matched_active / active_count) if active_count else 0.0

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
                    "bout_id": decision.bout_id,
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

    decision_rows = sorted(
        (d.as_dict() for d in decisions),
        key=lambda row: (row["external_event_id"], row["status"]),
    )
    blockers_sorted = sorted(
        blockers,
        key=lambda row: (
            row.get("kind", ""),
            str(row.get("external_event_id", "")),
            str(row.get("event_id", "")),
        ),
    )

    return {
        "ticket": "DWCS-203",
        "next_dwcs": bool(next_dwcs),
        "strict": bool(strict),
        "provider": provider,
        "contract_id": contract.contract_id,
        "contract_version": contract.contract_version,
        "contract_content_hash": contract.content_hash,
        "match_window_minutes": contract.match_window_minutes,
        "stale_after_minutes": contract.stale_after_minutes,
        "observed_at": stamp.isoformat(),
        "as_of": as_of_stamp.isoformat(),
        "scoped_event_id": scoped_event.id if scoped_event is not None else None,
        "scoped_event_name": scoped_event.name if scoped_event is not None else None,
        "active_bout_count": active_count,
        "matched_active_bouts": matched_active,
        "active_bout_match_rate": match_rate,
        "provider_event_count": len(provider_events),
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
            "Provider-ID matches remain subject to participant/status/series/time checks.",
            "Golden-card seeding is offline/disposable-DB only.",
        ],
    }


def _event_is_dwcs(event: CanonicalEvent | None) -> bool:
    if event is None:
        return False
    return (event.series or "").strip().lower() in _DWCS_SERIES


__all__ = [
    "OddsOfflineModeError",
    "OddsReconcileError",
    "activate_provider_alias",
    "apply_replacement",
    "load_golden_card",
    "persist_match_decision",
    "reconcile_provider_events",
    "require_disposable_database_url",
    "run_odds_reconcile",
    "seed_canonical_card",
    "select_next_dwcs_event",
    "supersede_provider_aliases",
]
