"""Load and validate versioned MMA settlement rule sets (DWCS-200).

Authoritative YAML bytes are packaged with the wheel. Default loads always verify
``PINNED_SETTLEMENT_HASH`` (SHA-256 of canonical JSON over the parsed document).
Content changes require bumping ``contract_version`` / rule-set versions **and**
updating ``PINNED_SETTLEMENT_HASH`` together.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from enum import StrEnum
from functools import lru_cache
from importlib import resources
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

SETTLEMENT_FILENAME: Final = "settlement_v1.yaml"
CONTRACT_ID: Final = "dwcs_settlement"
EXPECTED_SCHEMA_VERSION: Final = 1
EXPECTED_CONTRACT_VERSION: Final = "1.1.0"
DEFAULT_RULE_SET_ID: Final = "mma_generic"
# Canonical digest: SHA-256 of json.dumps(..., sort_keys=True,
# separators=(",", ":"), ensure_ascii=True) over the authoritative YAML document
# parsed as a plain mapping (packaged mma_model/markets/settlement_v1.yaml).
# Update only together with an intentional contract_version bump.
PINNED_SETTLEMENT_HASH: Final = (
    "af4772d54a5528e8972b1747096b4c8cfd1beeed1bc225f5b074743f38186e7c"
)


class SettlementRulesError(Exception):
    """Base error for settlement-rule contract failures."""


class SettlementRulesValidationError(SettlementRulesError):
    """Settlement YAML failed schema validation."""


class SettlementRulesVersionMismatch(SettlementRulesError):
    """Settlement contract id/version did not match the expected frozen identity."""


class SettlementRulesHashMismatch(SettlementRulesError):
    """Settlement content hash did not match the pinned digest."""


class UnknownRuleSetError(SettlementRulesError):
    """Requested rule set id is not present in the contract."""


class ProvisionalRuleSetError(SettlementRulesError):
    """Provisional sportsbook override selected without explicit allowance."""


class RuleSetStatus(StrEnum):
    """Governance status for a settlement rule set.

    ``internal_contract`` — repository-governed grading rules (default path).
    Not an approved external sportsbook source.

    ``provisional_pending_approved_source`` — sportsbook override lane that
    requires ``allow_provisional=True`` until a durable approved citation exists.
    """

    INTERNAL_CONTRACT = "internal_contract"
    PROVISIONAL_PENDING_APPROVED_SOURCE = "provisional_pending_approved_source"


class SideEffect(StrEnum):
    """How non-decisive bout outcomes map for a market family."""

    WIN = "win"
    LOSS = "loss"
    PUSH = "push"
    VOID = "void"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RuleSourceReference(_FrozenModel):
    id: str
    locator: str
    role: str
    accessed: str


class RuleSourceSpec(_FrozenModel):
    id: str
    citation: str
    references: tuple[RuleSourceReference, ...] = ()

    @field_validator("references", mode="before")
    @classmethod
    def _tuple_refs(cls, value: Any) -> tuple[Any, ...]:
        if value is None:
            return ()
        return tuple(value)


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
    """v1 totals: half-round lines only, elapsed-rounds boundary, push at exact half."""

    half_round_lines: tuple[float, ...]
    half_round_boundary: Literal["elapsed_rounds"]
    round_seconds: int
    exact_half_result: Literal["push"]
    decision_uses_full_scheduled_duration: bool
    no_contest: SideEffect
    cancellation: SideEffect

    @field_validator("half_round_lines", mode="before")
    @classmethod
    def _tuple_lines(cls, value: Any) -> tuple[float, ...]:
        lines = tuple(float(item) for item in value)
        if not lines:
            raise ValueError("half_round_lines must be non-empty")
        for line in lines:
            if line.is_integer():
                raise ValueError(
                    f"v1 totals only support half-round lines; got whole number {line}"
                )
        return lines

    @field_validator("round_seconds")
    @classmethod
    def _positive_round_seconds(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("round_seconds must be positive")
        return value


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
    contract_content_hash: str = Field(min_length=64, max_length=64)


class SettlementRulesContract(_FrozenModel):
    schema_version: int
    contract_id: str
    contract_version: str
    default_rule_set_id: str
    content_hash: str = Field(min_length=64, max_length=64)
    rule_sets: Mapping[str, SettlementRuleSet] = Field(min_length=1)

    @field_validator("rule_sets", mode="after")
    @classmethod
    def _freeze_rule_sets(
        cls, value: Mapping[str, SettlementRuleSet]
    ) -> Mapping[str, SettlementRuleSet]:
        return MappingProxyType(dict(value))

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


def _canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def compute_settlement_hash(payload: Mapping[str, Any]) -> str:
    """Return SHA-256 hex digest of canonical JSON (sorted keys, compact)."""
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SettlementRulesValidationError(
            f"unable to read settlement rules: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise SettlementRulesValidationError("settlement rules root must be a mapping")
    return payload


def _read_package_payload() -> dict[str, Any]:
    root = resources.files("mma_model.markets")
    resource = root.joinpath(SETTLEMENT_FILENAME)
    try:
        raw = resource.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, AttributeError) as exc:
        raise SettlementRulesValidationError(
            f"unable to read packaged settlement resource {SETTLEMENT_FILENAME}"
        ) from exc
    payload = yaml.safe_load(raw)
    if not isinstance(payload, dict):
        raise SettlementRulesValidationError("settlement rules root must be a mapping")
    return payload


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
    merged = {
        **base,
        **{k: v for k, v in raw.items() if k not in {"extends", "overrides"}},
    }
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


def _parse_contract(
    payload: Mapping[str, Any],
    *,
    expected_hash: str | None = None,
    enforce_pinned_digest: bool = True,
) -> SettlementRulesContract:
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

    content_hash = compute_settlement_hash(payload)
    pinned = expected_hash if expected_hash is not None else PINNED_SETTLEMENT_HASH
    if enforce_pinned_digest and content_hash != pinned:
        raise SettlementRulesHashMismatch(
            f"content hash mismatch: got {content_hash}, expected {pinned}"
        )

    parsed_sets: dict[str, SettlementRuleSet] = {}
    for rule_set_id in raw_sets:
        merged = _merge_extended(raw_sets, str(rule_set_id))
        merged["contract_content_hash"] = content_hash
        try:
            parsed_sets[str(rule_set_id)] = SettlementRuleSet.model_validate(merged)
        except ValidationError as exc:
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
                "content_hash": content_hash,
                "rule_sets": parsed_sets,
            }
        )
    except ValidationError as exc:
        raise SettlementRulesValidationError(str(exc)) from exc


def load_settlement_rules(
    path: Path | None = None,
    *,
    expected_hash: str | None = None,
    enforce_pinned_digest: bool = True,
) -> SettlementRulesContract:
    """Load the frozen settlement rules contract from packaged or explicit path."""
    payload = _read_package_payload() if path is None else _read_yaml_mapping(path)
    return _parse_contract(
        payload,
        expected_hash=expected_hash,
        enforce_pinned_digest=enforce_pinned_digest,
    )


@lru_cache(maxsize=1)
def default_settlement_rules() -> SettlementRulesContract:
    """Cached packaged settlement contract (pinned digest enforced)."""
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

