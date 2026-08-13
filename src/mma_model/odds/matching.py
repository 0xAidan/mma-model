"""DWCS-203 odds event ↔ canonical bout matching."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any, Final

import yaml
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from mma_model.db.tables.core import (
    BoutSourceId,
    CanonicalBout,
    CanonicalEvent,
    CanonicalFighter,
    FighterAlias,
)
from mma_model.db.tables.odds import OddsProviderEventAlias
from mma_model.identity.constants import RULE_QUEUE_AMBIGUOUS
from mma_model.identity.models import ReviewCandidate
from mma_model.identity.normalize import normalize_person_name
from mma_model.identity.review import enqueue_review
from mma_model.odds.lifecycle import OddsBoutLifecycleState
from mma_model.odds.types import PROVIDER_THE_ODDS_API

MATCH_RULE_PROVIDER_ID: Final[str] = "provider_id"
MATCH_RULE_PARTICIPANT_PAIR: Final[str] = "participant_pair"
MATCH_STATUS_MATCHED: Final[str] = "matched"
MATCH_STATUS_UNMATCHED: Final[str] = "unmatched"
MATCH_STATUS_AMBIGUOUS: Final[str] = "ambiguous_blocked"

_INACTIVE_BOUT_STATUSES: Final[frozenset[str]] = frozenset(
    {"cancelled", "canceled", "replaced"}
)


class MatchingContract(BaseModel):
    """Pinned DWCS-203 matching contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_id: str
    contract_version: str
    schema_version: int
    ticket: str
    provider: str
    match_window_minutes: int
    stale_after_minutes: int
    match_rules: tuple[str, ...]
    match_statuses: tuple[str, ...]
    lifecycle_states: tuple[str, ...]
    prohibited: tuple[str, ...]
    notes: str = ""

    @field_validator(
        "match_rules",
        "match_statuses",
        "lifecycle_states",
        "prohibited",
        mode="before",
    )
    @classmethod
    def _tupleize(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        return tuple(str(item) for item in value)


@dataclass(frozen=True)
class OddsMatchDecision:
    provider: str
    external_event_id: str
    status: str
    bout_id: str | None
    match_rule: str | None
    reason: str
    lifecycle: OddsBoutLifecycleState
    eligible_for_value: bool
    review_id: str | None = None
    candidate_bout_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "external_event_id": self.external_event_id,
            "status": self.status,
            "bout_id": self.bout_id,
            "match_rule": self.match_rule,
            "reason": self.reason,
            "lifecycle": self.lifecycle.value,
            "eligible_for_value": self.eligible_for_value,
            "review_id": self.review_id,
            "candidate_bout_ids": list(self.candidate_bout_ids),
        }


def package_matching_resource_path() -> Path:
    root = resources.files("mma_model.odds")
    return Path(str(root.joinpath("matching_v1.yaml")))


@lru_cache(maxsize=1)
def load_matching_contract() -> MatchingContract:
    path = package_matching_resource_path()
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("matching contract must be a mapping")
    return MatchingContract.model_validate(payload)


def participant_names_equal(
    left: Sequence[str],
    right: Sequence[str],
) -> bool:
    """Exact normalized set equality; ignores corner/home-away order."""
    left_set = {normalize_person_name(name) for name in left if str(name).strip()}
    right_set = {normalize_person_name(name) for name in right if str(name).strip()}
    if len(left_set) != 2 or len(right_set) != 2:
        return False
    return left_set == right_set


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _within_window(
    scheduled_start: datetime,
    commence_time: datetime,
    *,
    max_delta_minutes: int,
) -> bool:
    left = _as_utc(scheduled_start)
    right = _as_utc(commence_time)
    delta = abs((left - right).total_seconds())
    return delta <= max_delta_minutes * 60


def _fighter_name_set(session: Session, fighter_id: str) -> set[str]:
    fighter = session.get(CanonicalFighter, fighter_id)
    if fighter is None:
        return set()
    names = {normalize_person_name(fighter.display_name)}
    aliases = session.scalars(
        select(FighterAlias.alias).where(FighterAlias.fighter_id == fighter_id)
    ).all()
    names.update(normalize_person_name(alias) for alias in aliases)
    return {name for name in names if name}


def _bout_covers_provider_names(
    session: Session,
    bout: CanonicalBout,
    home_team: str,
    away_team: str,
) -> bool:
    a_names = _fighter_name_set(session, bout.fighter_a_id)
    b_names = _fighter_name_set(session, bout.fighter_b_id)
    if not a_names or not b_names:
        return False
    home = normalize_person_name(home_team)
    away = normalize_person_name(away_team)
    if not home or not away or home == away:
        return False
    if home in a_names and away in b_names:
        return True
    return home in b_names and away in a_names


def _lookup_provider_id_bout(
    session: Session,
    *,
    provider: str,
    external_event_id: str,
) -> str | None:
    active_alias = session.scalar(
        select(OddsProviderEventAlias).where(
            OddsProviderEventAlias.provider == provider,
            OddsProviderEventAlias.external_event_id == external_event_id,
            OddsProviderEventAlias.status == "active",
        )
    )
    if active_alias is not None:
        return active_alias.bout_id
    source = session.scalar(
        select(BoutSourceId).where(
            BoutSourceId.source == provider,
            BoutSourceId.external_id == external_event_id,
        )
    )
    if source is not None:
        return source.bout_id
    return None


def _candidate_bouts_for_participants(
    session: Session,
    *,
    home_team: str,
    away_team: str,
    commence_time: datetime,
    max_delta_minutes: int,
) -> list[CanonicalBout]:
    bouts = session.scalars(select(CanonicalBout)).all()
    hits: list[CanonicalBout] = []
    for bout in bouts:
        if bout.status in _INACTIVE_BOUT_STATUSES:
            continue
        if not _bout_covers_provider_names(session, bout, home_team, away_team):
            continue
        event = session.get(CanonicalEvent, bout.event_id)
        if event is None or event.scheduled_start_at is None:
            continue
        if not _within_window(
            event.scheduled_start_at,
            commence_time,
            max_delta_minutes=max_delta_minutes,
        ):
            continue
        hits.append(bout)
    return hits


def match_provider_event(
    session: Session,
    *,
    provider: str,
    external_event_id: str,
    home_team: str,
    away_team: str,
    commence_time: datetime,
    contract: MatchingContract | None = None,
    observed_at: datetime | None = None,
) -> OddsMatchDecision:
    """Match a provider event to a canonical bout.

    Order: exact stored provider IDs, then exact participant pair inside the
    configured time window. Ambiguity never auto-merges.
    """
    cfg = contract or load_matching_contract()
    if provider not in {PROVIDER_THE_ODDS_API, cfg.provider}:
        raise ValueError(f"unsupported odds matching provider: {provider!r}")
    if commence_time.tzinfo is None:
        raise ValueError("commence_time must be timezone-aware")
    commence = commence_time.astimezone()

    stored = _lookup_provider_id_bout(
        session,
        provider=provider,
        external_event_id=external_event_id,
    )
    if stored is not None:
        return OddsMatchDecision(
            provider=provider,
            external_event_id=external_event_id,
            status=MATCH_STATUS_MATCHED,
            bout_id=stored,
            match_rule=MATCH_RULE_PROVIDER_ID,
            reason="exact stored provider event id",
            lifecycle=OddsBoutLifecycleState.ACTIVE,
            eligible_for_value=True,
        )

    home_n = normalize_person_name(home_team)
    away_n = normalize_person_name(away_team)
    if not home_n or not away_n or home_n == away_n:
        return OddsMatchDecision(
            provider=provider,
            external_event_id=external_event_id,
            status=MATCH_STATUS_AMBIGUOUS,
            bout_id=None,
            match_rule=None,
            reason="partial or invalid provider participant identity",
            lifecycle=OddsBoutLifecycleState.REVIEW_BLOCKED,
            eligible_for_value=False,
        )

    hits = _candidate_bouts_for_participants(
        session,
        home_team=home_team,
        away_team=away_team,
        commence_time=commence,
        max_delta_minutes=cfg.match_window_minutes,
    )
    if len(hits) == 1:
        return OddsMatchDecision(
            provider=provider,
            external_event_id=external_event_id,
            status=MATCH_STATUS_MATCHED,
            bout_id=hits[0].id,
            match_rule=MATCH_RULE_PARTICIPANT_PAIR,
            reason="unique participant pair within match window",
            lifecycle=OddsBoutLifecycleState.ACTIVE,
            eligible_for_value=True,
            candidate_bout_ids=(hits[0].id,),
        )
    if len(hits) > 1:
        candidate_ids = tuple(sorted(bout.id for bout in hits))
        review_id = enqueue_review(
            session,
            ReviewCandidate(
                source=provider,
                external_id=external_event_id,
                display_name=f"{home_team} vs {away_team}",
                normalized_name=normalize_person_name(f"{home_team} {away_team}"),
                candidate_canonical_ids=candidate_ids,
                rule_id=RULE_QUEUE_AMBIGUOUS,
                evidence={
                    "kind": "odds_event_ambiguous_bout_match",
                    "home_team": home_team,
                    "away_team": away_team,
                    "commence_time": commence.isoformat(),
                    "candidate_bout_ids": list(candidate_ids),
                },
                bout_status="scheduled",
            ),
            actor="odds_reconcile",
            now=observed_at,
        )
        return OddsMatchDecision(
            provider=provider,
            external_event_id=external_event_id,
            status=MATCH_STATUS_AMBIGUOUS,
            bout_id=None,
            match_rule=None,
            reason="multiple participant+time matches",
            lifecycle=OddsBoutLifecycleState.REVIEW_BLOCKED,
            eligible_for_value=False,
            review_id=review_id,
            candidate_bout_ids=candidate_ids,
        )

    return OddsMatchDecision(
        provider=provider,
        external_event_id=external_event_id,
        status=MATCH_STATUS_UNMATCHED,
        bout_id=None,
        match_rule=None,
        reason="no unique participant pair within match window",
        lifecycle=OddsBoutLifecycleState.MISSING_UNKNOWN,
        eligible_for_value=False,
    )


def decision_dedupe_key(decision: OddsMatchDecision, *, observed_at: datetime) -> str:
    stamp = observed_at.astimezone().isoformat()
    material = "|".join(
        [
            decision.provider,
            decision.external_event_id,
            decision.status,
            decision.bout_id or "",
            decision.match_rule or "",
            decision.reason,
            stamp,
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def dump_evidence(evidence: Mapping[str, Any]) -> str:
    return json.dumps(dict(evidence), sort_keys=True, allow_nan=False)
