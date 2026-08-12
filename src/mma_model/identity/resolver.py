"""Exact-ID / Wikidata-first deterministic identity resolver (DWCS-104)."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from mma_model.db.tables.core import (
    BoutParticipant,
    CanonicalBout,
    CanonicalEvent,
    CanonicalFighter,
    FighterProfileObservation,
    FighterSourceId,
)
from mma_model.db.tables.identity import IdentityMatchEvidence, IdentityScoringBlock
from mma_model.identity.constants import (
    ALLOWED_RESOLVE_SOURCES,
    RESOLVER_VERSION,
    RULE_BLOCKED,
    RULE_CREATE_NEW,
    RULE_EXACT_SOURCE_EXTERNAL_ID,
    RULE_EXACT_WIKIDATA,
    RULE_NAME_CONTEXT_UNIQUE,
    RULE_NAME_DOB_UNIQUE,
    RULE_QUEUE_AMBIGUOUS,
    RULE_QUEUE_CONFLICT,
    RULE_QUEUE_FUZZY,
    RULE_QUEUE_SAME_NAME,
)
from mma_model.identity.models import ResolveResult, ReviewCandidate, dump_evidence_json
from mma_model.identity.normalize import normalize_person_name
from mma_model.identity.review import enqueue_review
from mma_model.sources.policy import SourcePolicy, load_source_policy

# Re-export rule constants for tests.
__all__ = [
    "ALLOWED_RESOLVE_SOURCES",
    "IdentityResolver",
    "RESOLVER_VERSION",
    "RULE_BLOCKED",
    "RULE_CREATE_NEW",
    "RULE_EXACT_SOURCE_EXTERNAL_ID",
    "RULE_EXACT_WIKIDATA",
    "RULE_NAME_CONTEXT_UNIQUE",
    "RULE_NAME_DOB_UNIQUE",
    "RULE_QUEUE_AMBIGUOUS",
    "RULE_QUEUE_CONFLICT",
    "RULE_QUEUE_FUZZY",
    "RULE_QUEUE_SAME_NAME",
    "resolve_fighter",
]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _write_evidence(
    session: Session,
    *,
    action: str,
    rule_id: str,
    source: str,
    external_id: str,
    display_name: str,
    normalized_name: str,
    actor: str,
    evidence: dict[str, Any],
    wikidata_id: str | None = None,
    dob: date | None = None,
    before_canonical_id: str | None = None,
    after_canonical_id: str | None = None,
    review_id: str | None = None,
    bout_id: str | None = None,
    reversible: bool = True,
    now: datetime | None = None,
) -> IdentityMatchEvidence:
    row = IdentityMatchEvidence(
        id=str(uuid.uuid4()),
        created_at=now or _utc_now(),
        resolver_version=RESOLVER_VERSION,
        rule_id=rule_id,
        action=action,
        source=source,
        external_id=external_id,
        display_name=display_name,
        normalized_name=normalized_name,
        wikidata_id=wikidata_id,
        dob=dob,
        actor=actor,
        before_canonical_id=before_canonical_id,
        after_canonical_id=after_canonical_id,
        review_id=review_id,
        bout_id=bout_id,
        evidence_json=dump_evidence_json(evidence),
        reversible=reversible,
        status="active",
    )
    session.add(row)
    session.flush()
    return row


class IdentityResolver:
    """Session-bound resolver with deterministic auto-link priority."""

    def __init__(
        self,
        session: Session,
        *,
        actor: str = "system",
        now: datetime | None = None,
        policy: SourcePolicy | None = None,
    ) -> None:
        self.session = session
        self.actor = (actor or "").strip() or "system"
        self.now = now or _utc_now()
        self.policy = policy if policy is not None else load_source_policy()

    def is_bout_scoring_blocked(self, bout_id: str) -> bool:
        row = self.session.scalar(
            select(IdentityScoringBlock).where(
                IdentityScoringBlock.bout_id == bout_id,
                IdentityScoringBlock.active.is_(True),
            )
        )
        return row is not None

    def resolve_fighter(
        self,
        *,
        source: str,
        external_id: str,
        display_name: str,
        wikidata_id: str | None = None,
        dob: date | None = None,
        opponent_normalized_name: str | None = None,
        event_id: str | None = None,
        event_date: date | None = None,
        bout_id: str | None = None,
        bout_status: str | None = None,
        candidate_hints: Sequence[str] = (),
        create_if_absent: bool = True,
    ) -> ResolveResult:
        source = (source or "").strip()
        external_id = (external_id or "").strip()
        display_name = (display_name or "").strip()
        if source not in ALLOWED_RESOLVE_SOURCES:
            raise ValueError(f"unknown or disallowed source: {source!r}")
        if not external_id or not display_name:
            raise ValueError("external_id and display_name are required")
        if not self.actor:
            raise ValueError("actor is required")

        rules = self.policy.identity_rules
        if rules.same_name_auto_merge:
            raise ValueError("same_name_auto_merge is forbidden; fail closed")
        if rules.fuzzy_or_transliteration != "candidates_only_in_reversible_review_queue":
            raise ValueError(
                "fuzzy_or_transliteration must be "
                "candidates_only_in_reversible_review_queue; fail closed"
            )

        normalized = normalize_person_name(display_name)
        hints = tuple(sorted({h.strip() for h in candidate_hints if h and h.strip()}))

        # 1) Exact (source, external_id) crosswalk.
        exact = self.session.scalar(
            select(FighterSourceId).where(
                FighterSourceId.source == source,
                FighterSourceId.external_id == external_id,
            )
        )
        if exact is not None:
            if not rules.exact_source_ids_first:
                return self._queue(
                    rule_id=RULE_QUEUE_CONFLICT,
                    source=source,
                    external_id=external_id,
                    display_name=display_name,
                    normalized_name=normalized,
                    wikidata_id=wikidata_id,
                    dob=dob,
                    bout_id=bout_id,
                    bout_status=bout_status,
                    candidates=(exact.fighter_id,),
                    evidence={
                        "reason": "exact_source_ids_first_disabled",
                        "source": source,
                        "external_id": external_id,
                    },
                )
            fighter = self.session.get(CanonicalFighter, exact.fighter_id)
            stored_norm = (
                normalize_person_name(fighter.display_name) if fighter is not None else ""
            )
            if stored_norm != normalized:
                return self._queue(
                    rule_id=RULE_QUEUE_CONFLICT,
                    source=source,
                    external_id=external_id,
                    display_name=display_name,
                    normalized_name=normalized,
                    wikidata_id=wikidata_id,
                    dob=dob,
                    bout_id=bout_id,
                    bout_status=bout_status,
                    candidates=(exact.fighter_id,),
                    evidence={
                        "reason": "duplicate_external_id",
                        "duplicate_external_id": True,
                        "source": source,
                        "external_id": external_id,
                        "stored_display_name": (
                            fighter.display_name if fighter is not None else None
                        ),
                        "stored_normalized_name": stored_norm,
                        "incoming_display_name": display_name,
                        "incoming_normalized_name": normalized,
                    },
                )
            return self._linked(
                rule_id=RULE_EXACT_SOURCE_EXTERNAL_ID,
                source=source,
                external_id=external_id,
                display_name=display_name,
                normalized_name=normalized,
                canonical_id=exact.fighter_id,
                wikidata_id=wikidata_id,
                dob=dob,
                bout_id=bout_id,
                evidence={
                    "match": "exact_source_external_id",
                    "source": source,
                    "external_id": external_id,
                    "display_name_original": display_name,
                    "normalized_name": normalized,
                },
            )

        # 2) Exact Wikidata crosswalk.
        if wikidata_id and rules.wikidata_crosswalk_first:
            wiki_rows = list(
                self.session.scalars(
                    select(FighterSourceId).where(
                        FighterSourceId.source == "wikidata",
                        FighterSourceId.external_id == wikidata_id.strip(),
                    )
                ).all()
            )
            wiki_fighter_ids = sorted({r.fighter_id for r in wiki_rows})
            if len(wiki_fighter_ids) == 1:
                wiki_fighter = wiki_fighter_ids[0]
                if dob is not None:
                    name_dob_matches = [
                        fid
                        for fid in self._fighters_by_normalized_name(normalized)
                        if self._fighter_dob(fid) == dob
                    ]
                    if len(name_dob_matches) == 1 and name_dob_matches[0] != wiki_fighter:
                        return self._queue(
                            rule_id=RULE_QUEUE_CONFLICT,
                            source=source,
                            external_id=external_id,
                            display_name=display_name,
                            normalized_name=normalized,
                            wikidata_id=wikidata_id,
                            dob=dob,
                            bout_id=bout_id,
                            bout_status=bout_status,
                            candidates=tuple(sorted({wiki_fighter, name_dob_matches[0]})),
                            evidence={
                                "reason": "wikidata_conflicts_with_name_dob",
                                "wikidata_id": wikidata_id,
                                "wikidata_fighter_id": wiki_fighter,
                                "name_dob_fighter_id": name_dob_matches[0],
                            },
                        )
                linked = self._link_source(
                    fighter_id=wiki_fighter,
                    source=source,
                    external_id=external_id,
                )
                return self._linked(
                    rule_id=RULE_EXACT_WIKIDATA,
                    source=source,
                    external_id=external_id,
                    display_name=display_name,
                    normalized_name=normalized,
                    canonical_id=linked.fighter_id,
                    wikidata_id=wikidata_id,
                    dob=dob,
                    bout_id=bout_id,
                    evidence={
                        "match": "exact_wikidata",
                        "wikidata_id": wikidata_id,
                        "display_name_original": display_name,
                    },
                )
            if len(wiki_fighter_ids) > 1:
                return self._queue(
                    rule_id=RULE_QUEUE_CONFLICT,
                    source=source,
                    external_id=external_id,
                    display_name=display_name,
                    normalized_name=normalized,
                    wikidata_id=wikidata_id,
                    dob=dob,
                    bout_id=bout_id,
                    bout_status=bout_status,
                    candidates=tuple(wiki_fighter_ids),
                    evidence={
                        "reason": "wikidata_conflict",
                        "wikidata_id": wikidata_id,
                        "fighter_ids": wiki_fighter_ids,
                    },
                )

        # Fuzzy / nickname / reordered / transliteration never auto-merge.
        # Exact source ID and Wikidata already won above.
        if set(hints) & {"nickname", "reordered", "transliterated", "fuzzy", "alias"}:
            return self._queue(
                rule_id=RULE_QUEUE_FUZZY,
                source=source,
                external_id=external_id,
                display_name=display_name,
                normalized_name=normalized,
                wikidata_id=wikidata_id,
                dob=dob,
                bout_id=bout_id,
                bout_status=bout_status,
                candidates=self._candidates_by_normalized_name(normalized),
                evidence={
                    "reason": "candidate_hints_forbid_auto_link",
                    "candidate_hints": list(hints),
                    "display_name_original": display_name,
                    "normalized_name": normalized,
                },
            )

        # 3) Exact normalized name + exact DOB when unique/nonconflicting.
        if dob is not None:
            name_matches = self._fighters_by_normalized_name(normalized)
            dob_matches = [
                fid for fid in name_matches if self._fighter_dob(fid) == dob
            ]
            different_dob = [
                fid
                for fid in name_matches
                if self._fighter_dob(fid) is not None and self._fighter_dob(fid) != dob
            ]
            if len(dob_matches) == 1:
                # Unique name+DOB wins even when other same-name people have other DOBs.
                linked = self._link_source(
                    fighter_id=dob_matches[0], source=source, external_id=external_id
                )
                return self._linked(
                    rule_id=RULE_NAME_DOB_UNIQUE,
                    source=source,
                    external_id=external_id,
                    display_name=display_name,
                    normalized_name=normalized,
                    canonical_id=linked.fighter_id,
                    wikidata_id=wikidata_id,
                    dob=dob,
                    bout_id=bout_id,
                    evidence={
                        "match": "exact_normalized_name_dob_unique",
                        "dob": dob.isoformat(),
                        "display_name_original": display_name,
                        "other_same_name_different_dob": sorted(different_dob),
                    },
                )
            if len(dob_matches) > 1:
                return self._queue(
                    rule_id=RULE_QUEUE_AMBIGUOUS,
                    source=source,
                    external_id=external_id,
                    display_name=display_name,
                    normalized_name=normalized,
                    wikidata_id=wikidata_id,
                    dob=dob,
                    bout_id=bout_id,
                    bout_status=bout_status,
                    candidates=tuple(sorted(dob_matches)),
                    evidence={"reason": "name_dob_not_unique", "dob": dob.isoformat()},
                )
            if different_dob and not dob_matches:
                return self._queue(
                    rule_id=RULE_QUEUE_CONFLICT,
                    source=source,
                    external_id=external_id,
                    display_name=display_name,
                    normalized_name=normalized,
                    wikidata_id=wikidata_id,
                    dob=dob,
                    bout_id=bout_id,
                    bout_status=bout_status,
                    candidates=tuple(sorted(set(name_matches))),
                    evidence={
                        "reason": "dob_conflict",
                        "dob": dob.isoformat(),
                        "conflicting_fighter_ids": sorted(different_dob),
                    },
                )

        # 4) Exact normalized name + opponent/event/date context when uniquely consistent.
        if opponent_normalized_name:
            context_ids = self._context_matches(
                normalized_name=normalized,
                opponent_normalized_name=normalize_person_name(opponent_normalized_name),
                event_id=event_id,
                event_date=event_date,
            )
            if len(context_ids) == 1:
                linked = self._link_source(
                    fighter_id=context_ids[0], source=source, external_id=external_id
                )
                return self._linked(
                    rule_id=RULE_NAME_CONTEXT_UNIQUE,
                    source=source,
                    external_id=external_id,
                    display_name=display_name,
                    normalized_name=normalized,
                    canonical_id=linked.fighter_id,
                    wikidata_id=wikidata_id,
                    dob=dob,
                    bout_id=bout_id,
                    evidence={
                        "match": "exact_normalized_name_opponent_event_date_unique",
                        "opponent_normalized_name": normalize_person_name(
                            opponent_normalized_name
                        ),
                        "event_id": event_id,
                        "event_date": event_date.isoformat() if event_date else None,
                    },
                )
            if len(context_ids) > 1:
                return self._queue(
                    rule_id=RULE_QUEUE_AMBIGUOUS,
                    source=source,
                    external_id=external_id,
                    display_name=display_name,
                    normalized_name=normalized,
                    wikidata_id=wikidata_id,
                    dob=dob,
                    bout_id=bout_id,
                    bout_status=bout_status,
                    candidates=tuple(context_ids),
                    evidence={"reason": "context_not_unique"},
                )

        # 5) Same normalized name alone always queues when any existing person matches.
        same_name = self._fighters_by_normalized_name(normalized)
        if same_name:
            return self._queue(
                rule_id=RULE_QUEUE_SAME_NAME,
                source=source,
                external_id=external_id,
                display_name=display_name,
                normalized_name=normalized,
                wikidata_id=wikidata_id,
                dob=dob,
                bout_id=bout_id,
                bout_status=bout_status,
                candidates=tuple(sorted(same_name)),
                evidence={
                    "reason": "same_normalized_name_without_unique_key",
                    "display_name_original": display_name,
                    "normalized_name": normalized,
                    "diacritics_preserved": True,
                },
            )

        # No existing person: create new canonical for this external identity.
        if not create_if_absent:
            return self._queue(
                rule_id=RULE_QUEUE_AMBIGUOUS,
                source=source,
                external_id=external_id,
                display_name=display_name,
                normalized_name=normalized,
                wikidata_id=wikidata_id,
                dob=dob,
                bout_id=bout_id,
                bout_status=bout_status,
                candidates=(),
                evidence={"reason": "create_disabled"},
            )
        fighter_id = str(uuid.uuid4())
        self.session.add(
            CanonicalFighter(
                id=fighter_id,
                display_name=display_name,
                created_at=self.now,
                updated_at=self.now,
            )
        )
        self.session.flush()
        self._link_source(fighter_id=fighter_id, source=source, external_id=external_id)
        if wikidata_id:
            existing_wiki = self.session.scalar(
                select(FighterSourceId).where(
                    FighterSourceId.source == "wikidata",
                    FighterSourceId.external_id == wikidata_id.strip(),
                )
            )
            if existing_wiki is None:
                self.session.add(
                    FighterSourceId(
                        fighter_id=fighter_id,
                        source="wikidata",
                        external_id=wikidata_id.strip(),
                    )
                )
        if dob is not None:
            self.session.add(
                FighterProfileObservation(
                    fighter_id=fighter_id,
                    attribute="dob",
                    value_date=dob,
                    source=source,
                    effective_at=self.now,
                    observed_at=self.now,
                )
            )
        evidence = _write_evidence(
            self.session,
            action="created",
            rule_id=RULE_CREATE_NEW,
            source=source,
            external_id=external_id,
            display_name=display_name,
            normalized_name=normalized,
            actor=self.actor,
            evidence={
                "created_canonical_id": fighter_id,
                "display_name_original": display_name,
            },
            wikidata_id=wikidata_id,
            dob=dob,
            before_canonical_id=None,
            after_canonical_id=fighter_id,
            bout_id=bout_id,
            now=self.now,
        )
        return ResolveResult(
            kind="created",
            canonical_id=fighter_id,
            review_id=None,
            evidence_id=evidence.id,
            rule_id=RULE_CREATE_NEW,
            resolver_version=RESOLVER_VERSION,
            reversible=True,
        )

    def _link_source(
        self, *, fighter_id: str, source: str, external_id: str
    ) -> FighterSourceId:
        existing = self.session.scalar(
            select(FighterSourceId).where(
                FighterSourceId.source == source,
                FighterSourceId.external_id == external_id,
            )
        )
        if existing is not None:
            return existing
        row = FighterSourceId(
            fighter_id=fighter_id, source=source, external_id=external_id
        )
        self.session.add(row)
        self.session.flush()
        return row

    def _fighters_by_normalized_name(self, normalized: str) -> list[str]:
        fighters = self.session.scalars(select(CanonicalFighter)).all()
        return sorted(
            f.id
            for f in fighters
            if normalize_person_name(f.display_name) == normalized
        )

    def _candidates_by_normalized_name(self, normalized: str) -> tuple[str, ...]:
        return tuple(self._fighters_by_normalized_name(normalized))

    def _fighter_dob(self, fighter_id: str) -> date | None:
        row = self.session.scalar(
            select(FighterProfileObservation)
            .where(
                FighterProfileObservation.fighter_id == fighter_id,
                FighterProfileObservation.attribute == "dob",
            )
            .order_by(FighterProfileObservation.observed_at.desc())
        )
        return row.value_date if row is not None else None

    def _context_matches(
        self,
        *,
        normalized_name: str,
        opponent_normalized_name: str,
        event_id: str | None,
        event_date: date | None,
    ) -> list[str]:
        bouts = self.session.scalars(select(CanonicalBout)).all()
        matched: set[str] = set()
        for bout in bouts:
            event = self.session.get(CanonicalEvent, bout.event_id)
            if event_id is not None and bout.event_id != event_id:
                continue
            if event_date is not None and (event is None or event.event_date != event_date):
                continue
            participants = list(
                self.session.scalars(
                    select(BoutParticipant).where(BoutParticipant.bout_id == bout.id)
                ).all()
            )
            if len(participants) != 2:
                continue
            names: dict[str, str] = {}
            for part in participants:
                fighter = self.session.get(CanonicalFighter, part.fighter_id)
                if fighter is None:
                    continue
                names[part.fighter_id] = normalize_person_name(fighter.display_name)
            if len(names) != 2:
                continue
            values = list(names.items())
            for fighter_id, name in values:
                other_names = [n for fid, n in values if fid != fighter_id]
                if name == normalized_name and opponent_normalized_name in other_names:
                    # Require event_id or event_date for uniqueness gate.
                    if event_id is None and event_date is None:
                        continue
                    matched.add(fighter_id)
        return sorted(matched)

    def _linked(
        self,
        *,
        rule_id: str,
        source: str,
        external_id: str,
        display_name: str,
        normalized_name: str,
        canonical_id: str,
        wikidata_id: str | None,
        dob: date | None,
        bout_id: str | None,
        evidence: dict[str, Any],
    ) -> ResolveResult:
        row = _write_evidence(
            self.session,
            action="linked",
            rule_id=rule_id,
            source=source,
            external_id=external_id,
            display_name=display_name,
            normalized_name=normalized_name,
            actor=self.actor,
            evidence=evidence,
            wikidata_id=wikidata_id,
            dob=dob,
            before_canonical_id=None,
            after_canonical_id=canonical_id,
            bout_id=bout_id,
            now=self.now,
        )
        return ResolveResult(
            kind="linked",
            canonical_id=canonical_id,
            review_id=None,
            evidence_id=row.id,
            rule_id=rule_id,
            resolver_version=RESOLVER_VERSION,
            reversible=True,
        )

    def _queue(
        self,
        *,
        rule_id: str,
        source: str,
        external_id: str,
        display_name: str,
        normalized_name: str,
        wikidata_id: str | None,
        dob: date | None,
        bout_id: str | None,
        bout_status: str | None,
        candidates: Sequence[str],
        evidence: dict[str, Any],
    ) -> ResolveResult:
        ordered = tuple(sorted({c for c in candidates if c}))
        review_id = enqueue_review(
            self.session,
            ReviewCandidate(
                source=source,
                external_id=external_id,
                display_name=display_name,
                normalized_name=normalized_name,
                candidate_canonical_ids=ordered,
                rule_id=rule_id,
                evidence=evidence,
                wikidata_id=wikidata_id,
                dob=dob,
                bout_id=bout_id,
                bout_status=bout_status,
            ),
            actor=self.actor,
            now=self.now,
        )
        # enqueue_review already wrote queued evidence; fetch latest for id.
        evidence_row = self.session.scalar(
            select(IdentityMatchEvidence)
            .where(IdentityMatchEvidence.review_id == review_id)
            .order_by(IdentityMatchEvidence.created_at.desc())
        )
        kind = "queued"
        if bout_id and (bout_status or "") in {"upcoming", "evaluated", "scheduled"}:
            if self.is_bout_scoring_blocked(bout_id):
                kind = "blocked"
                _write_evidence(
                    self.session,
                    action="blocked",
                    rule_id=RULE_BLOCKED,
                    source=source,
                    external_id=external_id,
                    display_name=display_name,
                    normalized_name=normalized_name,
                    actor=self.actor,
                    evidence={"bout_id": bout_id, "bout_status": bout_status},
                    wikidata_id=wikidata_id,
                    dob=dob,
                    review_id=review_id,
                    bout_id=bout_id,
                    now=self.now,
                )
        return ResolveResult(
            kind=kind,
            canonical_id=None,
            review_id=review_id,
            evidence_id=evidence_row.id if evidence_row is not None else "",
            rule_id=rule_id,
            resolver_version=RESOLVER_VERSION,
            reversible=True,
        )


def resolve_fighter(
    session: Session,
    *,
    source: str,
    external_id: str,
    display_name: str,
    wikidata_id: str | None = None,
    dob: date | None = None,
    opponent_normalized_name: str | None = None,
    event_id: str | None = None,
    event_date: date | None = None,
    bout_id: str | None = None,
    bout_status: str | None = None,
    candidate_hints: Sequence[str] = (),
    actor: str = "system",
    now: datetime | None = None,
    create_if_absent: bool = True,
    policy: SourcePolicy | None = None,
) -> ResolveResult:
    return IdentityResolver(session, actor=actor, now=now, policy=policy).resolve_fighter(
        source=source,
        external_id=external_id,
        display_name=display_name,
        wikidata_id=wikidata_id,
        dob=dob,
        opponent_normalized_name=opponent_normalized_name,
        event_id=event_id,
        event_date=event_date,
        bout_id=bout_id,
        bout_status=bout_status,
        candidate_hints=candidate_hints,
        create_if_absent=create_if_absent,
    )
