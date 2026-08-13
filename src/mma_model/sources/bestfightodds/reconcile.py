"""Optional BestFightOdds archive reconciliation (DWCS-205).

Policy-gated public historical *odds* reconciliation only. Never stats/PIT
evidence, never sportsbook-page scraping, never access-control bypass.
Uses the shared polite HTTP client when live fetches are enabled.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from mma_model.odds.normalize import ensure_utc
from mma_model.odds.provider_decision import (
    licensed_bookmaker_adapter_authorized,
    require_licensed_bookmaker_adapter,
)
from mma_model.odds.schedule import OddsScheduleContract, load_default_schedule_contract
from mma_model.sources.http.polite_client import PoliteHttpClient
from mma_model.sources.http_politeness import load_http_politeness
from mma_model.sources.policy import SourceId, load_source_policy


class BestFightOddsPolicyError(RuntimeError):
    """Raised when archive reconciliation is forbidden by committed policy."""


@dataclass(frozen=True)
class BestFightOddsReconcileResult:
    enabled: bool
    role: str
    stats_or_pit_evidence: bool
    sportsbook_page_scrape: bool
    status: str
    url: str | None
    payload_hash: str | None
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def reconcile_bestfightodds_archive(
    *,
    as_of: datetime,
    event_name: str,
    cache_dir: Path,
    schedule: OddsScheduleContract | None = None,
    fixture_html: str | None = None,
    live_fetch: bool = False,
    url: str | None = None,
) -> BestFightOddsReconcileResult:
    """Reconcile a public archive page only when source policy permits.

    ``fixture_html`` enables deterministic offline tests. Live fetch requires
    ``live_fetch=True`` and uses polite HTTP with no cookies/auth.
    """
    stamp = ensure_utc(as_of, field="as_of")
    sched = schedule or load_default_schedule_contract()
    bfo_cfg = dict(sched.bestfightodds_archive)
    if not bfo_cfg.get("enabled", False):
        return BestFightOddsReconcileResult(
            enabled=False,
            role=str(bfo_cfg.get("role") or "disabled"),
            stats_or_pit_evidence=False,
            sportsbook_page_scrape=False,
            status="disabled",
            url=None,
            payload_hash=None,
            detail="schedule_contract_disabled",
        )

    policy = load_source_policy()
    source_id = SourceId.BESTFIGHTODDS_ARCHIVE.value
    if source_id not in policy.source_ids:
        raise BestFightOddsPolicyError(
            f"{source_id} is not listed in committed source_ids"
        )
    role_spec = policy.roles[source_id]
    if role_spec.role != "public_historical_odds_reconciliation":
        raise BestFightOddsPolicyError(
            f"{source_id} role is {role_spec.role!r}; refusing non-odds use"
        )

    if bfo_cfg.get("never_stats_or_pit_evidence") is not True:
        raise BestFightOddsPolicyError(
            "schedule contract must set never_stats_or_pit_evidence=true"
        )
    if bfo_cfg.get("never_sportsbook_page_scrape") is not True:
        raise BestFightOddsPolicyError(
            "schedule contract must set never_sportsbook_page_scrape=true"
        )

    # Even if a licensed adapter is later authorized, this module never scrapes
    # sportsbook pages; licensed history uses require_licensed_bookmaker_adapter.
    _ = licensed_bookmaker_adapter_authorized()

    if fixture_html is not None:
        digest = hashlib.sha256(fixture_html.encode("utf-8")).hexdigest()
        return BestFightOddsReconcileResult(
            enabled=True,
            role="public_historical_odds_reconciliation",
            stats_or_pit_evidence=False,
            sportsbook_page_scrape=False,
            status="fixture_reconciled",
            url=url,
            payload_hash=digest,
            detail=(
                f"offline fixture for event={event_name!r} as_of="
                f"{stamp.isoformat()}; not_stats_pit_evidence"
            ),
        )

    if not live_fetch:
        return BestFightOddsReconcileResult(
            enabled=True,
            role="public_historical_odds_reconciliation",
            stats_or_pit_evidence=False,
            sportsbook_page_scrape=False,
            status="skipped_no_fetch",
            url=url,
            payload_hash=None,
            detail="live_fetch_disabled",
        )

    if not url or not str(url).startswith("https://www.bestfightodds.com/"):
        raise BestFightOddsPolicyError(
            "live fetch requires an https://www.bestfightodds.com/ URL"
        )

    politeness = load_http_politeness()
    client = PoliteHttpClient(
        host="bestfightodds.com",
        politeness=politeness,
        cache_dir=Path(cache_dir),
    )
    try:
        body, digest = client.get_text(url)
    finally:
        client.close()
    return BestFightOddsReconcileResult(
        enabled=True,
        role="public_historical_odds_reconciliation",
        stats_or_pit_evidence=False,
        sportsbook_page_scrape=False,
        status="fetched",
        url=url,
        payload_hash=digest,
        detail="polite_http_archive_fetch; not_stats_pit_evidence",
    )


def refuse_licensed_bookmaker_history_without_contract() -> None:
    """Fail closed for licensed bookmaker history when adapter unauthorized."""
    require_licensed_bookmaker_adapter("licensed_bookmaker_history")


__all__ = [
    "BestFightOddsPolicyError",
    "BestFightOddsReconcileResult",
    "reconcile_bestfightodds_archive",
    "refuse_licensed_bookmaker_history_without_contract",
]
