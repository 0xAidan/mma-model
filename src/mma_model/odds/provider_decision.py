"""Phase 0 licensed-bookmaker adapter gate (DWCS-202).

Committed DWCS-000 evidence selected ``the_odds_api_reference_fallback``.
No licensed bookmaker-specific provider passed; do not invent adapters.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

import yaml

DECISION_PATH_REFERENCE_FALLBACK: Final[str] = "the_odds_api_reference_fallback"
DECISION_PATH_LICENSED_BET365: Final[str] = "licensed_bet365_primary"
DECISION_PATH_HARD_BLOCKER: Final[str] = "hard_blocker"

_ODDS_YAML_NAME: Final[str] = "odds.yaml"
_PHASE0_SUMMARY_REL: Final[str] = "output/research/odds-coverage-summary.json"

# Modules that would constitute a sportsbook scraper (must stay absent).
_FORBIDDEN_SCRAPER_SUFFIXES: Final[tuple[str, ...]] = (
    "bet365_scraper",
    "sportsbook_scraper",
    "scrape_bet365",
    "scrape_sportsbook",
)


class LicensedBookmakerAdapterError(RuntimeError):
    """Raised when code attempts a licensed adapter that Phase 0 did not authorize."""


@dataclass(frozen=True)
class Phase0OddsDecision:
    path: str
    bet365_dwcs_status: str
    licensed_bookmaker_adapter_authorized: bool
    rationale: str
    evidence_path: str
    trial_providers: dict[str, str]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def odds_config_path() -> Path:
    return _repo_root() / "config" / "sources" / _ODDS_YAML_NAME


def phase0_evidence_path() -> Path:
    return _repo_root() / _PHASE0_SUMMARY_REL


@lru_cache(maxsize=1)
def load_odds_source_config() -> dict[str, Any]:
    path = odds_config_path()
    if not path.is_file():
        raise FileNotFoundError(f"missing odds source config: {path}")
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("config/sources/odds.yaml must be a mapping")
    return payload


@lru_cache(maxsize=1)
def load_phase0_odds_decision() -> Phase0OddsDecision:
    """Load the committed DWCS-000 decision; never infer licensed approval."""
    evidence = phase0_evidence_path()
    if not evidence.is_file():
        raise FileNotFoundError(f"missing Phase 0 odds evidence: {evidence}")
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    decision = payload.get("decision") or {}
    path = str(decision.get("path") or "")
    if path != DECISION_PATH_REFERENCE_FALLBACK:
        # Fail closed: only the committed fallback path is implemented in DWCS-202.
        if path == DECISION_PATH_LICENSED_BET365:
            raise LicensedBookmakerAdapterError(
                "committed evidence claims licensed_bet365_primary but DWCS-202 "
                "fallback branch refuses to invent a Bet365 adapter without a "
                "separate measured implementation ticket"
            )
        raise LicensedBookmakerAdapterError(
            f"unsupported Phase 0 decision path for DWCS-202 fallback: {path!r}"
        )

    providers = payload.get("providers") or {}
    trial_status: dict[str, str] = {}
    for name in ("opticodds", "sportsgameodds", "sportsdataio"):
        block = providers.get(name) or {}
        trial_status[name] = str(block.get("status") or "unknown")

    # Licensed bookmaker adapters require an evidence-backed primary path.
    # Reference Odds API consensus is never a licensed sportsbook adapter pass.
    authorized = False

    bet365_status = str(decision.get("bet365_dwcs_status") or "unresolved")
    rationale = str(decision.get("rationale") or "")
    return Phase0OddsDecision(
        path=path,
        bet365_dwcs_status=bet365_status,
        licensed_bookmaker_adapter_authorized=authorized,
        rationale=rationale,
        evidence_path=str(evidence.relative_to(_repo_root())),
        trial_providers=trial_status,
    )


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
    """Guardrail: odds package must not grow sportsbook scraper modules."""
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
        # Heuristic: executable scrape workflows, not policy prose.
        if "def scrape_" in lowered or "class sportsbookscraper" in lowered.replace(" ", ""):
            offenders.append(path.name)
        if "BET365_PASSWORD" in text or "SPORTSBOOK_COOKIE" in text:
            offenders.append(path.name)
    if offenders:
        raise AssertionError(
            "sportsbook scraper / credential paths forbidden: "
            + ", ".join(sorted(set(offenders)))
        )
