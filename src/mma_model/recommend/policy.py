"""Frozen confirmed-value / price-target policy (DWCS-307).

Authoritative YAML lives in package data. The checkout path
``config/recommendation_policy.yaml`` is a symlink to the same bytes.
The loader mirrors the frozen evaluation contract and fails closed on drift.
Replay must never tune these thresholds.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from importlib import resources
from pathlib import Path
from typing import Any, Final, Literal, Never, cast

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    ValidationError,
    field_validator,
    model_validator,
)

from mma_model.config import get_settings
from mma_model.domain.markets import (
    VOID_ON_DRAW_FAMILIES,
    MarketFamily,
    MarketMaturity,
    OutcomeKey,
    RecommendationState,
    assert_known_outcome,
    catalog_for_family,
)
from mma_model.domain.quote_eligibility import (
    ELIGIBILITY_BLOCKING_LIFECYCLES,
    RECOGNIZED_QUOTE_ELIGIBILITY_DECISION_VERSIONS,
)
from mma_model.evaluation.contract import (
    PINNED_CONTRACT_HASH,
    EvaluationContract,
    RankConfirmedBy,
    RecommendationClass,
    load_evaluation_contract,
)
from mma_model.markets.price_targets import (
    compute_price_thresholds,
    confirmed_value_min_prob_ev_positive,
    decimal_to_american,
    offered_meets_actionable,
)
from mma_model.value.ev import expected_value, expected_value_with_void
from mma_model.value.odds import (
    round_american_for_display,
    validate_decimal_odds,
    validate_probability,
)

POLICY_FILENAME: Final = "recommendation_policy.yaml"
POLICY_ID: Final = "dwcs_recommendation"
EXPECTED_SCHEMA_VERSION: Final = 1
EXPECTED_POLICY_VERSION: Final = "1.0.0"
PRODUCTION_BOOTSTRAP_REFITS: Final = 200
SHA256_HEX: Final = re.compile(r"^[0-9a-f]{64}$")
PINNED_POLICY_HASH: Final = (
    "6f18bffd536f4b9a7f41ac6e05903758595981e1dabc28a7d310a422532eb646"
)
OR_BETTER_SUFFIX: Final = " or better"
EV_LABEL_EXACT: Final = "exact_per_stake"
EV_LABEL_CONDITIONAL: Final = "conditional_nonvoid"
EV_LABEL_EXHAUSTIVE: Final = "exhaustive"


class RecommendationPolicyError(Exception):
    """Base error for recommendation-policy failures."""


class PolicyValidationError(RecommendationPolicyError):
    """Policy YAML failed schema or protocol validation."""


class PolicyVersionMismatch(RecommendationPolicyError):
    """Policy id/version/schema did not match the frozen identity."""


class PolicyHashMismatch(RecommendationPolicyError):
    """Policy content hash did not match the pinned digest."""


class PolicyContractDriftError(RecommendationPolicyError):
    """Policy YAML drifted from the frozen evaluation contract."""


class ProbabilitySemantics(StrEnum):
    EXHAUSTIVE = "exhaustive"
    CONDITIONAL_NONVOID = "conditional_nonvoid"


class QuoteSourceKind(StrEnum):
    AUTOMATIC = "automatic"
    USER_OBSERVED = "user_observed"


class GateId(StrEnum):
    IDENTITY = "identity"
    DATA_QUALITY = "data_quality"
    MODEL = "model"
    UNCERTAINTY = "uncertainty"
    MARKET_MATURITY = "market_maturity"
    QUOTE = "quote"
    PRICE = "price"


class NoBetReason(StrEnum):
    MALFORMED_CANDIDATE = "malformed_candidate"
    UNSUPPORTED_SELECTION = "unsupported_selection"
    IDENTITY_UNRESOLVED = "identity_unresolved"
    CANONICAL_MISMATCH = "canonical_mismatch"
    AMBIGUOUS_SELECTION = "ambiguous_selection"
    REPLACEMENT = "replacement"
    DATA_QUALITY = "data_quality"
    INCOMPLETE_DATA = "incomplete_data"
    MODEL_UNQUALIFIED = "model_unqualified"
    UNCALIBRATED = "uncalibrated"
    HASH_MISMATCH = "hash_mismatch"
    NONPRODUCTION_UNCERTAINTY = "nonproduction_uncertainty"
    MISSING_BOOTSTRAP = "missing_bootstrap"
    MISSING_P25 = "missing_p25"
    INVALID_PERCENTILES = "invalid_percentiles"
    MARKET_BLOCKED = "market_blocked"
    MARKET_EXPERIMENTAL = "market_experimental"
    STALE_LINE = "stale_line"
    POST_CUTOFF = "post_cutoff"
    SUSPENDED_LINE = "suspended_line"
    LOCKED_LINE = "locked_line"
    REPLACED_LINE = "replaced_line"
    INELIGIBLE_QUOTE = "ineligible_quote"
    MISSING_ELIGIBILITY_DECISION = "missing_eligibility_decision"
    BELOW_ACTIONABLE = "below_actionable"
    MISSING_PROB_EV_POSITIVE = "missing_prob_ev_positive"
    PROB_EV_POSITIVE_LOW = "prob_ev_positive_low"
    P25_EV_NONPOSITIVE = "p25_ev_nonpositive"
    LOWER_RANKED_ELIGIBLE_SELECTION = "lower_ranked_eligible_selection"
    SECONDARY_PRICE_TARGET = "secondary_price_target"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MirroredContractSpec(_FrozenModel):
    max_confirmed_value_markets_per_matchup: Literal[1]
    rank_confirmed_by: RankConfirmedBy
    emit_no_bet_when_gates_fail: Literal[True]
    fair_decimal_odds: Literal["1 / p50"]
    actionable_ev_target: Literal[0.05]
    strong_value_ev_target: Literal[0.1]
    actionable_decimal_price: Literal["max(1 / p25, 1.05 / p50)"]
    strong_value_decimal_price: Literal["max(1 / p25, 1.10 / p50)"]
    confirmed_value_min_prob_ev_positive: Literal[0.7]
    exact_round_actionable_ev_target: Literal[0.1]
    exact_round_min_prob_ev_positive: Literal[0.75]
    classifications: tuple[RecommendationClass, ...]
    unpriced_target_is_not_best_available_market: Literal[True]
    american_odds_renderer_expresses_or_better: Literal[True]
    production_bootstrap_refits: Literal[200]

    @field_validator("classifications", mode="before")
    @classmethod
    def _tupleize_classifications(cls, value: Any) -> tuple[Any, ...]:
        return tuple(value)


class RecommendationPolicy(_FrozenModel):
    schema_version: int
    policy_id: str
    policy_version: str
    ticket: Literal["DWCS-307"]
    description: str
    mirrors_evaluation_contract: MirroredContractSpec
    market_priority: tuple[MarketFamily, ...]
    market_maturity: dict[MarketFamily, MarketMaturity]
    qualified_families: tuple[MarketFamily, ...]
    gate_order: tuple[GateId, ...]
    no_bet_reason_precedence: tuple[NoBetReason, ...]
    content_hash: str
    evaluation_contract_hash: str

    @field_validator(
        "market_priority",
        "qualified_families",
        "gate_order",
        "no_bet_reason_precedence",
        mode="before",
    )
    @classmethod
    def _tupleize(cls, value: Any) -> tuple[Any, ...]:
        return tuple(value)

    @field_validator("market_maturity", mode="before")
    @classmethod
    def _parse_maturity(cls, value: Any) -> dict[MarketFamily, MarketMaturity]:
        if not isinstance(value, Mapping):
            raise ValueError("market_maturity must be a mapping")
        parsed: dict[MarketFamily, MarketMaturity] = {}
        for key, raw in value.items():
            family = MarketFamily(str(key))
            parsed[family] = MarketMaturity(str(raw))
        return parsed

    @model_validator(mode="after")
    def _validate_frozen_maps(self) -> RecommendationPolicy:
        families = tuple(MarketFamily)
        if set(self.market_priority) != set(families):
            raise ValueError("market_priority must list every v1 market family once")
        if len(self.market_priority) != len(set(self.market_priority)):
            raise ValueError("market_priority must not contain duplicates")
        if set(self.market_maturity) != set(families):
            raise ValueError("market_maturity must cover every v1 market family")
        if set(self.gate_order) != {
            GateId.IDENTITY,
            GateId.DATA_QUALITY,
            GateId.MODEL,
            GateId.UNCERTAINTY,
            GateId.MARKET_MATURITY,
        }:
            raise ValueError("gate_order must be the five frozen pre-price gates")
        if self.gate_order[0] is not GateId.IDENTITY:
            raise ValueError("identity must be the first pre-price gate")
        if set(self.no_bet_reason_precedence) != set(NoBetReason):
            raise ValueError("no_bet_reason_precedence must be the full frozen taxonomy")
        qualified = set(self.qualified_families)
        for family in families:
            maturity = self.market_maturity[family]
            if family in qualified and maturity is not MarketMaturity.QUALIFIED:
                raise ValueError(
                    f"{family.value} is qualified_families but maturity is {maturity.value}"
                )
            if maturity is MarketMaturity.QUALIFIED and family not in qualified:
                raise ValueError(
                    f"{family.value} is qualified in the maturity map but missing "
                    "from qualified_families"
                )
        return self

    def maturity_for(self, family: MarketFamily) -> MarketMaturity:
        if family is MarketFamily.MONEYLINE:
            return self.market_maturity[family]
        if family is MarketFamily.TOTALS:
            return self.market_maturity[family]
        if family is MarketFamily.GOES_DISTANCE:
            return self.market_maturity[family]
        if family is MarketFamily.METHOD:
            return self.market_maturity[family]
        if family is MarketFamily.FIGHTER_BY_METHOD:
            return self.market_maturity[family]
        if family is MarketFamily.EXACT_ROUND:
            return self.market_maturity[family]
        never_family: Never = family
        raise PolicyValidationError(f"unhandled market family: {never_family!r}")

    def market_priority_index(self, family: MarketFamily) -> int:
        return self.market_priority.index(family)

    def family_is_qualified(self, family: MarketFamily) -> bool:
        return family in self.qualified_families

    def min_prob_ev_positive(self, family: MarketFamily) -> float:
        return confirmed_value_min_prob_ev_positive(family)

    def primary_reason(self, reasons: Sequence[NoBetReason]) -> NoBetReason | None:
        if not reasons:
            return None
        present = set(reasons)
        for reason in self.no_bet_reason_precedence:
            if reason in present:
                return reason
        listed = ", ".join(item.value for item in reasons)
        raise PolicyValidationError(f"no-bet reasons missing from frozen precedence: {listed}")


def package_policy_resource_path() -> Path:
    root = resources.files("mma_model.recommend")
    resource = root.joinpath(POLICY_FILENAME)
    with resources.as_file(resource) as path:
        return Path(path)


def visible_policy_path(*, root: Path | None = None) -> Path:
    base = root if root is not None else get_settings().project_root
    return base / "config" / POLICY_FILENAME


def _canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )


def compute_policy_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def _is_valid_policy_yaml_file(path: Path) -> bool:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        return False
    return (
        isinstance(payload, dict)
        and "schema_version" in payload
        and "policy_id" in payload
        and "policy_version" in payload
    )


def _resolve_symlink_pointer_text(visible: Path) -> Path | None:
    try:
        text = visible.read_text(encoding="utf-8")
    except OSError:
        return None
    stripped = text.strip()
    if not stripped or "\n" in stripped or "\r" in stripped:
        return None
    if stripped[0] in "{[":
        return None
    candidate = (visible.parent / stripped).resolve()
    if candidate.is_file() and _is_valid_policy_yaml_file(candidate):
        return candidate
    return None


def policy_path(*, root: Path | None = None) -> Path:
    visible = visible_policy_path(root=root)
    if visible.is_file() and _is_valid_policy_yaml_file(visible):
        return visible
    if visible.is_file():
        pointed = _resolve_symlink_pointer_text(visible)
        if pointed is not None:
            return pointed
    return package_policy_resource_path()


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RecommendationPolicyError(f"Unable to read recommendation policy at {path}") from exc
    except yaml.YAMLError as exc:
        raise PolicyValidationError(f"Recommendation policy is not valid YAML: {path}") from exc
    if not isinstance(payload, dict):
        raise PolicyValidationError("Recommendation policy root must be a mapping")
    return payload


def _read_package_payload() -> dict[str, Any]:
    root = resources.files("mma_model.recommend")
    resource = root.joinpath(POLICY_FILENAME)
    try:
        raw = resource.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, AttributeError) as exc:
        raise RecommendationPolicyError(
            f"Unable to read packaged recommendation policy {POLICY_FILENAME}"
        ) from exc
    payload = yaml.safe_load(raw)
    if not isinstance(payload, dict):
        raise PolicyValidationError("Recommendation policy root must be a mapping")
    return payload


def _assert_mirrors_contract(mirrors: MirroredContractSpec, contract: EvaluationContract) -> None:
    rec = contract.recommendation
    if (
        mirrors.max_confirmed_value_markets_per_matchup
        != rec.max_confirmed_value_markets_per_matchup
    ):
        raise PolicyContractDriftError("max_confirmed_value_markets_per_matchup drifted")
    if mirrors.rank_confirmed_by is not rec.rank_confirmed_by:
        raise PolicyContractDriftError("rank_confirmed_by drifted")
    if mirrors.actionable_ev_target != rec.actionable_ev_target:
        raise PolicyContractDriftError("actionable_ev_target drifted")
    if mirrors.strong_value_ev_target != rec.strong_value_ev_target:
        raise PolicyContractDriftError("strong_value_ev_target drifted")
    if mirrors.exact_round_actionable_ev_target != rec.exact_round_actionable_ev_target:
        raise PolicyContractDriftError("exact_round_actionable_ev_target drifted")
    if mirrors.confirmed_value_min_prob_ev_positive != rec.confirmed_value_min_prob_ev_positive:
        raise PolicyContractDriftError("confirmed_value_min_prob_ev_positive drifted")
    if mirrors.exact_round_min_prob_ev_positive != rec.exact_round_min_prob_ev_positive:
        raise PolicyContractDriftError("exact_round_min_prob_ev_positive drifted")
    if tuple(mirrors.classifications) != tuple(rec.classifications):
        raise PolicyContractDriftError("classifications drifted")
    if (
        mirrors.unpriced_target_is_not_best_available_market
        is not rec.unpriced_target_is_not_best_available_market
    ):
        raise PolicyContractDriftError("unpriced_target_is_not_best_available_market drifted")
    if mirrors.production_bootstrap_refits != contract.confidence_intervals.bootstrap_refits:
        raise PolicyContractDriftError("production_bootstrap_refits drifted")
    if contract.content_hash != PINNED_CONTRACT_HASH:
        raise PolicyContractDriftError("evaluation contract content hash is not the pinned digest")


def load_recommendation_policy(
    *,
    path: Path | None = None,
    contract: EvaluationContract | None = None,
    expected_hash: str | None = None,
    enforce_pinned_digest: bool = True,
    root: Path | None = None,
) -> RecommendationPolicy:
    """Load the frozen policy and cross-check the evaluation contract."""
    if path is not None:
        payload = _read_yaml_mapping(path)
    elif root is not None:
        payload = _read_yaml_mapping(policy_path(root=root))
    else:
        payload = _read_package_payload()

    content_hash = compute_policy_hash(payload)
    if payload.get("schema_version") != EXPECTED_SCHEMA_VERSION:
        raise PolicyVersionMismatch(
            f"schema_version mismatch: got {payload.get('schema_version')!r}, "
            f"expected {EXPECTED_SCHEMA_VERSION!r}"
        )
    if payload.get("policy_id") != POLICY_ID:
        raise PolicyVersionMismatch(
            f"policy_id mismatch: got {payload.get('policy_id')!r}, expected {POLICY_ID!r}"
        )
    if payload.get("policy_version") != EXPECTED_POLICY_VERSION:
        raise PolicyVersionMismatch(
            "policy_version mismatch: "
            f"got {payload.get('policy_version')!r}, expected {EXPECTED_POLICY_VERSION!r}"
        )
    if enforce_pinned_digest and content_hash != PINNED_POLICY_HASH:
        raise PolicyHashMismatch(
            f"content hash mismatch versus pinned digest: got {content_hash}, "
            f"expected {PINNED_POLICY_HASH}"
        )
    if expected_hash is not None and content_hash != expected_hash:
        raise PolicyHashMismatch(
            f"content hash mismatch: got {content_hash}, expected {expected_hash}"
        )

    resolved_contract = contract if contract is not None else load_evaluation_contract()
    try:
        policy = RecommendationPolicy.model_validate(
            {
                **payload,
                "content_hash": content_hash,
                "evaluation_contract_hash": resolved_contract.content_hash,
            }
        )
    except ValidationError as exc:
        raise PolicyValidationError(str(exc)) from exc
    _assert_mirrors_contract(policy.mirrors_evaluation_contract, resolved_contract)
    return policy


def require_sha256(value: object, *, field: str) -> str:
    text = str(value or "")
    if not SHA256_HEX.fullmatch(text):
        raise ValueError(f"{field} must be a 64-char sha256 hex digest")
    return text


def require_aware(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def canonical_selection_id(
    *,
    event_id: str,
    bout_id: str,
    family: MarketFamily,
    outcome: OutcomeKey,
    line_point: float | None,
) -> str:
    catalog = catalog_for_family(family)
    assert_known_outcome(family, outcome)
    if not catalog.is_valid_line_point(line_point):
        raise ValueError(f"line_point {line_point!r} is not valid for {family.value}")
    if line_point is None:
        return f"{event_id}/{bout_id}/{family.value}:{outcome.value}"
    return f"{event_id}/{bout_id}/{family.value}:{outcome.value}:{float(line_point)}"


def format_american_or_better(american: float) -> str:
    """Render an American threshold as ``<american> or better``."""
    rounded = round_american_for_display(american)
    if abs(rounded - round(rounded)) < 1e-9:
        whole = int(round(rounded))
        signed = f"+{whole}" if whole > 0 else f"{whole}"
        return f"{signed}{OR_BETTER_SUFFIX}"
    if rounded > 0:
        return f"+{rounded:.2f}{OR_BETTER_SUFFIX}"
    return f"{rounded:.2f}{OR_BETTER_SUFFIX}"


@dataclass(frozen=True)
class RenderedThresholds:
    fair_decimal: float
    actionable_decimal: float
    strong_value_decimal: float
    fair_american: float
    actionable_american: float
    strong_value_american: float
    fair_or_better: str
    actionable_or_better: str
    strong_value_or_better: str
    actionable_ev_target: float
    strong_value_ev_target: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "actionable_american": self.actionable_american,
            "actionable_decimal": self.actionable_decimal,
            "actionable_ev_target": self.actionable_ev_target,
            "actionable_or_better": self.actionable_or_better,
            "fair_american": self.fair_american,
            "fair_decimal": self.fair_decimal,
            "fair_or_better": self.fair_or_better,
            "strong_value_american": self.strong_value_american,
            "strong_value_decimal": self.strong_value_decimal,
            "strong_value_ev_target": self.strong_value_ev_target,
            "strong_value_or_better": self.strong_value_or_better,
        }


def render_thresholds(p50: float, p25: float, *, family: MarketFamily) -> RenderedThresholds:
    raw = compute_price_thresholds(p50, p25, family=family)
    fair_american = decimal_to_american(raw.fair_decimal)
    actionable_american = decimal_to_american(raw.actionable_decimal)
    strong_american = decimal_to_american(raw.strong_value_decimal)
    return RenderedThresholds(
        fair_decimal=raw.fair_decimal,
        actionable_decimal=raw.actionable_decimal,
        strong_value_decimal=raw.strong_value_decimal,
        fair_american=fair_american,
        actionable_american=actionable_american,
        strong_value_american=strong_american,
        fair_or_better=format_american_or_better(fair_american),
        actionable_or_better=format_american_or_better(actionable_american),
        strong_value_or_better=format_american_or_better(strong_american),
        actionable_ev_target=raw.actionable_ev_target,
        strong_value_ev_target=raw.strong_value_ev_target,
    )


@dataclass(frozen=True)
class QuoteEvidence:
    offered_decimal: float
    source_kind: QuoteSourceKind
    observed_at: datetime
    cutoff: datetime
    bookmaker_key: str | None = None
    region: str | None = None
    eligibility_decision_identity: str | None = None
    eligibility_decision_version: str | None = None
    eligibility_evaluated_at: datetime | None = None
    eligible: bool = False
    availability: str = "unknown"
    lifecycle: str = "unresolved"
    freshness_at: datetime | None = None
    stale: bool = False
    suspended: bool = False
    locked: bool = False
    replaced: bool = False
    ambiguous: bool = False

    def __post_init__(self) -> None:
        validate_decimal_odds(self.offered_decimal, field="offered_decimal")
        require_aware(self.observed_at, field="observed_at")
        require_aware(self.cutoff, field="cutoff")
        if self.eligibility_evaluated_at is not None:
            require_aware(self.eligibility_evaluated_at, field="eligibility_evaluated_at")
        if self.freshness_at is not None:
            require_aware(self.freshness_at, field="freshness_at")
        if self.source_kind is QuoteSourceKind.AUTOMATIC:
            return
        if self.source_kind is QuoteSourceKind.USER_OBSERVED:
            return
        never_kind: Never = self.source_kind
        raise ValueError(f"unhandled quote source kind: {never_kind!r}")


@dataclass(frozen=True)
class SelectionCandidate:
    event_id: str
    bout_id: str
    selection_id: str
    family: MarketFamily
    outcome: OutcomeKey
    line_point: float | None
    p50: float
    p25: float | None
    probability_semantics: ProbabilitySemantics
    bootstrap_successful_count: int | None
    bootstrap_seed: int | None
    estimator_hash: str
    calibration_hash: str | None
    data_hash: str
    config_hash: str
    identity_resolved: bool
    canonical_match: bool
    ambiguous: bool
    replacement: bool
    data_quality_pass: bool
    model_qualified: bool
    calibrated: bool
    market_maturity: MarketMaturity
    p_win_unconditional: float | None = None
    p_void: float | None = None
    evaluation_contract_hash: str | None = None
    quote: QuoteEvidence | None = None
    prob_ev_positive: float | None = None
    production_uncertainty: bool = False

    def __post_init__(self) -> None:
        if not self.event_id.strip() or not self.bout_id.strip() or not self.selection_id.strip():
            raise ValueError("event_id, bout_id, and selection_id must be non-empty")
        expected = canonical_selection_id(
            event_id=self.event_id,
            bout_id=self.bout_id,
            family=self.family,
            outcome=self.outcome,
            line_point=self.line_point,
        )
        if self.selection_id != expected:
            raise ValueError(
                f"selection_id mismatch: got {self.selection_id!r}, expected {expected!r}"
            )
        validate_probability(self.p50, field="p50")
        if self.p25 is not None:
            validate_probability(self.p25, field="p25")
            if self.p25 > self.p50:
                raise ValueError("p25 must be <= p50")
        require_sha256(self.estimator_hash, field="estimator_hash")
        require_sha256(self.data_hash, field="data_hash")
        require_sha256(self.config_hash, field="config_hash")
        if self.calibration_hash is not None:
            require_sha256(self.calibration_hash, field="calibration_hash")
        if self.evaluation_contract_hash is not None:
            require_sha256(self.evaluation_contract_hash, field="evaluation_contract_hash")
        if self.p_win_unconditional is not None:
            validate_probability(self.p_win_unconditional, field="p_win_unconditional")
        if self.p_void is not None and (
            not math.isfinite(self.p_void) or not 0.0 <= self.p_void < 1.0
        ):
            raise ValueError("p_void must be in [0, 1)")
        if self.prob_ev_positive is not None and (
            not math.isfinite(self.prob_ev_positive) or not 0.0 <= self.prob_ev_positive <= 1.0
        ):
            raise ValueError("prob_ev_positive must be in [0, 1]")
        if self.probability_semantics is ProbabilitySemantics.EXHAUSTIVE:
            return
        if self.probability_semantics is ProbabilitySemantics.CONDITIONAL_NONVOID:
            return
        never_sem: Never = self.probability_semantics
        raise ValueError(f"unhandled probability semantics: {never_sem!r}")


@dataclass(frozen=True)
class GateResult:
    gate: GateId
    passed: bool
    reasons: tuple[NoBetReason, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "gate": self.gate.value,
            "passed": self.passed,
            "reasons": [reason.value for reason in self.reasons],
        }


@dataclass(frozen=True)
class GateTrace:
    results: tuple[GateResult, ...]

    @property
    def failed_reasons(self) -> tuple[NoBetReason, ...]:
        reasons: list[NoBetReason] = []
        seen: set[NoBetReason] = set()
        for result in self.results:
            for reason in result.reasons:
                if reason not in seen:
                    seen.add(reason)
                    reasons.append(reason)
        return tuple(reasons)

    def pre_price_passed(self) -> bool:
        for result in self.results:
            if result.gate in {
                GateId.QUOTE,
                GateId.PRICE,
            }:
                continue
            if not result.passed:
                return False
        return True

    def as_dict(self) -> dict[str, Any]:
        return {"results": [item.as_dict() for item in self.results]}


@dataclass(frozen=True)
class SelectionDecision:
    event_id: str
    bout_id: str
    selection_id: str
    family: MarketFamily | None
    outcome: OutcomeKey | None
    line_point: float | None
    classification: RecommendationState
    reasons: tuple[NoBetReason, ...]
    primary_reason: NoBetReason | None
    gate_trace: GateTrace
    thresholds: RenderedThresholds | None
    p50: float | None
    p25: float | None
    probability_semantics: ProbabilitySemantics | None
    p25_ev: float | None
    median_ev: float | None
    ev_semantics_label: str | None
    offered_decimal: float | None
    offered_american: float | None
    prob_ev_positive: float | None
    primary_price_target: bool = False
    confirmed_rank: int | None = None
    watchlist_rank: int | None = None
    roi: None = None
    clv: None = None
    profit: None = None
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "bout_id": self.bout_id,
            "classification": self.classification.value,
            "confirmed_rank": self.confirmed_rank,
            "detail": self.detail,
            "event_id": self.event_id,
            "ev_semantics_label": self.ev_semantics_label,
            "family": None if self.family is None else self.family.value,
            "gate_trace": self.gate_trace.as_dict(),
            "line_point": self.line_point,
            "outcome": None if self.outcome is None else self.outcome.value,
            "p25": self.p25,
            "p50": self.p50,
            "primary_price_target": self.primary_price_target,
            "primary_reason": None if self.primary_reason is None else self.primary_reason.value,
            "probability_semantics": (
                None if self.probability_semantics is None else self.probability_semantics.value
            ),
            "reasons": [reason.value for reason in self.reasons],
            "selection_id": self.selection_id,
            "thresholds": None if self.thresholds is None else self.thresholds.as_dict(),
            "watchlist_rank": self.watchlist_rank,
        }
        is_target = self.classification is RecommendationState.PRICE_TARGET
        if is_target:
            payload["clv"] = None
            payload["is_best_available_market"] = False
            payload["median_ev"] = None
            payload["offered_american"] = None
            payload["offered_decimal"] = None
            payload["p25_ev"] = None
            payload["prob_ev_positive"] = None
            payload["profit"] = None
            payload["roi"] = None
            payload["unpriced_target_is_not_best_available_market"] = True
            return payload
        payload["clv"] = self.clv
        payload["median_ev"] = self.median_ev
        payload["offered_american"] = self.offered_american
        payload["offered_decimal"] = self.offered_decimal
        payload["p25_ev"] = self.p25_ev
        payload["prob_ev_positive"] = self.prob_ev_positive
        payload["profit"] = self.profit
        payload["roi"] = self.roi
        return payload


def _maturity_reason(maturity: MarketMaturity) -> NoBetReason | None:
    if maturity is MarketMaturity.QUALIFIED:
        return None
    if maturity is MarketMaturity.EXPERIMENTAL:
        return NoBetReason.MARKET_EXPERIMENTAL
    if maturity is MarketMaturity.BLOCKED:
        return NoBetReason.MARKET_BLOCKED
    never_mat: Never = maturity
    raise PolicyValidationError(f"unhandled market maturity: {never_mat!r}")


def _evaluate_identity(candidate: SelectionCandidate) -> GateResult:
    reasons: list[NoBetReason] = []
    if not candidate.identity_resolved:
        reasons.append(NoBetReason.IDENTITY_UNRESOLVED)
    if not candidate.canonical_match:
        reasons.append(NoBetReason.CANONICAL_MISMATCH)
    if candidate.ambiguous:
        reasons.append(NoBetReason.AMBIGUOUS_SELECTION)
    if candidate.replacement:
        reasons.append(NoBetReason.REPLACEMENT)
    return GateResult(gate=GateId.IDENTITY, passed=not reasons, reasons=tuple(reasons))


def _evaluate_data_quality(candidate: SelectionCandidate) -> GateResult:
    reasons: list[NoBetReason] = []
    if not candidate.data_quality_pass:
        reasons.append(NoBetReason.DATA_QUALITY)
    return GateResult(gate=GateId.DATA_QUALITY, passed=not reasons, reasons=tuple(reasons))


def _evaluate_model(candidate: SelectionCandidate, policy: RecommendationPolicy) -> GateResult:
    reasons: list[NoBetReason] = []
    if not candidate.model_qualified:
        reasons.append(NoBetReason.MODEL_UNQUALIFIED)
    if not candidate.calibrated:
        reasons.append(NoBetReason.UNCALIBRATED)
    if (
        candidate.evaluation_contract_hash is not None
        and candidate.evaluation_contract_hash != policy.evaluation_contract_hash
    ):
        reasons.append(NoBetReason.HASH_MISMATCH)
    return GateResult(gate=GateId.MODEL, passed=not reasons, reasons=tuple(reasons))


def _evaluate_uncertainty(candidate: SelectionCandidate) -> GateResult:
    reasons: list[NoBetReason] = []
    if candidate.p25 is None:
        reasons.append(NoBetReason.MISSING_P25)
    elif candidate.p25 > candidate.p50:
        reasons.append(NoBetReason.INVALID_PERCENTILES)
    return GateResult(gate=GateId.UNCERTAINTY, passed=not reasons, reasons=tuple(reasons))


def _evaluate_maturity(
    candidate: SelectionCandidate, policy: RecommendationPolicy
) -> GateResult:
    configured = policy.maturity_for(candidate.family)
    effective = candidate.market_maturity
    if configured is not MarketMaturity.QUALIFIED:
        effective = configured
    elif not policy.family_is_qualified(candidate.family):
        effective = MarketMaturity.EXPERIMENTAL
    reason = _maturity_reason(effective)
    reasons = () if reason is None else (reason,)
    return GateResult(
        gate=GateId.MARKET_MATURITY,
        passed=reason is None,
        reasons=reasons,
    )


def _evaluate_named_gate(
    gate: GateId,
    candidate: SelectionCandidate,
    policy: RecommendationPolicy,
) -> GateResult:
    if gate is GateId.IDENTITY:
        return _evaluate_identity(candidate)
    if gate is GateId.DATA_QUALITY:
        return _evaluate_data_quality(candidate)
    if gate is GateId.MODEL:
        return _evaluate_model(candidate, policy)
    if gate is GateId.UNCERTAINTY:
        return _evaluate_uncertainty(candidate)
    if gate is GateId.MARKET_MATURITY:
        return _evaluate_maturity(candidate, policy)
    if gate is GateId.QUOTE or gate is GateId.PRICE:
        raise PolicyValidationError(f"{gate.value} is not a pre-price gate")
    never_gate: Never = gate
    raise PolicyValidationError(f"unhandled gate: {never_gate!r}")


def _quote_gate_reasons(quote: QuoteEvidence) -> tuple[NoBetReason, ...]:
    reasons: list[NoBetReason] = []
    observed = require_aware(quote.observed_at, field="observed_at")
    cutoff = require_aware(quote.cutoff, field="cutoff")
    if observed > cutoff:
        reasons.append(NoBetReason.POST_CUTOFF)
    if quote.stale or quote.lifecycle == "stale":
        reasons.append(NoBetReason.STALE_LINE)
    if quote.suspended or quote.availability == "suspended":
        reasons.append(NoBetReason.SUSPENDED_LINE)
    if quote.locked or quote.lifecycle == "locked":
        reasons.append(NoBetReason.LOCKED_LINE)
    if quote.replaced or quote.lifecycle == "replaced":
        reasons.append(NoBetReason.REPLACED_LINE)
    if quote.ambiguous:
        reasons.append(NoBetReason.AMBIGUOUS_SELECTION)
    if quote.lifecycle in ELIGIBILITY_BLOCKING_LIFECYCLES and quote.lifecycle not in {
        "stale",
        "locked",
        "replaced",
    }:
        reasons.append(NoBetReason.INELIGIBLE_QUOTE)
    missing_decision = (
        not quote.eligibility_decision_identity
        or not quote.eligibility_decision_version
        or quote.eligibility_evaluated_at is None
    )
    if missing_decision:
        reasons.append(NoBetReason.MISSING_ELIGIBILITY_DECISION)
    else:
        version = str(quote.eligibility_decision_version)
        if version not in RECOGNIZED_QUOTE_ELIGIBILITY_DECISION_VERSIONS:
            reasons.append(NoBetReason.MISSING_ELIGIBILITY_DECISION)
        evaluated = require_aware(
            cast(datetime, quote.eligibility_evaluated_at),
            field="eligibility_evaluated_at",
        )
        if evaluated != cutoff:
            reasons.append(NoBetReason.INELIGIBLE_QUOTE)
        if not quote.eligible:
            reasons.append(NoBetReason.INELIGIBLE_QUOTE)
    if quote.source_kind is QuoteSourceKind.AUTOMATIC or (
        quote.source_kind is QuoteSourceKind.USER_OBSERVED
    ):
        pass
    else:
        never_kind: Never = quote.source_kind
        raise PolicyValidationError(f"unhandled quote source kind: {never_kind!r}")
    # Deduplicate while preserving order.
    seen: set[NoBetReason] = set()
    ordered: list[NoBetReason] = []
    for reason in reasons:
        if reason not in seen:
            seen.add(reason)
            ordered.append(reason)
    return tuple(ordered)


def _production_uncertainty(candidate: SelectionCandidate) -> bool:
    return candidate.bootstrap_successful_count == PRODUCTION_BOOTSTRAP_REFITS


def _price_gate_reasons(
    candidate: SelectionCandidate,
    policy: RecommendationPolicy,
    thresholds: RenderedThresholds,
    *,
    p25_ev: float | None,
) -> tuple[NoBetReason, ...]:
    reasons: list[NoBetReason] = []
    quote = candidate.quote
    if quote is None:
        return ()
    if not offered_meets_actionable(
        offered_decimal=quote.offered_decimal,
        thresholds=compute_price_thresholds(
            candidate.p50,
            cast(float, candidate.p25),
            family=candidate.family,
        ),
    ):
        reasons.append(NoBetReason.BELOW_ACTIONABLE)
    if not _production_uncertainty(candidate):
        if candidate.bootstrap_successful_count is None:
            reasons.append(NoBetReason.MISSING_BOOTSTRAP)
        else:
            reasons.append(NoBetReason.NONPRODUCTION_UNCERTAINTY)
    if candidate.prob_ev_positive is None:
        reasons.append(NoBetReason.MISSING_PROB_EV_POSITIVE)
    else:
        minimum = policy.min_prob_ev_positive(candidate.family)
        if candidate.prob_ev_positive < minimum:
            reasons.append(NoBetReason.PROB_EV_POSITIVE_LOW)
    if p25_ev is None or p25_ev < 0.0:
        reasons.append(NoBetReason.P25_EV_NONPOSITIVE)
    del thresholds
    seen: set[NoBetReason] = set()
    ordered: list[NoBetReason] = []
    for reason in reasons:
        if reason not in seen:
            seen.add(reason)
            ordered.append(reason)
    return tuple(ordered)


def observed_ev(
    candidate: SelectionCandidate,
    *,
    probability: float,
    offered_decimal: float,
) -> tuple[float, str]:
    """Return (ev, semantics label) for an observed decimal price.

    Thresholds always use the candidate p50/p25 as provided (conditional
    non-void for void-on-draw families). Exact per-stake EV uses
    unconditional components when present and does not condition twice.
    """
    family = candidate.family
    voids = family in VOID_ON_DRAW_FAMILIES
    if (
        voids
        and candidate.p_win_unconditional is not None
        and candidate.p_void is not None
        and probability == candidate.p50
    ):
        return (
            expected_value_with_void(
                p_win=candidate.p_win_unconditional,
                p_void=candidate.p_void,
                offered_decimal=offered_decimal,
            ),
            EV_LABEL_EXACT,
        )
    if candidate.probability_semantics is ProbabilitySemantics.CONDITIONAL_NONVOID:
        return expected_value(probability, offered_decimal), EV_LABEL_CONDITIONAL
    if candidate.probability_semantics is ProbabilitySemantics.EXHAUSTIVE:
        if (
            voids
            and candidate.p_void not in (None, 0.0)
            and probability == candidate.p50
            and candidate.p_win_unconditional is None
        ):
            return expected_value(probability, offered_decimal), EV_LABEL_CONDITIONAL
        return expected_value(probability, offered_decimal), EV_LABEL_EXHAUSTIVE
    never_sem: Never = candidate.probability_semantics
    raise PolicyValidationError(f"unhandled probability semantics: {never_sem!r}")


def ranking_p25_ev(candidate: SelectionCandidate, offered_decimal: float) -> float:
    """Frozen ranking score: p25 EV in the policy's declared semantics."""
    p25 = candidate.p25
    if p25 is None:
        raise PolicyValidationError("ranking p25 EV requires a valid p25")
    if candidate.probability_semantics is ProbabilitySemantics.CONDITIONAL_NONVOID:
        return expected_value(p25, offered_decimal)
    if candidate.probability_semantics is ProbabilitySemantics.EXHAUSTIVE:
        return expected_value(p25, offered_decimal)
    never_sem: Never = candidate.probability_semantics
    raise PolicyValidationError(f"unhandled probability semantics: {never_sem!r}")


def _no_bet_decision(
    *,
    candidate: SelectionCandidate | None,
    event_id: str,
    bout_id: str,
    selection_id: str,
    reasons: Sequence[NoBetReason],
    policy: RecommendationPolicy,
    gate_trace: GateTrace,
    thresholds: RenderedThresholds | None = None,
    family: MarketFamily | None = None,
    outcome: OutcomeKey | None = None,
    line_point: float | None = None,
    p50: float | None = None,
    p25: float | None = None,
    probability_semantics: ProbabilitySemantics | None = None,
    p25_ev: float | None = None,
    median_ev: float | None = None,
    ev_semantics_label: str | None = None,
    offered_decimal: float | None = None,
    offered_american: float | None = None,
    prob_ev_positive: float | None = None,
    detail: str = "",
) -> SelectionDecision:
    ordered = tuple(reasons)
    return SelectionDecision(
        event_id=event_id,
        bout_id=bout_id,
        selection_id=selection_id,
        family=family if candidate is None else candidate.family,
        outcome=outcome if candidate is None else candidate.outcome,
        line_point=line_point if candidate is None else candidate.line_point,
        classification=RecommendationState.NO_BET,
        reasons=ordered,
        primary_reason=policy.primary_reason(ordered),
        gate_trace=gate_trace,
        thresholds=thresholds,
        p50=p50 if candidate is None else candidate.p50,
        p25=p25 if candidate is None else candidate.p25,
        probability_semantics=(
            probability_semantics if candidate is None else candidate.probability_semantics
        ),
        p25_ev=p25_ev,
        median_ev=median_ev,
        ev_semantics_label=ev_semantics_label,
        offered_decimal=offered_decimal,
        offered_american=offered_american,
        prob_ev_positive=prob_ev_positive if candidate is None else candidate.prob_ev_positive,
        detail=detail,
    )


def malformed_no_bet(
    *,
    event_id: str,
    bout_id: str,
    selection_id: str,
    policy: RecommendationPolicy,
    detail: str,
    extra_reasons: Sequence[NoBetReason] = (),
) -> SelectionDecision:
    reasons = (NoBetReason.MALFORMED_CANDIDATE, *tuple(extra_reasons))
    return _no_bet_decision(
        candidate=None,
        event_id=event_id or "unknown-event",
        bout_id=bout_id or "unknown-bout",
        selection_id=selection_id or "malformed",
        reasons=reasons,
        policy=policy,
        gate_trace=GateTrace(
            results=(
                GateResult(
                    gate=GateId.IDENTITY,
                    passed=False,
                    reasons=(NoBetReason.MALFORMED_CANDIDATE,),
                ),
            )
        ),
        family=None,
        outcome=None,
        detail=detail,
    )


def evaluate_selection(
    candidate: SelectionCandidate,
    policy: RecommendationPolicy,
) -> SelectionDecision:
    """Evaluate one selection. Failed pre-price gates never consult price."""
    pre_results = [
        _evaluate_named_gate(gate, candidate, policy) for gate in policy.gate_order
    ]
    pre_trace = GateTrace(results=tuple(pre_results))
    if not pre_trace.pre_price_passed():
        return _no_bet_decision(
            candidate=candidate,
            event_id=candidate.event_id,
            bout_id=candidate.bout_id,
            selection_id=candidate.selection_id,
            reasons=pre_trace.failed_reasons,
            policy=policy,
            gate_trace=pre_trace,
        )

    thresholds = render_thresholds(
        candidate.p50, cast(float, candidate.p25), family=candidate.family
    )
    quote = candidate.quote
    if quote is None:
        return SelectionDecision(
            event_id=candidate.event_id,
            bout_id=candidate.bout_id,
            selection_id=candidate.selection_id,
            family=candidate.family,
            outcome=candidate.outcome,
            line_point=candidate.line_point,
            classification=RecommendationState.PRICE_TARGET,
            reasons=(),
            primary_reason=None,
            gate_trace=pre_trace,
            thresholds=thresholds,
            p50=candidate.p50,
            p25=candidate.p25,
            probability_semantics=candidate.probability_semantics,
            p25_ev=None,
            median_ev=None,
            ev_semantics_label=None,
            offered_decimal=None,
            offered_american=None,
            prob_ev_positive=None,
        )

    quote_reasons = _quote_gate_reasons(quote)
    quote_result = GateResult(
        gate=GateId.QUOTE, passed=not quote_reasons, reasons=quote_reasons
    )
    offered_american = decimal_to_american(quote.offered_decimal)
    p25_ev = ranking_p25_ev(candidate, quote.offered_decimal)
    median_ev, ev_label = observed_ev(
        candidate, probability=candidate.p50, offered_decimal=quote.offered_decimal
    )
    price_reasons = _price_gate_reasons(
        candidate, policy, thresholds, p25_ev=p25_ev
    )
    price_result = GateResult(
        gate=GateId.PRICE, passed=not price_reasons, reasons=price_reasons
    )
    full_trace = GateTrace(results=(*pre_results, quote_result, price_result))
    failed = full_trace.failed_reasons
    if failed:
        return _no_bet_decision(
            candidate=candidate,
            event_id=candidate.event_id,
            bout_id=candidate.bout_id,
            selection_id=candidate.selection_id,
            reasons=failed,
            policy=policy,
            gate_trace=full_trace,
            thresholds=thresholds,
            p25_ev=p25_ev,
            median_ev=median_ev,
            ev_semantics_label=ev_label,
            offered_decimal=quote.offered_decimal,
            offered_american=offered_american,
        )
    return SelectionDecision(
        event_id=candidate.event_id,
        bout_id=candidate.bout_id,
        selection_id=candidate.selection_id,
        family=candidate.family,
        outcome=candidate.outcome,
        line_point=candidate.line_point,
        classification=RecommendationState.CONFIRMED_VALUE,
        reasons=(),
        primary_reason=None,
        gate_trace=full_trace,
        thresholds=thresholds,
        p50=candidate.p50,
        p25=candidate.p25,
        probability_semantics=candidate.probability_semantics,
        p25_ev=p25_ev,
        median_ev=median_ev,
        ev_semantics_label=ev_label,
        offered_decimal=quote.offered_decimal,
        offered_american=offered_american,
        prob_ev_positive=candidate.prob_ev_positive,
    )


def coerce_candidate(
    raw: Mapping[str, Any] | SelectionCandidate,
    policy: RecommendationPolicy,
) -> SelectionCandidate | SelectionDecision:
    """Build a typed candidate or a fail-closed no-bet. Never raises past the bout."""
    if isinstance(raw, SelectionCandidate):
        try:
            # Re-run post-init invariants by reconstructing.
            return SelectionCandidate(**raw.__dict__)
        except (TypeError, ValueError) as exc:
            return malformed_no_bet(
                event_id=raw.event_id,
                bout_id=raw.bout_id,
                selection_id=raw.selection_id,
                policy=policy,
                detail=str(exc),
            )
    event_id = str(raw.get("event_id") or "")
    bout_id = str(raw.get("bout_id") or "")
    try:
        family = MarketFamily(str(raw["family"]))
        outcome = OutcomeKey(str(raw["outcome"]))
        line_point = raw.get("line_point")
        line_value = None if line_point is None else float(line_point)
        selection_id = str(
            raw.get("selection_id")
            or canonical_selection_id(
                event_id=event_id,
                bout_id=bout_id,
                family=family,
                outcome=outcome,
                line_point=line_value,
            )
        )
        quote_raw = raw.get("quote")
        quote: QuoteEvidence | None
        if quote_raw is None:
            quote = None
        elif isinstance(quote_raw, QuoteEvidence):
            quote = quote_raw
        elif isinstance(quote_raw, Mapping):
            quote = QuoteEvidence(
                offered_decimal=float(quote_raw["offered_decimal"]),
                source_kind=QuoteSourceKind(str(quote_raw["source_kind"])),
                observed_at=quote_raw["observed_at"],
                cutoff=quote_raw["cutoff"],
                bookmaker_key=quote_raw.get("bookmaker_key"),
                region=quote_raw.get("region"),
                eligibility_decision_identity=quote_raw.get("eligibility_decision_identity"),
                eligibility_decision_version=quote_raw.get("eligibility_decision_version"),
                eligibility_evaluated_at=quote_raw.get("eligibility_evaluated_at"),
                eligible=bool(quote_raw.get("eligible", False)),
                availability=str(quote_raw.get("availability") or "unknown"),
                lifecycle=str(quote_raw.get("lifecycle") or "unresolved"),
                freshness_at=quote_raw.get("freshness_at"),
                stale=bool(quote_raw.get("stale", False)),
                suspended=bool(quote_raw.get("suspended", False)),
                locked=bool(quote_raw.get("locked", False)),
                replaced=bool(quote_raw.get("replaced", False)),
                ambiguous=bool(quote_raw.get("ambiguous", False)),
            )
        else:
            raise ValueError("quote must be a mapping or QuoteEvidence")
        maturity_raw = raw.get("market_maturity")
        if maturity_raw is None:
            maturity = policy.maturity_for(family)
        else:
            maturity = MarketMaturity(str(maturity_raw))
        return SelectionCandidate(
            event_id=event_id,
            bout_id=bout_id,
            selection_id=selection_id,
            family=family,
            outcome=outcome,
            line_point=line_value,
            p50=float(raw["p50"]),
            p25=None if raw.get("p25") is None else float(raw["p25"]),
            probability_semantics=ProbabilitySemantics(
                str(raw.get("probability_semantics") or ProbabilitySemantics.EXHAUSTIVE.value)
            ),
            bootstrap_successful_count=(
                None
                if raw.get("bootstrap_successful_count") is None
                else int(raw["bootstrap_successful_count"])
            ),
            bootstrap_seed=(
                None if raw.get("bootstrap_seed") is None else int(raw["bootstrap_seed"])
            ),
            estimator_hash=str(raw["estimator_hash"]),
            calibration_hash=(
                None if raw.get("calibration_hash") is None else str(raw["calibration_hash"])
            ),
            data_hash=str(raw["data_hash"]),
            config_hash=str(raw["config_hash"]),
            identity_resolved=bool(raw.get("identity_resolved", True)),
            canonical_match=bool(raw.get("canonical_match", True)),
            ambiguous=bool(raw.get("ambiguous", False)),
            replacement=bool(raw.get("replacement", False)),
            data_quality_pass=bool(raw.get("data_quality_pass", True)),
            model_qualified=bool(raw.get("model_qualified", True)),
            calibrated=bool(raw.get("calibrated", True)),
            market_maturity=maturity,
            p_win_unconditional=(
                None
                if raw.get("p_win_unconditional") is None
                else float(raw["p_win_unconditional"])
            ),
            p_void=None if raw.get("p_void") is None else float(raw["p_void"]),
            evaluation_contract_hash=(
                None
                if raw.get("evaluation_contract_hash") is None
                else str(raw["evaluation_contract_hash"])
            ),
            quote=quote,
            prob_ev_positive=(
                None if raw.get("prob_ev_positive") is None else float(raw["prob_ev_positive"])
            ),
            production_uncertainty=bool(raw.get("production_uncertainty", False)),
        )
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        return malformed_no_bet(
            event_id=event_id,
            bout_id=bout_id,
            selection_id=str(raw.get("selection_id") or "malformed"),
            policy=policy,
            detail=str(exc),
            extra_reasons=(NoBetReason.UNSUPPORTED_SELECTION,),
        )


__all__ = [
    "EV_LABEL_CONDITIONAL",
    "EV_LABEL_EXACT",
    "EV_LABEL_EXHAUSTIVE",
    "EXPECTED_POLICY_VERSION",
    "EXPECTED_SCHEMA_VERSION",
    "PINNED_POLICY_HASH",
    "POLICY_FILENAME",
    "POLICY_ID",
    "PRODUCTION_BOOTSTRAP_REFITS",
    "GateId",
    "GateResult",
    "GateTrace",
    "NoBetReason",
    "PolicyContractDriftError",
    "PolicyHashMismatch",
    "PolicyValidationError",
    "PolicyVersionMismatch",
    "ProbabilitySemantics",
    "QuoteEvidence",
    "QuoteSourceKind",
    "RecommendationPolicy",
    "RecommendationPolicyError",
    "RenderedThresholds",
    "SelectionCandidate",
    "SelectionDecision",
    "canonical_selection_id",
    "coerce_candidate",
    "compute_policy_hash",
    "evaluate_selection",
    "format_american_or_better",
    "load_recommendation_policy",
    "malformed_no_bet",
    "observed_ev",
    "package_policy_resource_path",
    "policy_path",
    "ranking_p25_ev",
    "render_thresholds",
    "visible_policy_path",
]
