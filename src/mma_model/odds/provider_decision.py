"""Packaged DWCS-202 odds decision contract and licensed-adapter gate.

Authoritative bytes ship inside ``mma_model.odds.odds_decision_v1.yaml``.
``config/sources/odds.yaml`` is the plan-visible symlink in a checkout.
Runtime never depends on checkout-only evidence paths.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any, Final

import yaml

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

# Scoped filename heuristics inside mma_model.odds only (not a repo-wide proof).
_FORBIDDEN_SCRAPER_SUFFIXES: Final[tuple[str, ...]] = (
    "bet365_scraper",
    "sportsbook_scraper",
    "scrape_bet365",
    "scrape_sportsbook",
)

_REQUIRED_TRIAL_STATUSES: Final[Mapping[str, str]] = {
    "opticodds": "not_configured",
    "sportsgameodds": "not_configured",
    "sportsdataio": "not_configured",
}


class LicensedBookmakerAdapterError(RuntimeError):
    """Raised when code attempts a licensed adapter that Phase 0 did not authorize."""


class OddsDecisionError(RuntimeError):
    """Invalid or drifted odds decision contract."""


class OddsDecisionHashMismatch(OddsDecisionError):
    """Pinned digest mismatch."""


@dataclass(frozen=True)
class Phase0OddsDecision:
    path: str
    bet365_dwcs_status: str
    licensed_bookmaker_adapter_authorized: bool
    rationale: str
    evidence_path: str
    trial_providers: dict[str, str]
    content_hash: str
    contract_version: str


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
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
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


def _validate_payload(
    payload: Mapping[str, Any],
    *,
    enforce_pinned_digest: bool,
) -> dict[str, Any]:
    content_hash = compute_odds_decision_hash(payload)
    if payload.get("contract_id") != CONTRACT_ID:
        raise OddsDecisionError(
            f"contract_id mismatch: got {payload.get('contract_id')!r}, "
            f"expected {CONTRACT_ID!r}"
        )
    if payload.get("schema_version") != EXPECTED_SCHEMA_VERSION:
        raise OddsDecisionError(
            f"schema_version mismatch: got {payload.get('schema_version')!r}, "
            f"expected {EXPECTED_SCHEMA_VERSION!r}"
        )
    if payload.get("contract_version") != EXPECTED_CONTRACT_VERSION:
        raise OddsDecisionError(
            "contract_version mismatch: "
            f"got {payload.get('contract_version')!r}, "
            f"expected {EXPECTED_CONTRACT_VERSION!r}"
        )
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
    if str(decision.get("bet365_dwcs_status") or "") != "scoped_absent":
        raise OddsDecisionError(
            "bet365_dwcs_status drift: expected 'scoped_absent', got "
            f"{decision.get('bet365_dwcs_status')!r}"
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

    manual = payload.get("manual_observation")
    if not isinstance(manual, Mapping):
        raise OddsDecisionError("manual_observation must be a mapping")
    if manual.get("source_label") != "user_observed":
        raise OddsDecisionError("manual_observation.source_label must be user_observed")
    if manual.get("automated") is not False:
        raise OddsDecisionError("manual_observation.automated must be false")

    reference = payload.get("reference_odds")
    if not isinstance(reference, Mapping):
        raise OddsDecisionError("reference_odds must be a mapping")
    if reference.get("never_label_as_bet365") is not True:
        raise OddsDecisionError("reference_odds.never_label_as_bet365 must be true")

    prohibited = payload.get("prohibited")
    if not isinstance(prohibited, list) or "sportsbook_website_scraping" not in prohibited:
        raise OddsDecisionError("prohibited must include sportsbook_website_scraping")

    return {**dict(payload), "content_hash": content_hash}


def _cross_check_visible_symlink(package_payload: Mapping[str, Any]) -> None:
    """Fail closed when checkout symlink exists but drifts from packaged authority."""
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
    """When checkout evidence exists, require matching path/status cells."""
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
) -> dict[str, Any]:
    """Load the authoritative packaged odds decision contract."""
    payload = _read_package_payload()
    validated = _validate_payload(payload, enforce_pinned_digest=enforce_pinned_digest)
    _cross_check_visible_symlink(payload)
    return validated


@lru_cache(maxsize=1)
def load_odds_source_config() -> dict[str, Any]:
    """Compatibility alias: returns the packaged decision contract mapping."""
    return load_odds_decision_contract()


@lru_cache(maxsize=1)
def load_phase0_odds_decision() -> Phase0OddsDecision:
    """Load Phase 0 decision from the packaged contract; never invent approval."""
    payload = load_odds_decision_contract()
    decision = payload["decision"]
    trials = {str(k): str(v) for k, v in dict(payload["trial_providers"]).items()}
    evidence_refs = payload.get("evidence_refs") or {}
    result = Phase0OddsDecision(
        path=str(decision["path"]),
        bet365_dwcs_status=str(decision["bet365_dwcs_status"]),
        licensed_bookmaker_adapter_authorized=False,
        rationale=str(decision.get("rationale") or "").strip(),
        evidence_path=str(
            evidence_refs.get("phase0_summary")
            or "output/research/odds-coverage-summary.json"
        ),
        trial_providers=trials,
        content_hash=str(payload["content_hash"]),
        contract_version=str(payload["contract_version"]),
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
