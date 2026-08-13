"""DWCS-203 odds event ↔ canonical bout matching (hardened)."""

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
from pydantic import BaseModel, ConfigDict, Field, field_validator
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
from mma_model.identity.normalize import normalize_person_name
from mma_model.odds.lifecycle import OddsBoutLifecycleState, resolve_match_lifecycle
from mma_model.odds.types import PROVIDER_THE_ODDS_API

MATCH_RULE_PROVIDER_ID: Final[str] = "provider_id"
MATCH_RULE_PARTICIPANT_PAIR: Final[str] = "participant_pair"
MATCH_STATUS_MATCHED: Final[str] = "matched"
MATCH_STATUS_UNMATCHED: Final[str] = "unmatched"
MATCH_STATUS_AMBIGUOUS: Final[str] = "ambiguous_blocked"

MATCHING_CONTRACT_ID: Final[str] = "dwcs_odds_matching"
EXPECTED_MATCHING_CONTRACT_VERSION: Final[str] = "1.0.0"
EXPECTED_MATCHING_SCHEMA_VERSION: Final[int] = 1
MATCHING_FILENAME: Final[str] = "matching_v1.yaml"
PINNED_MATCHING_CONTRACT_HASH: Final[str] = (
    "ada7d61aea144b2691f38529735b8cf07b6af358b96d0b9e20b7c3417332a982"
)

_INACTIVE_BOUT_STATUSES: Final[frozenset[str]] = frozenset(
    {"cancelled", "canceled", "replaced"}
)
_ACTIVE_BOUT_STATUSES: Final[frozenset[str]] = frozenset(
    {"scheduled", "upcoming", "occurred", "completed"}
)
_DWCS_SERIES: Final[frozenset[str]] = frozenset({"dwcs", "dwcs_brazil"})
_REQUIRED_MATCH_RULES: Final[tuple[str, ...]] = (
    MATCH_RULE_PROVIDER_ID,
    MATCH_RULE_PARTICIPANT_PAIR,
)
_REQUIRED_MATCH_STATUSES: Final[tuple[str, ...]] = (
    MATCH_STATUS_MATCHED,
    MATCH_STATUS_UNMATCHED,
    MATCH_STATUS_AMBIGUOUS,
)
_REQUIRED_LIFECYCLES: Final[tuple[str, ...]] = (
    "active",
    "stale",
    "missing_unknown",
    "locked",
    "cancelled",
    "replaced",
    "review_blocked",
)


class MatchingContractError(RuntimeError):
    """Invalid or drifted matching contract."""


class MatchingContractHashMismatch(MatchingContractError):
    """Pinned digest mismatch."""


class MatchingContract(BaseModel):
    """Pinned DWCS-203 matching contract (deeply immutable)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_id: str
    contract_version: str
    schema_version: int
    ticket: str
    provider: str
    match_window_minutes: int = Field(gt=0)
    stale_after_minutes: int = Field(gt=0)
    match_rules: tuple[str, ...]
    match_statuses: tuple[str, ...]
    lifecycle_states: tuple[str, ...]
    prohibited: tuple[str, ...]
    notes: str = ""
    content_hash: str = Field(min_length=64, max_length=64)

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
    target = root.joinpath(MATCHING_FILENAME)
    with resources.as_file(target) as path:
        return Path(path)


def visible_matching_path() -> Path:
    return Path(__file__).resolve().parents[3] / "config" / "odds" / "matching_v1.yaml"


def compute_matching_contract_hash(payload: Mapping[str, Any]) -> str:
    material = {
        k: v for k, v in dict(payload).items() if k != "content_hash"
    }
    raw = json.dumps(
        material, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _validate_contract_payload(payload: Mapping[str, Any]) -> MatchingContract:
    content_hash = compute_matching_contract_hash(payload)
    if content_hash != PINNED_MATCHING_CONTRACT_HASH:
        raise MatchingContractHashMismatch(
            f"content hash mismatch versus pinned digest: got {content_hash}, "
            f"expected {PINNED_MATCHING_CONTRACT_HASH}"
        )
    if str(payload.get("contract_id") or "") != MATCHING_CONTRACT_ID:
        raise MatchingContractError(
            f"unexpected contract_id {payload.get('contract_id')!r}"
        )
    if str(payload.get("contract_version") or "") != EXPECTED_MATCHING_CONTRACT_VERSION:
        raise MatchingContractError(
            f"unexpected contract_version {payload.get('contract_version')!r}"
        )
    if int(payload.get("schema_version") or 0) != EXPECTED_MATCHING_SCHEMA_VERSION:
        raise MatchingContractError(
            f"unexpected schema_version {payload.get('schema_version')!r}"
        )
    if str(payload.get("ticket") or "") != "DWCS-203":
        raise MatchingContractError(f"unexpected ticket {payload.get('ticket')!r}")
    if str(payload.get("provider") or "") != PROVIDER_THE_ODDS_API:
        raise MatchingContractError(f"unexpected provider {payload.get('provider')!r}")

    rules = tuple(str(x) for x in (payload.get("match_rules") or ()))
    statuses = tuple(str(x) for x in (payload.get("match_statuses") or ()))
    lifecycles = tuple(str(x) for x in (payload.get("lifecycle_states") or ()))
    if rules != _REQUIRED_MATCH_RULES:
        raise MatchingContractError(f"match_rules drift: {rules!r}")
    if statuses != _REQUIRED_MATCH_STATUSES:
        raise MatchingContractError(f"match_statuses drift: {statuses!r}")
    if lifecycles != _REQUIRED_LIFECYCLES:
        raise MatchingContractError(f"lifecycle_states drift: {lifecycles!r}")
    if int(payload.get("match_window_minutes") or 0) <= 0:
        raise MatchingContractError("match_window_minutes must be > 0")
    if int(payload.get("stale_after_minutes") or 0) <= 0:
        raise MatchingContractError("stale_after_minutes must be > 0")

    return MatchingContract.model_validate({**dict(payload), "content_hash": content_hash})


@lru_cache(maxsize=1)
def load_matching_contract() -> MatchingContract:
    path = package_matching_resource_path()
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise MatchingContractError(f"unable to read matching contract: {exc}") from exc
    if not isinstance(payload, dict):
        raise MatchingContractError("matching contract must be a mapping")
    return _validate_contract_payload(payload)


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


def require_aware_utc(value: datetime, *, field: str) -> datetime:
    """Reject naive external inputs; normalize aware values to UTC."""
    if value.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware UTC (naive rejected)")
    return value.astimezone(UTC)


def as_utc_sqlite(value: datetime) -> datetime:
    """Normalize DB-read timestamps: naive SQLite values are treated as UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _within_window(
    scheduled_start: datetime,
    commence_time: datetime,
    *,
    max_delta_minutes: int,
) -> bool:
    left = as_utc_sqlite(scheduled_start)
    right = as_utc_sqlite(commence_time)
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


def bout_covers_provider_names(
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


def _event_is_dwcs(event: CanonicalEvent | None) -> bool:
    if event is None:
        return False
    series = (event.series or "").strip().lower()
    return series in _DWCS_SERIES


def _bout_is_active(bout: CanonicalBout) -> bool:
    return bout.status not in _INACTIVE_BOUT_STATUSES and (
        bout.status in _ACTIVE_BOUT_STATUSES or bout.status not in _INACTIVE_BOUT_STATUSES
    )


def validate_linked_bout(
    session: Session,
    *,
    bout_id: str,
    home_team: str,
    away_team: str,
    commence_time: datetime,
    max_delta_minutes: int,
    require_dwcs: bool = True,
) -> tuple[bool, str]:
    """Return (ok, reason) for a provider-ID candidate bout."""
    bout = session.get(CanonicalBout, bout_id)
    if bout is None:
        return False, "linked bout missing"
    if bout.status in _INACTIVE_BOUT_STATUSES:
        return False, f"linked bout status={bout.status}"
    if not _bout_is_active(bout):
        return False, f"linked bout not active ({bout.status})"
    event = session.get(CanonicalEvent, bout.event_id)
    if event is None:
        return False, "linked event missing"
    if require_dwcs and not _event_is_dwcs(event):
        return False, f"linked event series not DWCS ({event.series!r})"
    if event.scheduled_start_at is None:
        return False, "linked event missing scheduled_start_at"
    if not bout_covers_provider_names(session, bout, home_team, away_team):
        return False, "provider participants no longer match linked bout"
    if not _within_window(
        event.scheduled_start_at,
        commence_time,
        max_delta_minutes=max_delta_minutes,
    ):
        return False, "linked bout outside match window"
    return True, "ok"


def _lookup_provider_id_candidate(
    session: Session,
    *,
    provider: str,
    external_event_id: str,
) -> tuple[str | None, str | None]:
    """Return (bout_id, source_kind) for stored provider ID. Alias wins over BoutSourceId."""
    active_alias = session.scalar(
        select(OddsProviderEventAlias).where(
            OddsProviderEventAlias.provider == provider,
            OddsProviderEventAlias.external_event_id == external_event_id,
            OddsProviderEventAlias.status == "active",
        )
    )
    if active_alias is not None:
        return active_alias.bout_id, "active_alias"
    source = session.scalar(
        select(BoutSourceId).where(
            BoutSourceId.source == provider,
            BoutSourceId.external_id == external_event_id,
        )
    )
    if source is not None:
        return source.bout_id, "bout_source_id"
    return None, None


def _candidate_bouts_for_participants(
    session: Session,
    *,
    home_team: str,
    away_team: str,
    commence_time: datetime,
    max_delta_minutes: int,
    require_dwcs: bool = True,
    event_ids: Sequence[str] | None = None,
) -> list[CanonicalBout]:
    """Scope to active DWCS events in the time window, then exact participants."""
    commence = as_utc_sqlite(commence_time)
    event_stmt = select(CanonicalEvent).where(
        CanonicalEvent.scheduled_start_at.is_not(None)
    )
    if require_dwcs:
        event_stmt = event_stmt.where(CanonicalEvent.series.in_(tuple(_DWCS_SERIES)))
    if event_ids is not None:
        event_stmt = event_stmt.where(CanonicalEvent.id.in_(tuple(event_ids)))

    scoped_event_ids = [
        event.id
        for event in session.scalars(event_stmt).all()
        if event.scheduled_start_at is not None
        and _within_window(
            event.scheduled_start_at,
            commence,
            max_delta_minutes=max_delta_minutes,
        )
        and (not require_dwcs or _event_is_dwcs(event))
    ]
    if not scoped_event_ids:
        return []

    bouts = session.scalars(
        select(CanonicalBout).where(
            CanonicalBout.event_id.in_(scoped_event_ids),
            CanonicalBout.status.notin_(tuple(_INACTIVE_BOUT_STATUSES)),
        )
    ).all()
    return [
        bout
        for bout in bouts
        if bout_covers_provider_names(session, bout, home_team, away_team)
    ]


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
    require_dwcs: bool = True,
    event_ids: Sequence[str] | None = None,
    enqueue_ambiguous_review: bool = True,
) -> OddsMatchDecision:
    """Match a provider event to a canonical bout with safety checks.

    Stored provider IDs are a strong candidate only: the linked bout must exist,
    be active DWCS, match both participants (order-independent), and lie inside
    the configured time window. Failures block; they never stay value-eligible.
    """
    # Local import avoids circular dependency at module import time.
    from mma_model.odds.match_review import enqueue_bout_match_review

    cfg = contract or load_matching_contract()
    if provider not in {PROVIDER_THE_ODDS_API, cfg.provider}:
        raise ValueError(f"unsupported odds matching provider: {provider!r}")
    commence = require_aware_utc(commence_time, field="commence_time")
    stamp = (
        require_aware_utc(observed_at, field="observed_at")
        if observed_at is not None
        else datetime.now(UTC)
    )

    stored_bout_id, stored_kind = _lookup_provider_id_candidate(
        session,
        provider=provider,
        external_event_id=external_event_id,
    )
    if stored_bout_id is not None:
        ok, reason = validate_linked_bout(
            session,
            bout_id=stored_bout_id,
            home_team=home_team,
            away_team=away_team,
            commence_time=commence,
            max_delta_minutes=cfg.match_window_minutes,
            require_dwcs=require_dwcs,
        )
        if ok:
            lifecycle, eligible = resolve_match_lifecycle(
                session,
                bout_id=stored_bout_id,
                provider=provider,
                external_event_id=external_event_id,
                observed_at=stamp,
                stale_after_minutes=cfg.stale_after_minutes,
            )
            return OddsMatchDecision(
                provider=provider,
                external_event_id=external_event_id,
                status=MATCH_STATUS_MATCHED,
                bout_id=stored_bout_id,
                match_rule=MATCH_RULE_PROVIDER_ID,
                reason=f"exact stored provider event id ({stored_kind})",
                lifecycle=lifecycle,
                eligible_for_value=eligible,
                candidate_bout_ids=(stored_bout_id,),
            )
        # Immutable BoutSourceId may still point at a cancelled/replaced bout after
        # same-ID replacement. That ID is no longer authority — fall through to
        # participant matching. Participant/series/window mismatches on an active
        # linked bout remain hard review blocks.
        inactive_authority = reason.startswith(
            ("linked bout missing", "linked bout status=", "linked bout not active")
        )
        if not inactive_authority:
            review_id = None
            if enqueue_ambiguous_review:
                review_id = enqueue_bout_match_review(
                    session,
                    provider=provider,
                    external_event_id=external_event_id,
                    home_team=home_team,
                    away_team=away_team,
                    commence_time=commence,
                    candidate_bout_ids=(stored_bout_id,),
                    reason=f"stored provider id unsafe: {reason}",
                    observed_at=stamp,
                )
            return OddsMatchDecision(
                provider=provider,
                external_event_id=external_event_id,
                status=MATCH_STATUS_AMBIGUOUS,
                bout_id=None,
                match_rule=None,
                reason=f"stored provider id unsafe: {reason}",
                lifecycle=OddsBoutLifecycleState.REVIEW_BLOCKED,
                eligible_for_value=False,
                review_id=review_id,
                candidate_bout_ids=(stored_bout_id,),
            )

    home_n = normalize_person_name(home_team)
    away_n = normalize_person_name(away_team)
    if not home_n or not away_n or home_n == away_n:
        review_id = None
        if enqueue_ambiguous_review:
            review_id = enqueue_bout_match_review(
                session,
                provider=provider,
                external_event_id=external_event_id,
                home_team=home_team,
                away_team=away_team,
                commence_time=commence,
                candidate_bout_ids=(),
                reason="partial or invalid provider participant identity",
                observed_at=stamp,
            )
        return OddsMatchDecision(
            provider=provider,
            external_event_id=external_event_id,
            status=MATCH_STATUS_AMBIGUOUS,
            bout_id=None,
            match_rule=None,
            reason="partial or invalid provider participant identity",
            lifecycle=OddsBoutLifecycleState.REVIEW_BLOCKED,
            eligible_for_value=False,
            review_id=review_id,
        )

    hits = _candidate_bouts_for_participants(
        session,
        home_team=home_team,
        away_team=away_team,
        commence_time=commence,
        max_delta_minutes=cfg.match_window_minutes,
        require_dwcs=require_dwcs,
        event_ids=event_ids,
    )
    if len(hits) == 1:
        bout_id = hits[0].id
        lifecycle, eligible = resolve_match_lifecycle(
            session,
            bout_id=bout_id,
            provider=provider,
            external_event_id=external_event_id,
            observed_at=stamp,
            stale_after_minutes=cfg.stale_after_minutes,
        )
        return OddsMatchDecision(
            provider=provider,
            external_event_id=external_event_id,
            status=MATCH_STATUS_MATCHED,
            bout_id=bout_id,
            match_rule=MATCH_RULE_PARTICIPANT_PAIR,
            reason="unique participant pair within match window",
            lifecycle=lifecycle,
            eligible_for_value=eligible,
            candidate_bout_ids=(bout_id,),
        )
    if len(hits) > 1:
        candidate_ids = tuple(sorted(bout.id for bout in hits))
        review_id = None
        if enqueue_ambiguous_review:
            review_id = enqueue_bout_match_review(
                session,
                provider=provider,
                external_event_id=external_event_id,
                home_team=home_team,
                away_team=away_team,
                commence_time=commence,
                candidate_bout_ids=candidate_ids,
                reason="multiple participant+time matches",
                observed_at=stamp,
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
    stamp = require_aware_utc(observed_at, field="observed_at").isoformat()
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
