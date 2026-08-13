"""Optional BestFightOdds archive reconciliation (DWCS-205).

Policy-gated public historical *odds archive* reconciliation only. This is not
direct sportsbook-site scraping, never stats/PIT evidence, and never an
access-control bypass. Uses the shared polite HTTP client when live fetches
are enabled.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from mma_model.odds.normalize import ensure_utc
from mma_model.odds.provider_decision import (
    licensed_bookmaker_adapter_authorized,
    require_licensed_bookmaker_adapter,
)
from mma_model.odds.schedule import OddsScheduleContract, load_default_schedule_contract
from mma_model.sources.http.polite_client import PoliteHttpClient
from mma_model.sources.http_politeness import load_http_politeness
from mma_model.sources.policy import SourceId, load_source_policy

_ALLOWED_HOSTS = frozenset({"bestfightodds.com", "www.bestfightodds.com"})
# Segment-boundary prefixes only (/events must not match /eventsX).
_ALLOWED_PATH_PREFIXES = ("/archive", "/events", "/odds", "/mma")
_UNSAFE_QUERY_KEYS = frozenset({"redirect", "url", "next", "return", "callback"})


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


def _path_matches_allowed_prefix(path: str) -> bool:
    for prefix in _ALLOWED_PATH_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return False


def validate_bestfightodds_archive_url(url: str) -> str:
    """Fail closed unless HTTPS, exact allowed host, no credentials, allowed path."""
    raw = str(url).strip()
    if "#" in raw:
        raise BestFightOddsPolicyError("archive URL must not include fragments")
    parsed = urlparse(raw)
    if parsed.scheme != "https":
        raise BestFightOddsPolicyError("archive URL must use https")
    host = (parsed.hostname or "").lower()
    if host not in _ALLOWED_HOSTS:
        raise BestFightOddsPolicyError(f"archive host not allowed: {host!r}")
    if parsed.username is not None or parsed.password is not None:
        raise BestFightOddsPolicyError("archive URL must not include credentials")
    if parsed.port not in {None, 443}:
        raise BestFightOddsPolicyError("archive URL must use default https port")
    if parsed.fragment:
        raise BestFightOddsPolicyError("archive URL must not include fragments")

    path = parsed.path or "/"
    decoded = unquote(path)
    if decoded != path:
        path = decoded
    lowered = path.lower()
    if (
        ".." in path
        or "/." in path
        or path.startswith("./")
        or "//" in path
        or "%" in path
        or "\\" in path
    ):
        raise BestFightOddsPolicyError(
            f"archive path rejects traversal/ambiguous normalization: {path!r}"
        )
    if not _path_matches_allowed_prefix(lowered):
        raise BestFightOddsPolicyError(f"archive path not allowed: {path!r}")

    if parsed.query:
        for part in parsed.query.split("&"):
            if not part:
                continue
            key = unquote(part.split("=", 1)[0]).lower()
            if key in _UNSAFE_QUERY_KEYS:
                raise BestFightOddsPolicyError(
                    f"archive URL query parameter not allowed: {key!r}"
                )

    query = f"?{parsed.query}" if parsed.query else ""
    return f"https://{host}{path}{query}"


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
    """Reconcile a public odds-archive page only when source policy permits."""
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

    _ = licensed_bookmaker_adapter_authorized()

    if fixture_html is not None:
        if url is not None:
            url = validate_bestfightodds_archive_url(url)
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
                f"offline odds-archive fixture for event={event_name!r} as_of="
                f"{stamp.isoformat()}; not_stats_pit_evidence; "
                "not_direct_sportsbook_scrape"
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

    if not url:
        raise BestFightOddsPolicyError("live fetch requires an archive URL")
    safe_url = validate_bestfightodds_archive_url(url)

    politeness = load_http_politeness()
    client = PoliteHttpClient(
        host="bestfightodds.com",
        politeness=politeness,
        cache_dir=Path(cache_dir),
    )
    try:
        body, digest = client.get_text(safe_url)
    finally:
        client.close()
    _ = body
    return BestFightOddsReconcileResult(
        enabled=True,
        role="public_historical_odds_reconciliation",
        stats_or_pit_evidence=False,
        sportsbook_page_scrape=False,
        status="fetched",
        url=safe_url,
        payload_hash=digest,
        detail=(
            "polite_http_odds_archive_fetch; not_stats_pit_evidence; "
            "not_direct_sportsbook_scrape"
        ),
    )


def refuse_licensed_bookmaker_history_without_contract() -> None:
    """Fail closed for licensed bookmaker history when adapter unauthorized."""
    require_licensed_bookmaker_adapter("licensed_bookmaker_history")


__all__ = [
    "BestFightOddsPolicyError",
    "BestFightOddsReconcileResult",
    "reconcile_bestfightodds_archive",
    "refuse_licensed_bookmaker_history_without_contract",
    "validate_bestfightodds_archive_url",
]
