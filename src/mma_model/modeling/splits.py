"""Event-grouped rolling-origin splits (DWCS-302).

One test unit is one canonical event (all bouts, one prediction cutoff).
Outer folds are rolling-origin, one card at a time. Inner prior-time folds
stay inside development (2017–2023) and never see 2024/2025 labels. The
2025 holdout is locked unless an explicit sealed path is enabled.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Never

from sqlalchemy import select
from sqlalchemy.orm import Session

from mma_model.backtest.contract import (
    EvaluatorHashMismatchError,
    HashKind,
    compute_data_hash,
    compute_splits_config_hash,
    current_feature_spec_hash,
    verify_evaluator_hashes,
)
from mma_model.db.tables.core import CanonicalBout, CanonicalEvent
from mma_model.dwcs.classification import SeriesVariant
from mma_model.dwcs.manifest import load_dwcs_bout_manifest, load_dwcs_event_manifest
from mma_model.evaluation.contract import (
    PINNED_CONTRACT_HASH,
    EvaluationContract,
    load_evaluation_contract,
)
from mma_model.features.as_of import (
    AsOfCutoff,
    assert_identical_event_cutoffs,
    cutoff_for_event,
    ensure_utc,
    implied_event_start,
)

UTC = timezone.utc


class FoldRole(StrEnum):
    DEVELOPMENT = "development"
    VALIDATION = "validation"
    HOLDOUT = "holdout"


class FoldKind(StrEnum):
    OUTER = "outer"
    INNER = "inner"


class SplitError(ValueError):
    """Invalid split construction."""


class HoldoutLockedError(SplitError):
    """2025 holdout was requested without the sealed allow_holdout path."""


class TargetCardCountError(SplitError):
    """Universe card count did not match the frozen splits.target_cards value."""

    def __init__(self, got: int, expected: int) -> None:
        self.got = got
        self.expected = expected
        super().__init__(
            f"split universe has {got} cards, expected {expected}; "
            "exclusions must be explicit"
        )


@dataclass(frozen=True)
class SplitCard:
    """One canonical event and its bouts, used as a fold grouping unit."""

    event_id: str
    scheduled_start_at: datetime | None
    event_date: date | None
    series_variant: SeriesVariant
    bout_ids: tuple[str, ...]


@dataclass(frozen=True)
class EventGroup:
    """Grouped card with a single cutoff, season role, and sensitivity flags."""

    event_id: str
    cutoff: AsOfCutoff
    event_start: datetime
    season: int
    role: FoldRole
    series_variant: SeriesVariant
    in_all_dwcs: bool
    in_standard_only: bool
    bout_ids: tuple[str, ...]
    locked: bool


@dataclass(frozen=True)
class FoldMetadata:
    fold_id: str
    kind: FoldKind
    role: FoldRole
    test_event_id: str
    test_event_ids: tuple[str, ...]
    test_bout_ids: tuple[str, ...]
    cutoff: datetime
    max_train_timestamp: datetime | None
    train_event_ids: tuple[str, ...]
    series_variant: SeriesVariant
    in_all_dwcs: bool
    in_standard_only: bool
    locked: bool
    contract_hash: str
    feature_spec_hash: str
    data_hash: str
    config_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "fold_id": self.fold_id,
            "kind": self.kind.value,
            "role": self.role.value,
            "test_event_id": self.test_event_id,
            "test_event_ids": list(self.test_event_ids),
            "test_bout_ids": list(self.test_bout_ids),
            "cutoff": _isoformat(self.cutoff),
            "max_train_timestamp": (
                _isoformat(self.max_train_timestamp)
                if self.max_train_timestamp is not None
                else None
            ),
            "train_event_ids": list(self.train_event_ids),
            "series_variant": self.series_variant.value,
            "in_all_dwcs": self.in_all_dwcs,
            "in_standard_only": self.in_standard_only,
            "locked": self.locked,
            "contract_hash": self.contract_hash,
            "feature_spec_hash": self.feature_spec_hash,
            "data_hash": self.data_hash,
            "config_hash": self.config_hash,
        }


@dataclass(frozen=True)
class FoldPlan:
    folds: tuple[FoldMetadata, ...]
    contract_hash: str
    feature_spec_hash: str
    data_hash: str
    config_hash: str
    include_holdout: bool
    kind: FoldKind
    n_cards: int

    @property
    def n_folds(self) -> int:
        return len(self.folds)

    def to_dict(self) -> dict[str, Any]:
        return {
            "config_hash": self.config_hash,
            "contract_hash": self.contract_hash,
            "data_hash": self.data_hash,
            "feature_spec_hash": self.feature_spec_hash,
            "folds": [fold.to_dict() for fold in self.folds],
            "include_holdout": self.include_holdout,
            "kind": self.kind.value,
            "n_cards": self.n_cards,
            "n_folds": len(self.folds),
        }


def _isoformat(value: datetime) -> str:
    return ensure_utc(value).isoformat()


def _require_contract(contract: EvaluationContract | None) -> EvaluationContract:
    return contract if contract is not None else load_evaluation_contract()


def parse_series_variant(raw: str) -> SeriesVariant:
    value = str(raw).strip().lower()
    if value.startswith("dwcs_"):
        value = value[len("dwcs_") :]
    if value == SeriesVariant.STANDARD.value:
        return SeriesVariant.STANDARD
    if value == SeriesVariant.BRAZIL.value:
        return SeriesVariant.BRAZIL
    raise SplitError(f"unknown series_variant: {raw!r}")


def sensitivity_membership(variant: SeriesVariant) -> tuple[bool, bool]:
    """Return (in_all_dwcs, in_standard_only) for a card variant."""
    if variant is SeriesVariant.STANDARD:
        return True, True
    if variant is SeriesVariant.BRAZIL:
        return True, False
    never_variant: Never = variant
    raise SplitError(f"unhandled series_variant: {never_variant!r}")


def role_for_season(season: int, contract: EvaluationContract) -> FoldRole:
    if season in contract.splits.development.seasons:
        return FoldRole.DEVELOPMENT
    if season in contract.splits.validation.seasons:
        return FoldRole.VALIDATION
    if season in contract.splits.holdout.seasons:
        return FoldRole.HOLDOUT
    raise SplitError(f"season {season} is outside the evaluation windows")


def _window_locked(role: FoldRole, contract: EvaluationContract) -> bool:
    if role is FoldRole.DEVELOPMENT:
        return bool(contract.splits.development.locked)
    if role is FoldRole.VALIDATION:
        return bool(contract.splits.validation.locked)
    if role is FoldRole.HOLDOUT:
        return bool(contract.splits.holdout.locked)
    never_role: Never = role
    raise SplitError(f"unhandled fold role: {never_role!r}")


def _parse_utc(value: str) -> datetime:
    text = str(value).strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    return ensure_utc(parsed)


def protocol_fixture_cards() -> tuple[SplitCard, ...]:
    """Small chronology covering development, Brazil, validation, and holdout."""
    return (
        SplitCard(
            event_id="dev-2017",
            scheduled_start_at=datetime(2017, 7, 11, 19, 0, tzinfo=UTC),
            event_date=date(2017, 7, 11),
            series_variant=SeriesVariant.STANDARD,
            bout_ids=("2017-a", "2017-b"),
        ),
        SplitCard(
            event_id="brazil-2018",
            scheduled_start_at=datetime(2018, 8, 11, 1, 0, tzinfo=UTC),
            event_date=date(2018, 8, 11),
            series_variant=SeriesVariant.BRAZIL,
            bout_ids=("br-a", "br-b"),
        ),
        SplitCard(
            event_id="dev-2023",
            scheduled_start_at=datetime(2023, 8, 22, 2, 0, tzinfo=UTC),
            event_date=date(2023, 8, 22),
            series_variant=SeriesVariant.STANDARD,
            bout_ids=("2023-a",),
        ),
        SplitCard(
            event_id="val-2024",
            scheduled_start_at=datetime(2024, 8, 13, 2, 0, tzinfo=UTC),
            event_date=date(2024, 8, 13),
            series_variant=SeriesVariant.STANDARD,
            bout_ids=("2024-a",),
        ),
        SplitCard(
            event_id="hold-2025",
            scheduled_start_at=datetime(2025, 8, 12, 2, 0, tzinfo=UTC),
            event_date=date(2025, 8, 12),
            series_variant=SeriesVariant.STANDARD,
            bout_ids=("2025-a",),
        ),
    )


def cards_from_manifest() -> tuple[SplitCard, ...]:
    events = load_dwcs_event_manifest()
    bouts = load_dwcs_bout_manifest()
    bouts_by_event: dict[str, list[str]] = {}
    for bout in bouts:
        bouts_by_event.setdefault(bout.event_id, []).append(bout.bout_id)
    cards: list[SplitCard] = []
    for event in events:
        bout_ids = tuple(bouts_by_event.get(event.event_id, ()))
        if not bout_ids:
            raise SplitError(f"manifest event {event.event_id} has no bouts")
        start = _parse_utc(event.occurrence_timestamp)
        cards.append(
            SplitCard(
                event_id=event.event_id,
                scheduled_start_at=start,
                event_date=start.date(),
                series_variant=parse_series_variant(event.series_variant),
                bout_ids=bout_ids,
            )
        )
    if not cards:
        raise SplitError("DWCS manifest produced no split cards")
    return tuple(cards)


def cards_from_session(session: Session) -> tuple[SplitCard, ...]:
    events = list(session.scalars(select(CanonicalEvent)).all())
    bouts = list(session.scalars(select(CanonicalBout)).all())
    bouts_by_event: dict[str, list[str]] = {}
    for bout in bouts:
        bouts_by_event.setdefault(str(bout.event_id), []).append(str(bout.id))
    cards: list[SplitCard] = []
    for event in events:
        series = str(event.series or "").strip().lower()
        if not series.startswith("dwcs"):
            continue
        bout_ids = tuple(bouts_by_event.get(str(event.id), ()))
        if not bout_ids:
            raise SplitError(f"event {event.id} has no bouts")
        cards.append(
            SplitCard(
                event_id=str(event.id),
                scheduled_start_at=event.scheduled_start_at,
                event_date=event.event_date,
                series_variant=parse_series_variant(series),
                bout_ids=bout_ids,
            )
        )
    if not cards:
        raise SplitError("database contains no DWCS events for splits")
    return tuple(cards)


def group_cards(
    cards: Sequence[SplitCard],
    contract: EvaluationContract | None = None,
) -> tuple[EventGroup, ...]:
    resolved = _require_contract(contract)
    if not cards:
        raise SplitError("no events to group")
    seen: set[str] = set()
    groups: list[EventGroup] = []
    cutoffs: list[AsOfCutoff] = []
    for card in cards:
        if card.event_id in seen:
            raise SplitError(f"duplicate event_id: {card.event_id}")
        seen.add(card.event_id)
        if not card.bout_ids:
            raise SplitError(f"event {card.event_id} has no bouts")
        if len(set(card.bout_ids)) != len(card.bout_ids):
            raise SplitError(f"event {card.event_id} has duplicate bout ids")
        cutoff = cutoff_for_event(card)
        cutoffs.append(cutoff)
        start = implied_event_start(cutoff)
        season = start.year
        role = role_for_season(season, resolved)
        in_all_dwcs, in_standard_only = sensitivity_membership(card.series_variant)
        groups.append(
            EventGroup(
                event_id=card.event_id,
                cutoff=cutoff,
                event_start=start,
                season=season,
                role=role,
                series_variant=card.series_variant,
                in_all_dwcs=in_all_dwcs,
                in_standard_only=in_standard_only,
                bout_ids=card.bout_ids,
                locked=_window_locked(role, resolved),
            )
        )
    assert_identical_event_cutoffs(cutoffs)
    groups.sort(key=lambda item: (item.cutoff.cutoff, item.event_id))
    return tuple(groups)


def _data_rows(groups: Sequence[EventGroup]) -> list[dict[str, Any]]:
    return [
        {
            "event_id": group.event_id,
            "bout_ids": list(group.bout_ids),
            "cutoff": _isoformat(group.cutoff.cutoff),
        }
        for group in groups
    ]


def _eligible_train(
    test: EventGroup,
    groups: Sequence[EventGroup],
    *,
    kind: FoldKind,
) -> tuple[EventGroup, ...]:
    trained: list[EventGroup] = []
    for candidate in groups:
        if candidate.event_id == test.event_id:
            continue
        if candidate.event_start >= test.cutoff.cutoff:
            continue
        if kind is FoldKind.INNER and candidate.role is not FoldRole.DEVELOPMENT:
            continue
        if kind is FoldKind.INNER:
            trained.append(candidate)
            continue
        if kind is FoldKind.OUTER:
            trained.append(candidate)
            continue
        never_kind: Never = kind
        raise SplitError(f"unhandled fold kind: {never_kind!r}")
    return tuple(trained)


def _max_train_timestamp(trained: Sequence[EventGroup]) -> datetime | None:
    if not trained:
        return None
    return max(item.event_start for item in trained)


def verify_fold_plan(
    plan: FoldPlan,
    cards: Sequence[SplitCard],
    contract: EvaluationContract | None = None,
) -> None:
    """Recompute hashes from cards/contract/spec and hard-fail on mismatch."""
    resolved = _require_contract(contract)
    groups = group_cards(cards, resolved)
    if plan.n_cards != len(groups):
        raise SplitError(
            f"fold plan n_cards {plan.n_cards} does not match grouped universe {len(groups)}"
        )
    expected_data = compute_data_hash(_data_rows(groups))
    expected_config = compute_splits_config_hash(resolved)
    verify_evaluator_hashes(
        contract_hash=plan.contract_hash,
        feature_spec_hash=plan.feature_spec_hash,
        data_hash=plan.data_hash,
        config_hash=plan.config_hash,
        expected_data_hash=expected_data,
        expected_config_hash=expected_config,
        expected_contract_hash=PINNED_CONTRACT_HASH,
    )
    if plan.contract_hash != resolved.content_hash:
        raise EvaluatorHashMismatchError(
            HashKind.CONTRACT, got=plan.contract_hash, expected=resolved.content_hash
        )
    for fold in plan.folds:
        if fold.contract_hash != plan.contract_hash:
            raise EvaluatorHashMismatchError(
                HashKind.CONTRACT, got=fold.contract_hash, expected=plan.contract_hash
            )
        if fold.feature_spec_hash != plan.feature_spec_hash:
            raise EvaluatorHashMismatchError(
                HashKind.FEATURE_SPEC,
                got=fold.feature_spec_hash,
                expected=plan.feature_spec_hash,
            )
        if fold.data_hash != plan.data_hash:
            raise EvaluatorHashMismatchError(
                HashKind.DATA, got=fold.data_hash, expected=plan.data_hash
            )
        if fold.config_hash != plan.config_hash:
            raise EvaluatorHashMismatchError(
                HashKind.CONFIG, got=fold.config_hash, expected=plan.config_hash
            )


def build_fold_plan(
    cards: Sequence[SplitCard],
    *,
    kind: FoldKind,
    roles: frozenset[FoldRole],
    allow_holdout: bool = False,
    contract: EvaluationContract | None = None,
    require_target_cards: bool = True,
) -> FoldPlan:
    if FoldRole.HOLDOUT in roles and not allow_holdout:
        raise HoldoutLockedError(
            "2025 holdout is locked; use sealed_holdout_folds(..., allow_holdout=True)"
        )
    if kind is FoldKind.INNER and roles != frozenset({FoldRole.DEVELOPMENT}):
        raise SplitError("inner prior-time folds exist only inside development")
    resolved = _require_contract(contract)
    if require_target_cards and len(cards) != resolved.splits.target_cards:
        raise TargetCardCountError(len(cards), resolved.splits.target_cards)
    groups = group_cards(cards, resolved)
    if require_target_cards and len(groups) != resolved.splits.target_cards:
        raise TargetCardCountError(len(groups), resolved.splits.target_cards)
    contract_hash = resolved.content_hash
    feature_hash = current_feature_spec_hash()
    data_hash = compute_data_hash(_data_rows(groups))
    config_hash = compute_splits_config_hash(resolved)
    verify_evaluator_hashes(
        contract_hash=contract_hash,
        feature_spec_hash=feature_hash,
        data_hash=data_hash,
        config_hash=config_hash,
        expected_data_hash=data_hash,
        expected_config_hash=config_hash,
        expected_contract_hash=PINNED_CONTRACT_HASH,
    )

    folds: list[FoldMetadata] = []
    for test in groups:
        if test.role not in roles:
            continue
        if test.role is FoldRole.HOLDOUT and not allow_holdout:
            raise HoldoutLockedError(
                "2025 holdout is locked; use sealed_holdout_folds(..., allow_holdout=True)"
            )
        trained = _eligible_train(test, groups, kind=kind)
        max_train = _max_train_timestamp(trained)
        if max_train is not None and max_train >= test.cutoff.cutoff:
            raise SplitError(
                f"max_train_timestamp {max_train.isoformat()} is not strictly before "
                f"cutoff {test.cutoff.cutoff.isoformat()} for event {test.event_id}"
            )
        if test.event_id in {item.event_id for item in trained}:
            raise SplitError(f"same-event leakage: {test.event_id} in training")
        if kind is FoldKind.INNER:
            for item in (*trained, test):
                if item.role is FoldRole.VALIDATION or item.role is FoldRole.HOLDOUT:
                    raise SplitError("inner folds must not include 2024 or 2025 labels")
                if item.role is FoldRole.DEVELOPMENT:
                    continue
                never_role: Never = item.role
                raise SplitError(f"unhandled fold role: {never_role!r}")
        folds.append(
            FoldMetadata(
                fold_id=f"{kind.value}:{test.role.value}:{test.event_id}",
                kind=kind,
                role=test.role,
                test_event_id=test.event_id,
                test_event_ids=(test.event_id,),
                test_bout_ids=test.bout_ids,
                cutoff=test.cutoff.cutoff,
                max_train_timestamp=max_train,
                train_event_ids=tuple(item.event_id for item in trained),
                series_variant=test.series_variant,
                in_all_dwcs=test.in_all_dwcs,
                in_standard_only=test.in_standard_only,
                locked=test.locked,
                contract_hash=contract_hash,
                feature_spec_hash=feature_hash,
                data_hash=data_hash,
                config_hash=config_hash,
            )
        )
    plan = FoldPlan(
        folds=tuple(folds),
        contract_hash=contract_hash,
        feature_spec_hash=feature_hash,
        data_hash=data_hash,
        config_hash=config_hash,
        include_holdout=allow_holdout and FoldRole.HOLDOUT in roles,
        kind=kind,
        n_cards=len(groups),
    )
    verify_fold_plan(plan, cards, resolved)
    return plan


def tuning_folds(
    cards: Sequence[SplitCard],
    *,
    contract: EvaluationContract | None = None,
    require_target_cards: bool = True,
) -> FoldPlan:
    """Inner prior-time folds inside development. Never includes 2024/2025."""
    return build_fold_plan(
        cards,
        kind=FoldKind.INNER,
        roles=frozenset({FoldRole.DEVELOPMENT}),
        allow_holdout=False,
        contract=contract,
        require_target_cards=require_target_cards,
    )


def validation_folds(
    cards: Sequence[SplitCard],
    *,
    contract: EvaluationContract | None = None,
    require_target_cards: bool = True,
) -> FoldPlan:
    """2024 outer validation folds. Never includes 2025 holdout."""
    return build_fold_plan(
        cards,
        kind=FoldKind.OUTER,
        roles=frozenset({FoldRole.VALIDATION}),
        allow_holdout=False,
        contract=contract,
        require_target_cards=require_target_cards,
    )


def sealed_holdout_folds(
    cards: Sequence[SplitCard],
    *,
    allow_holdout: bool = False,
    contract: EvaluationContract | None = None,
    require_target_cards: bool = True,
) -> FoldPlan:
    """Explicit 2025 holdout listing. Default off; ordinary tuning must not call this."""
    return build_fold_plan(
        cards,
        kind=FoldKind.OUTER,
        roles=frozenset({FoldRole.HOLDOUT}),
        allow_holdout=allow_holdout,
        contract=contract,
        require_target_cards=require_target_cards,
    )


def outer_folds(
    cards: Sequence[SplitCard],
    *,
    allow_holdout: bool = False,
    contract: EvaluationContract | None = None,
    require_target_cards: bool = True,
) -> FoldPlan:
    """Outer rolling-origin folds. Default omits locked 2025 holdout."""
    roles = {FoldRole.DEVELOPMENT, FoldRole.VALIDATION}
    if allow_holdout:
        roles.add(FoldRole.HOLDOUT)
    return build_fold_plan(
        cards,
        kind=FoldKind.OUTER,
        roles=frozenset(roles),
        allow_holdout=allow_holdout,
        contract=contract,
        require_target_cards=require_target_cards,
    )


def inspect_folds(
    *,
    contract_path: Path | None = None,
    include_holdout: bool = False,
    cards: Sequence[SplitCard] | None = None,
    contract: EvaluationContract | None = None,
    require_target_cards: bool | None = None,
) -> FoldPlan:
    """Build the inspect-folds outer plan from the 89-card universe by default."""
    if contract is None and contract_path is not None:
        resolved = load_evaluation_contract(path=contract_path)
    else:
        resolved = _require_contract(contract)
    if cards is None:
        resolved_cards = cards_from_manifest()
    else:
        resolved_cards = tuple(cards)
    enforce = True if require_target_cards is None else require_target_cards
    return outer_folds(
        resolved_cards,
        allow_holdout=include_holdout,
        contract=resolved,
        require_target_cards=enforce,
    )


def render_fold_plan(plan: FoldPlan, *, as_json: bool = False) -> str:
    if as_json:
        return json.dumps(plan.to_dict(), indent=2, sort_keys=True) + "\n"
    role_counts: dict[str, int] = {}
    for fold in plan.folds:
        role_counts[fold.role.value] = role_counts.get(fold.role.value, 0) + 1
    lines = [
        f"contract_hash: {plan.contract_hash}",
        f"feature_spec_hash: {plan.feature_spec_hash}",
        f"data_hash: {plan.data_hash}",
        f"config_hash: {plan.config_hash}",
        f"universe_cards: {plan.n_cards}",
        (
            f"folds: {len(plan.folds)} "
            f"(development={role_counts.get('development', 0)} "
            f"validation={role_counts.get('validation', 0)} "
            f"holdout={role_counts.get('holdout', 0)})"
        ),
    ]
    if not plan.include_holdout:
        lines.append("holdout: locked (pass --include-holdout to list 2025 folds)")
    lines.append(
        "fold_id role test_event_id cutoff max_train_timestamp "
        "train_n test_bouts variant all_dwcs standard_only locked"
    )
    for fold in plan.folds:
        max_train = (
            _isoformat(fold.max_train_timestamp) if fold.max_train_timestamp is not None else "none"
        )
        lines.append(
            f"{fold.fold_id} {fold.role.value} {fold.test_event_id} "
            f"{_isoformat(fold.cutoff)} {max_train} {len(fold.train_event_ids)} "
            f"{len(fold.test_bout_ids)} {fold.series_variant.value} "
            f"{fold.in_all_dwcs} {fold.in_standard_only} {fold.locked}"
        )
    return "\n".join(lines) + "\n"
