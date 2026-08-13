"""Snapshot orchestration for The Odds API reference quotes (DWCS-201)."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from sqlalchemy.orm import Session

from mma_model.config import get_settings
from mma_model.odds.normalize import ensure_utc, normalize_odds_payload, parse_single_region
from mma_model.odds.schedule import SnapshotCutoffError, assert_snapshot_at_or_before
from mma_model.odds.store import OddsQuoteStore
from mma_model.odds.the_odds_api import OddsApiError, TheOddsApiClient
from mma_model.odds.types import (
    ALLOWED_REQUESTED_SERIES,
    PROVIDER_LABEL_THE_ODDS_API,
    PROVIDER_SCOPE_UNMATCHED,
    PROVIDER_THE_ODDS_API,
    NormalizeReport,
    QuotaHeaders,
)

LIVE_DEFAULT_DB_URLS = frozenset(
    {
        "sqlite:///data/mma.db",
        "sqlite:///./data/mma.db",
    }
)

_TESTISH_DB_PATH = re.compile(
    r"(?:^|[\\/_-])(?:test|tests|tmp|temp|fixture|fixtures|pytest)(?:[\\/_-]|$)",
    re.IGNORECASE,
)


class OddsOfflineModeError(RuntimeError):
    """Raised when offline fixtures are requested without required safeguards."""


class OddsConfigurationError(RuntimeError):
    """Raised for fail-closed odds configuration errors before any DB write."""


@dataclass(frozen=True)
class SnapshotResult:
    provider: str
    requested_series: str
    canonical_series_verified: bool
    provider_scope: str
    mode: str
    markets: str
    region: str
    empty: bool
    quote_count: int
    inserted: int
    deduped: int
    unknown_observation_count: int
    unknown_inserted: int
    unknown_deduped: int
    skipped_unsupported_markets: tuple[str, ...]
    skipped_unsupported_line_points: tuple[str, ...]
    quota: dict[str, Any]
    snapshot_at: str | None
    observed_at: str
    used_fixtures: bool
    claims_bet365: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_requested_series(series: str) -> str:
    value = str(series).strip()
    if value not in ALLOWED_REQUESTED_SERIES:
        raise OddsConfigurationError(
            f"unsupported requested series {series!r}; "
            f"allowed: {sorted(ALLOWED_REQUESTED_SERIES)}"
        )
    return value


def resolve_odds_client(
    *,
    provider: str,
    api_key: str | None = None,
    fixture_dir: Path | None = None,
    offline_fixtures: bool = False,
) -> tuple[TheOddsApiClient, bool]:
    """Build a live or explicitly offline client. Never invent a fixture path."""
    if provider not in {PROVIDER_THE_ODDS_API, PROVIDER_LABEL_THE_ODDS_API}:
        raise OddsConfigurationError(
            f"unsupported odds provider {provider!r}; "
            f"DWCS-201 supports only {PROVIDER_LABEL_THE_ODDS_API}"
        )
    if offline_fixtures:
        if fixture_dir is None:
            raise OddsOfflineModeError(
                "--offline-fixtures requires an explicit --fixture-dir"
            )
        return TheOddsApiClient(api_key="", fixture_dir=Path(fixture_dir)), True

    client = TheOddsApiClient(api_key=api_key, fixture_dir=None)
    if client.has_api_key:
        return client, False
    raise OddsConfigurationError(
        "ODDS_API_KEY is required for live odds snapshot/audit. "
        "For deterministic offline tests use --offline-fixtures with an explicit "
        "--fixture-dir and a disposable --database-url."
    )


def _sqlite_url_filesystem_path(
    database_url: str, *, project_root: Path
) -> Path | None:
    """Resolve a SQLite URL to a filesystem path, or None for ``:memory:``."""
    raw = str(database_url).strip()
    if "?" in raw:
        raw = raw.split("?", 1)[0]
    if "#" in raw:
        raw = raw.split("#", 1)[0]
    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    if not scheme.startswith("sqlite"):
        raise OddsOfflineModeError(
            "offline fixture mode requires an explicit SQLite --database-url "
            f"(refusing non-SQLite scheme {scheme or 'missing'!r})"
        )

    # Preserve SQLAlchemy's 3-slash relative / 4-slash absolute convention.
    if raw.lower().startswith("sqlite:////") or raw.lower().startswith(
        "sqlite+pysqlite:////"
    ):
        prefix = "sqlite+pysqlite:////" if "pysqlite" in scheme else "sqlite:////"
        rest = unquote(raw[len(prefix) :])
        path = Path("/" + rest.lstrip("/"))
        return path.resolve()

    # sqlite:///:memory: or sqlite://user@/path forms
    if raw.lower().endswith("/:memory:") or raw.lower().endswith("/:memory"):
        return None
    if parsed.path in {":memory:", "/:memory:", "/:memory"}:
        return None

    three = "sqlite+pysqlite:///" if "pysqlite" in scheme else "sqlite:///"
    if not raw.lower().startswith(three):
        raise OddsOfflineModeError(
            f"unsupported SQLite URL form for offline fixtures: {database_url!r}"
        )
    rest = unquote(raw[len(three) :])
    if not rest or rest == ":memory:":
        return None
    path = Path(rest)
    if not path.is_absolute():
        path = project_root / path
    return path.resolve()


def _looks_like_test_db_path(path: Path) -> bool:
    return bool(_TESTISH_DB_PATH.search(path.as_posix()))


def require_disposable_database_url(
    database_url: str | None,
    *,
    live_database_url: str | None = None,
    project_root: Path | None = None,
) -> str:
    """Offline fixture writes must target an explicit disposable SQLite DB URL.

    Compares normalized filesystem paths against the configured live DB so
    absolute paths, ``sqlite:////…/data/mma.db``, and query variants cannot
    poison production. Non-SQLite URLs are rejected. Existing files must look
    like test/fixture paths; brand-new paths are allowed.
    """
    if not database_url or not str(database_url).strip():
        raise OddsOfflineModeError(
            "offline fixture mode requires an explicit disposable --database-url"
        )
    url = str(database_url).strip()
    settings = get_settings()
    root = project_root if project_root is not None else settings.project_root
    live_url = (
        live_database_url
        if live_database_url is not None
        else settings.mma_database_url
    )

    candidate_path = _sqlite_url_filesystem_path(url, project_root=root)
    try:
        live_path = _sqlite_url_filesystem_path(str(live_url), project_root=root)
    except OddsOfflineModeError:
        live_path = None

    if candidate_path is None:
        # :memory: is always disposable.
        return url

    if live_path is not None and candidate_path == live_path:
        raise OddsOfflineModeError(
            "refusing live data/mma.db for offline fixture odds writes; "
            "pass an explicit disposable SQLite --database-url"
        )

    # Extra guard for default production layout under alternate spellings when
    # MMA_DATABASE_URL is overridden away from data/mma.db.
    if candidate_path.name == "mma.db" and candidate_path.parent.name == "data":
        raise OddsOfflineModeError(
            "refusing live data/mma.db for offline fixture odds writes; "
            "pass an explicit disposable SQLite --database-url"
        )

    if candidate_path.exists() and not _looks_like_test_db_path(candidate_path):
        raise OddsOfflineModeError(
            "offline fixture database must be a non-existing or explicitly "
            "test/fixture-named SQLite path; refusing existing non-test database"
        )
    return url


def run_odds_snapshot(
    session: Session,
    *,
    series: str = "dwcs",
    provider: str = PROVIDER_LABEL_THE_ODDS_API,
    markets: str = "h2h",
    regions: str = "us",
    historical_date: datetime | str | None = None,
    fixture_dir: Path | None = None,
    offline_fixtures: bool = False,
    observed_at: datetime | None = None,
    enforce_historical_cutoff: bool = False,
) -> SnapshotResult:
    """Fetch (or explicit-fixture-load), normalize, and append-only store quotes."""
    requested_series = validate_requested_series(series)
    region = parse_single_region(regions)
    observed = ensure_utc(observed_at or datetime.now(UTC), field="observed_at")

    client, used_fixtures = resolve_odds_client(
        provider=provider,
        fixture_dir=fixture_dir,
        offline_fixtures=offline_fixtures,
    )
    store = OddsQuoteStore(session)

    requested_cutoff: datetime | None = None
    if historical_date is not None:
        if isinstance(historical_date, datetime):
            requested_cutoff = ensure_utc(historical_date, field="historical_date")
        else:
            text_date = str(historical_date).strip()
            if text_date.endswith("Z"):
                text_date = text_date[:-1] + "+00:00"
            requested_cutoff = ensure_utc(
                datetime.fromisoformat(text_date), field="historical_date"
            )
        response = client.fetch_historical_odds(
            date=historical_date,
            regions=region,
            markets=markets,
            odds_format="decimal",
        )
        endpoint = "historical_odds"
        mode = "historical"
        if enforce_historical_cutoff:
            assert_snapshot_at_or_before(
                snapshot_at=response.snapshot_at,
                requested_cutoff=requested_cutoff,
            )
    else:
        response = client.fetch_current_odds(
            regions=region,
            markets=markets,
            odds_format="decimal",
        )
        endpoint = "current_odds"
        mode = "current"

    store.record_quota(
        provider=PROVIDER_THE_ODDS_API,
        endpoint=endpoint,
        observed_at=observed,
        quota=response.quota,
        empty_response=response.empty,
    )

    report = normalize_odds_payload(
        response.events,
        observed_at=observed,
        region=region,
        odds_format="decimal",
        requested_markets=[m.strip() for m in markets.split(",") if m.strip()],
        snapshot_at=response.snapshot_at,
        provider=PROVIDER_THE_ODDS_API,
    )
    quote_result = store.append_quotes(report.quotes)
    unknown_result = store.append_unknown_observations(report.unknown_observations)
    session.flush()

    return SnapshotResult(
        provider=PROVIDER_THE_ODDS_API,
        requested_series=requested_series,
        canonical_series_verified=False,
        provider_scope=PROVIDER_SCOPE_UNMATCHED,
        mode=mode,
        markets=markets,
        region=region,
        empty=response.empty,
        quote_count=len(report.quotes),
        inserted=quote_result.inserted,
        deduped=quote_result.deduped,
        unknown_observation_count=len(report.unknown_observations),
        unknown_inserted=unknown_result.unknown_inserted,
        unknown_deduped=unknown_result.unknown_deduped,
        skipped_unsupported_markets=report.skipped_unsupported_markets,
        skipped_unsupported_line_points=report.skipped_unsupported_line_points,
        quota=response.quota.as_dict(),
        snapshot_at=None
        if response.snapshot_at is None
        else response.snapshot_at.isoformat(),
        observed_at=observed.isoformat(),
        used_fixtures=used_fixtures,
        claims_bet365=False,
    )


def run_odds_audit(
    session: Session,
    *,
    series: str = "dwcs",
    provider: str = PROVIDER_LABEL_THE_ODDS_API,
    markets: str = "h2h",
    regions: str = "us",
    fixture_dir: Path | None = None,
    offline_fixtures: bool = False,
) -> dict[str, Any]:
    """Sanitized audit summary: events, market discovery sample, quota, no prices."""
    requested_series = validate_requested_series(series)
    region = parse_single_region(regions)
    observed = datetime.now(UTC)
    client, used_fixtures = resolve_odds_client(
        provider=provider,
        fixture_dir=fixture_dir,
        offline_fixtures=offline_fixtures,
    )
    store = OddsQuoteStore(session)

    events_response = client.list_events()
    store.record_quota(
        provider=PROVIDER_THE_ODDS_API,
        endpoint="events",
        observed_at=observed,
        quota=events_response.quota,
        empty_response=events_response.empty,
    )

    discovery: dict[str, Any] | None = None
    if events_response.events:
        sample = events_response.events[0]
        market_response = client.discover_markets(sample.id, regions=region)
        store.record_quota(
            provider=PROVIDER_THE_ODDS_API,
            endpoint="market_discovery",
            observed_at=observed,
            quota=market_response.quota,
            empty_response=market_response.empty,
        )
        discovery = {
            "event_id": sample.id,
            "empty": market_response.empty,
            "market_keys": sorted({m.market_key for m in market_response.markets}),
            "bookmaker_keys": sorted({m.bookmaker_key for m in market_response.markets}),
            "quota": market_response.quota.as_dict(),
        }

    odds_response = client.fetch_current_odds(
        regions=region,
        markets=markets,
        odds_format="decimal",
    )
    store.record_quota(
        provider=PROVIDER_THE_ODDS_API,
        endpoint="current_odds",
        observed_at=observed,
        quota=odds_response.quota,
        empty_response=odds_response.empty,
    )
    report = normalize_odds_payload(
        odds_response.events,
        observed_at=observed,
        region=region,
        odds_format="decimal",
        requested_markets=[m.strip() for m in markets.split(",") if m.strip()],
        provider=PROVIDER_THE_ODDS_API,
    )

    return {
        "provider": PROVIDER_THE_ODDS_API,
        "requested_series": requested_series,
        "canonical_series_verified": False,
        "provider_scope": PROVIDER_SCOPE_UNMATCHED,
        "used_fixtures": used_fixtures,
        "claims_bet365": False,
        "region": region,
        "events": {
            "count": len(events_response.events),
            "empty": events_response.empty,
            "quota": events_response.quota.as_dict(),
            "ids": [event.id for event in events_response.events],
        },
        "market_discovery": discovery,
        "current_odds": {
            "empty": odds_response.empty,
            "event_count": len(odds_response.events),
            "normalized_quote_count": len(report.quotes),
            "unknown_observation_count": len(report.unknown_observations),
            "skipped_unsupported_markets": list(report.skipped_unsupported_markets),
            "skipped_unsupported_line_points": list(
                report.skipped_unsupported_line_points
            ),
            "quota": odds_response.quota.as_dict(),
        },
        "product_note": (
            "Exact bookmaker lines are optional enrichment. Sportsbook-agnostic "
            "actionable price guidance remains the required fallback. Reference "
            "odds are never labeled as Bet365. Provider rows are unmatched until "
            "DWCS-203 canonical bout matching."
        ),
    }


def empty_quota_report(quota: QuotaHeaders, *, empty: bool) -> dict[str, Any]:
    """Report empty-response quota semantics for operators/tests."""
    expected = quota.expected_cost
    return {
        "empty": empty,
        "quota": quota.as_dict(),
        "billed": bool(expected is not None and expected > 0),
        "requests_last_source": quota.requests_last_source,
    }


def _persist_report(store: OddsQuoteStore, report: NormalizeReport) -> None:
    store.append_quotes(report.quotes)
    store.append_unknown_observations(report.unknown_observations)


# Re-export for tests that patch configuration failures.
__all__ = [
    "LIVE_DEFAULT_DB_URLS",
    "OddsApiError",
    "OddsConfigurationError",
    "OddsOfflineModeError",
    "SnapshotCutoffError",
    "SnapshotResult",
    "empty_quota_report",
    "require_disposable_database_url",
    "resolve_odds_client",
    "run_odds_audit",
    "run_odds_snapshot",
    "validate_requested_series",
]
