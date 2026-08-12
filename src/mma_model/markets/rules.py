"""Load and validate versioned MMA settlement rule sets (DWCS-200)."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any, Final, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SETTLEMENT_FILENAME: Final = "settlement_v1.yaml"
CONTRACT_ID: Final = "dwcs_settlement"
EXPECTED_SCHEMA_VERSION: Final = 1
EXPECTED_CONTRACT_VERSION: Final = "1.0.0"
DEFAULT_RULE_SET_ID: Final = "mma_generic"


class SettlementRulesError(Exception):
    """Base error for settlement-rule contract failures."""


class SettlementRulesValidationError(SettlementRulesError):
    """Settlement YAML failed schema validation."""


class SettlementRulesVersionMismatch(SettlementRulesError):
    """Settlement contract id/version did not match the expected frozen identity."""


class UnknownRuleSetError(SettlementRulesError):
    """Requested rule set id is not present in the contract."""


class ProvisionalRuleSetError(SettlementRulesError):
    """Provisional sportsbook override selected without explicit allowance."""


class RuleSetStatus(StrEnum):
    APPROVED = "approved"
    PROVISIONAL_PENDING_APPROVED_SOURCE = "provisional_pending_approved_source"


class SideEffect(StrEnum):
    """How non-decisive bout outcomes map for a market family."""

    WIN = "win"
    LOSS = "loss"
    PUSH = "push"
    VOID = "void"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RuleSourceSpec(_FrozenModel):
    id: str
    citation: str


class MoneylineRules(_FrozenModel):
    draw: SideEffect
    no_contest: SideEffect
    cancellation: SideEffect
    technical_decision: Literal["settle_as_decision"]


class GoesDistanceRules(_FrozenModel):
    decision_counts_as_goes_distance: bool
    technical_decision_counts_as_goes_distance: bool
    draw_counts_as_goes_distance: bool
    no_contest: SideEffect
    cancellation: SideEffect


class TotalsRules(_FrozenModel):
    half_round_lines: tuple[float, ...]
    half_round_boundary: Literal["ending_round"]
    half_round_push: bool
    whole_round_push: bool
    decision_uses_scheduled_rounds_as_ending_round: bool
    no_contest: SideEffect
    cancellation: SideEffect

    @field_validator("half_round_lines", mode="before")
    @classmethod
    def _tuple_lines(cls, value: Any) -> tuple[float, ...]:
        return tuple(float(item) for item in value)


class MethodRules(_FrozenModel):
    technical_decision_counts_as: Literal["decision"]
    draw: SideEffect
    no_contest: SideEffect
    cancellation: SideEffect


class ExactRoundRules(_FrozenModel):
    decision: SideEffect
    draw: SideEffect
    technical_decision: SideEffect
    no_contest: SideEffect
    cancellation: SideEffect


class SettlementRuleSet(_FrozenModel):
    rule_set_id: str
    version: str
    status: RuleSetStatus
    source: RuleSourceSpec
    moneyline: MoneylineRules
    goes_distance: GoesDistanceRules
    totals: TotalsRules
    method: MethodRules
    fighter_by_method: MethodRules
    exact_round: ExactRoundRules
    extends: str | None = None


class SettlementRulesContract(_FrozenModel):
    schema_version: int
    contract_id: str
    contract_version: str
    default_rule_set_id: str
    rule_sets: Mapping[str, SettlementRuleSet] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_default(self) -> SettlementRulesContract:
        if self.default_rule_set_id not in self.rule_sets:
            raise ValueError(
                f"default_rule_set_id {self.default_rule_set_id!r} missing from rule_sets"
            )
        return self


def package_settlement_resource_path() -> Path:
    """Filesystem path to the packaged settlement YAML (checkout / wheel)."""
    root = resources.files("mma_model.markets")
    target = root.joinpath(SETTLEMENT_FILENAME)
    with resources.as_file(target) as path:
        return Path(path)


def visible_settlement_path() -> Path:
    """Plan-visible config path (symlink to packaged bytes in checkout)."""
    return Path(__file__).resolve().parents[3] / "config" / "markets" / SETTLEMENT_FILENAME


def _merge_extended(
    raw_sets: Mapping[str, Any],
    rule_set_id: str,
    *,
    stack: tuple[str, ...] = (),
) -> dict[str, Any]:
    if rule_set_id in stack:
        raise SettlementRulesValidationError(
            f"cyclic rule set extends involving {rule_set_id!r}"
        )
    raw = raw_sets.get(rule_set_id)
    if not isinstance(raw, Mapping):
        raise UnknownRuleSetError(f"unknown settlement rule set: {rule_set_id!r}")
    extends = raw.get("extends")
    base: dict[str, Any] = {}
    if extends is not None:
        base = _merge_extended(raw_sets, str(extends), stack=(*stack, rule_set_id))
    merged = {**base, **{k: v for k, v in raw.items() if k not in {"extends", "overrides"}}}
    overrides = raw.get("overrides") or {}
    if overrides and not isinstance(overrides, Mapping):
        raise SettlementRulesValidationError(
            f"rule set {rule_set_id!r} overrides must be a mapping"
        )
    for section, patch in dict(overrides).items():
        if not isinstance(patch, Mapping):
            raise SettlementRulesValidationError(
                f"override section {section!r} must be a mapping"
            )
        current = dict(merged.get(section) or {})
        current.update(dict(patch))
        merged[section] = current
    merged["rule_set_id"] = rule_set_id
    return merged


def _parse_contract(payload: Mapping[str, Any]) -> SettlementRulesContract:
    if payload.get("contract_id") != CONTRACT_ID:
        raise SettlementRulesVersionMismatch(
            f"contract_id mismatch: got {payload.get('contract_id')!r}, expected {CONTRACT_ID!r}"
        )
    if payload.get("schema_version") != EXPECTED_SCHEMA_VERSION:
        raise SettlementRulesVersionMismatch(
            f"schema_version mismatch: got {payload.get('schema_version')!r}, "
            f"expected {EXPECTED_SCHEMA_VERSION!r}"
        )
    if payload.get("contract_version") != EXPECTED_CONTRACT_VERSION:
        raise SettlementRulesVersionMismatch(
            f"contract_version mismatch: got {payload.get('contract_version')!r}, "
            f"expected {EXPECTED_CONTRACT_VERSION!r}"
        )
    raw_sets = payload.get("rule_sets")
    if not isinstance(raw_sets, Mapping) or not raw_sets:
        raise SettlementRulesValidationError("rule_sets must be a non-empty mapping")

    parsed_sets: dict[str, SettlementRuleSet] = {}
    for rule_set_id in raw_sets:
        merged = _merge_extended(raw_sets, str(rule_set_id))
        try:
            parsed_sets[str(rule_set_id)] = SettlementRuleSet.model_validate(merged)
        except Exception as exc:
            raise SettlementRulesValidationError(
                f"invalid rule set {rule_set_id!r}: {exc}"
            ) from exc

    try:
        return SettlementRulesContract.model_validate(
            {
                "schema_version": payload["schema_version"],
                "contract_id": payload["contract_id"],
                "contract_version": payload["contract_version"],
                "default_rule_set_id": payload["default_rule_set_id"],
                "rule_sets": parsed_sets,
            }
        )
    except Exception as exc:
        raise SettlementRulesValidationError(str(exc)) from exc


def load_settlement_rules(path: Path | None = None) -> SettlementRulesContract:
    """Load the frozen settlement rules contract from packaged or explicit path."""
    target = path if path is not None else package_settlement_resource_path()
    try:
        payload = yaml.safe_load(target.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SettlementRulesValidationError(f"unable to read settlement rules: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise SettlementRulesValidationError("settlement rules root must be a mapping")
    return _parse_contract(payload)


@lru_cache(maxsize=1)
def default_settlement_rules() -> SettlementRulesContract:
    """Cached packaged settlement contract."""
    return load_settlement_rules()


def get_rule_set(
    rule_set_id: str | None = None,
    *,
    contract: SettlementRulesContract | None = None,
    allow_provisional: bool = False,
) -> SettlementRuleSet:
    """Resolve a rule set; provisional sportsbook overrides require explicit allow."""
    rules = contract if contract is not None else default_settlement_rules()
    key = rule_set_id or rules.default_rule_set_id
    try:
        rule_set = rules.rule_sets[key]
    except KeyError as exc:
        raise UnknownRuleSetError(f"unknown settlement rule set: {key!r}") from exc
    if (
        rule_set.status is RuleSetStatus.PROVISIONAL_PENDING_APPROVED_SOURCE
        and not allow_provisional
    ):
        raise ProvisionalRuleSetError(
            f"rule set {key!r} is provisional_pending_approved_source; "
            "pass allow_provisional=True only after an approved source citation exists"
        )
    return rule_set
