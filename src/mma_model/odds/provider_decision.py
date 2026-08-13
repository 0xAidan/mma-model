"""Packaged DWCS-202 odds decision contract and licensed-adapter gate.

Authoritative bytes ship inside ``mma_model.odds.odds_decision_v1.yaml``.
``config/sources/odds.yaml`` is the plan-visible symlink in a checkout.
Runtime never depends on checkout-only evidence paths.

Loaded contracts are deeply immutable (frozen Pydantic models + mapping proxies).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
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
    GetCoreSchemaHandler,
    ValidationError,
    field_validator,
)
from pydantic_core import core_schema

DECISION_PATH_REFERENCE_FALLBACK: Final[str] = "the_odds_api_reference_fallback"
DECISION_PATH_LICENSED_BET365: Final[str] = "licensed_bet365_primary"
DECISION_PATH_HARD_BLOCKER: Final[str] = "hard_blocker"

CONTRACT_ID: Final[str] = "dwcs_odds_decision"
EXPECTED_CONTRACT_VERSION: Final[str] = "1.0.0"
EXPECTED_SCHEMA_VERSION: Final[int] = 1
DECISION_FILENAME: Final[str] = "odds_decision_v1.yaml"
PINNED_ODDS_DECISION_HASH: Final[str] = (
    "85e036e1717ba9df41bd31ed7aed1e2fcc1a54747fc0175ce5d53679ac6a1637"
)

_FORBIDDEN_SCRAPER_SUFFIXES: Final[tuple[str, ...]] = (
    "bet365_scraper",
    "sportsbook_scraper",
    "scrape_bet365",
    "scrape_sportsbook",
)

_REQUIRED_TRIAL_STATUSES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "opticodds": "not_configured",
        "sportsgameodds": "not_configured",
        "sportsdataio": "not_configured",
    }
)


class LicensedBookmakerAdapterError(RuntimeError):
    """Raised when code attempts a licensed adapter that Phase 0 did not authorize."""


class OddsDecisionError(RuntimeError):
    """Invalid or drifted odds decision contract."""


class OddsDecisionHashMismatch(OddsDecisionError):
    """Pinned digest mismatch."""


class FrozenStrMapping(Mapping[str, str]):
    """Read-only str→str mapping that Pydantic will not coerce back to dict."""

    __slots__ = ("_data",)

    def __init__(self, data: Mapping[str, Any]) -> None:
        self._data = MappingProxyType({str(k): str(v) for k, v in data.items()})

    def __getitem__(self, key: str) -> str:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return f"FrozenStrMapping({dict(self._data)!r})"

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        _source: Any,
        _handler: GetCoreSchemaHandler,
    ) -> core_schema.CoreSchema:
        def _validate(value: Any) -> FrozenStrMapping:
            if isinstance(value, cls):
                return value
            if not isinstance(value, Mapping):
                raise TypeError("expected a mapping")
            return cls(value)

        return core_schema.no_info_plain_validator_function(
            _validate,
            serialization=core_schema.plain_serializer_function_ser_schema(
                lambda v: dict(v),
                info_arg=False,
                return_schema=core_schema.dict_schema(
                    core_schema.str_schema(),
                    core_schema.str_schema(),
                ),
            ),
        )


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class OddsDecisionBlock(_FrozenModel):
    path: Literal["the_odds_api_reference_fallback"]
    licensed_bookmaker_adapter_authorized: Literal[False]
    bet365_dwcs_status: Literal["scoped_absent"]
    rationale: str


class OddsReferenceBlock(_FrozenModel):
    provider: Literal["the_odds_api"]
    role: str
    never_label_as_bet365: Literal[True]


class OddsManualObservationBlock(_FrozenModel):
    source_label: Literal["user_observed"]
    automated: Literal[False]
    required_for_price_targets: bool
    required_for_exact_ev: bool
    retention: str


class OddsEvidenceRefs(_FrozenModel):
    phase0_summary: str
    audit_doc: str


class OddsDecisionContract(_FrozenModel):
    """Deeply immutable packaged odds decision contract."""

    contract_id: Literal["dwcs_odds_decision"]
    contract_version: Literal["1.0.0"]
    schema_version: Literal[1]
    ticket: Literal["DWCS-202"]
    decision: OddsDecisionBlock
    trial_providers: FrozenStrMapping
    reference_odds: OddsReferenceBlock
    manual_observation: OddsManualObservationBlock
    line_lifecycle_states: tuple[str, ...]
    prohibited: tuple[str, ...]
    evidence_refs: OddsEvidenceRefs
    content_hash: str = Field(min_length=64, max_length=64)

    @field_validator("line_lifecycle_states", "prohibited", mode="before")
    @classmethod
    def _tupleize_str_sequences(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        return tuple(str(item) for item in value)

    def as_readonly_mapping(self) -> Mapping[str, Any]:
        """Compatibility read-only nested mapping (deep MappingProxyType/tuple)."""
        return _deep_freeze(self.model_dump(mode="python"))


class Phase0OddsDecision(_FrozenModel):
    path: str
    bet365_dwcs_status: str
    licensed_bookmaker_adapter_authorized: bool
    rationale: str
    evidence_path: str
    trial_providers: FrozenStrMapping
    content_hash: str
    contract_version: str


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, FrozenStrMapping):
        return MappingProxyType(dict(value))
    if isinstance(value, Mapping):
        return MappingProxyType({str(k): _deep_freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_deep_freeze(v) for v in value)
    if isinstance(value, tuple):
        return tuple(_deep_freeze(v) for v in value)
    return value


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def package_decision_resource_path() -> Path:
    """Filesystem path to the packaged decision YAML (checkout / wheel)."""
    root = resources.files("mma_model.odds")
    target = root.joinpath(DECISION_FILENAME)
    with resources.as_file(target) as path:
        return Path(path)


def visible_decision_path() -> Path:
    """Plan-visible config path (symlink to packaged bytes in checkout)."""
    return _repo_root() / "config" / "sources" / "odds.yaml"


def compute_odds_decision_hash(payload: Mapping[str, Any]) -> str:
    """SHA-256 of canonical JSON (sorted keys, compact)."""
    raw = json.dumps(
        dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise OddsDecisionError(f"unable to read odds decision: {exc}") from exc
    if not isinstance(payload, dict):
        raise OddsDecisionError("odds decision root must be a mapping")
    return payload


def _read_package_payload() -> dict[str, Any]:
    root = resources.files("mma_model.odds")
    resource = root.joinpath(DECISION_FILENAME)
    try:
        raw = resource.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, AttributeError) as exc:
        raise OddsDecisionError(
            f"unable to read packaged odds decision resource {DECISION_FILENAME}"
        ) from exc
    payload = yaml.safe_load(raw)
    if not isinstance(payload, dict):
        raise OddsDecisionError("odds decision root must be a mapping")
    return payload


def _validate_raw_payload(
    payload: Mapping[str, Any],
    *,
    enforce_pinned_digest: bool,
) -> OddsDecisionContract:
    content_hash = compute_odds_decision_hash(payload)
    if enforce_pinned_digest and content_hash != PINNED_ODDS_DECISION_HASH:
        raise OddsDecisionHashMismatch(
            f"content hash mismatch versus pinned digest: got {content_hash}, "
            f"expected {PINNED_ODDS_DECISION_HASH}"
        )

    decision = payload.get("decision")
    if not isinstance(decision, Mapping):
        raise OddsDecisionError("decision must be a mapping")
    path = str(decision.get("path") or "")
    if path != DECISION_PATH_REFERENCE_FALLBACK:
        raise LicensedBookmakerAdapterError(
            f"unsupported Phase 0 decision path for DWCS-202 fallback: {path!r}"
        )
    if bool(decision.get("licensed_bookmaker_adapter_authorized")):
        raise LicensedBookmakerAdapterError(
            "contract claims licensed_bookmaker_adapter_authorized=true but "
            "DWCS-202 fallback refuses to invent a licensed adapter"
        )

    trials = payload.get("trial_providers")
    if not isinstance(trials, Mapping):
        raise OddsDecisionError("trial_providers must be a mapping")
    for name, expected in _REQUIRED_TRIAL_STATUSES.items():
        got = str(trials.get(name) or "")
        if got != expected:
            raise OddsDecisionError(
                f"trial provider status drift for {name}: got {got!r}, "
                f"expected {expected!r}"
            )

    prohibited = payload.get("prohibited")
    if not isinstance(prohibited, list) or "sportsbook_website_scraping" not in prohibited:
        raise OddsDecisionError("prohibited must include sportsbook_website_scraping")

    try:
        return OddsDecisionContract.model_validate(
            {
                **dict(payload),
                "trial_providers": FrozenStrMapping(dict(trials)),
                "line_lifecycle_states": tuple(
                    str(x) for x in (payload.get("line_lifecycle_states") or ())
                ),
                "prohibited": tuple(str(x) for x in prohibited),
                "content_hash": content_hash,
            }
        )
    except ValidationError as exc:
        raise OddsDecisionError(str(exc)) from exc


def _cross_check_visible_symlink(package_payload: Mapping[str, Any]) -> None:
    visible = visible_decision_path()
    if not visible.exists():
        return
    visible_payload = _read_yaml_mapping(visible)
    if compute_odds_decision_hash(visible_payload) != compute_odds_decision_hash(
        package_payload
    ):
        raise OddsDecisionHashMismatch(
            "visible config/sources/odds.yaml drifted from packaged "
            f"{DECISION_FILENAME}"
        )


def _cross_check_phase0_evidence(decision: Phase0OddsDecision) -> None:
    refs = _repo_root() / "output" / "research" / "odds-coverage-summary.json"
    if not refs.is_file():
        return
    try:
        payload = json.loads(refs.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OddsDecisionError(f"unable to read Phase 0 evidence: {exc}") from exc
    evidence_decision = payload.get("decision") or {}
    if str(evidence_decision.get("path") or "") != decision.path:
        raise OddsDecisionError(
            "Phase 0 evidence decision.path drifted from packaged contract"
        )
    if str(evidence_decision.get("bet365_dwcs_status") or "") != decision.bet365_dwcs_status:
        raise OddsDecisionError(
            "Phase 0 evidence bet365_dwcs_status drifted from packaged contract"
        )
    providers = payload.get("providers") or {}
    for name, expected in decision.trial_providers.items():
        status = str((providers.get(name) or {}).get("status") or "")
        if status != expected:
            raise OddsDecisionError(
                f"Phase 0 evidence provider status drift for {name}: "
                f"got {status!r}, expected {expected!r}"
            )


@lru_cache(maxsize=1)
def load_odds_decision_contract(
    *,
    enforce_pinned_digest: bool = True,
) -> OddsDecisionContract:
    """Load the authoritative packaged odds decision contract (immutable)."""
    payload = _read_package_payload()
    contract = _validate_raw_payload(
        payload, enforce_pinned_digest=enforce_pinned_digest
    )
    _cross_check_visible_symlink(payload)
    return contract


@lru_cache(maxsize=1)
def load_odds_source_config() -> Mapping[str, Any]:
    """Compatibility read-only mapping view of the packaged decision contract."""
    return load_odds_decision_contract().as_readonly_mapping()


@lru_cache(maxsize=1)
def load_phase0_odds_decision() -> Phase0OddsDecision:
    """Load Phase 0 decision from the packaged contract; never invent approval."""
    contract = load_odds_decision_contract()
    result = Phase0OddsDecision(
        path=contract.decision.path,
        bet365_dwcs_status=contract.decision.bet365_dwcs_status,
        licensed_bookmaker_adapter_authorized=False,
        rationale=contract.decision.rationale.strip(),
        evidence_path=contract.evidence_refs.phase0_summary,
        trial_providers=FrozenStrMapping(contract.trial_providers),
        content_hash=contract.content_hash,
        contract_version=contract.contract_version,
    )
    _cross_check_phase0_evidence(result)
    return result


def licensed_bookmaker_adapter_authorized() -> bool:
    return load_phase0_odds_decision().licensed_bookmaker_adapter_authorized


def require_licensed_bookmaker_adapter(provider: str) -> None:
    """Hard-fail any attempt to use an unauthorized licensed bookmaker feed."""
    decision = load_phase0_odds_decision()
    if decision.licensed_bookmaker_adapter_authorized:
        return
    raise LicensedBookmakerAdapterError(
        f"licensed bookmaker adapter {provider!r} is not authorized "
        f"(Phase 0 path={decision.path!r}, bet365_dwcs_status="
        f"{decision.bet365_dwcs_status!r}). Use sportsbook-agnostic price "
        "targets and optional user_observed prices instead."
    )


def assert_no_sportsbook_scraper_modules() -> None:
    """Scoped package heuristic: flag scraper-shaped modules under mma_model.odds.

    This is **not** proof that no scraper exists elsewhere in the repository.
    Repo-wide absence still relies on review and broader tests.
    """
    odds_dir = Path(__file__).resolve().parent
    self_name = Path(__file__).name
    offenders: list[str] = []
    for path in odds_dir.glob("*.py"):
        if path.name == self_name:
            continue
        stem = path.stem.lower()
        if any(token in stem for token in _FORBIDDEN_SCRAPER_SUFFIXES):
            offenders.append(path.name)
        text = path.read_text(encoding="utf-8")
        lowered = text.lower()
        if "def scrape_" in lowered or "class sportsbookscraper" in lowered.replace(
            " ", ""
        ):
            offenders.append(path.name)
        if "BET365_PASSWORD" in text or "SPORTSBOOK_COOKIE" in text:
            offenders.append(path.name)
    if offenders:
        raise AssertionError(
            "scoped odds-package scraper / credential heuristic hit: "
            + ", ".join(sorted(set(offenders)))
        )
