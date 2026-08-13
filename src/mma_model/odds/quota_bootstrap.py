"""Zero-cost quota bootstrap for DWCS-205 odds jobs.

Calls a documented fixed-cost-0 provider endpoint (default: ``events``), persists
raw remaining headers, and never issues a paid request. Idempotent within the
contract bootstrap min-interval.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from mma_model.db.tables.odds import OddsQuotaObservation
from mma_model.odds.normalize import ensure_utc
from mma_model.odds.schedule import OddsScheduleContract, load_default_schedule_contract
from mma_model.odds.snapshot import resolve_odds_client
from mma_model.odds.store import OddsQuoteStore
from mma_model.odds.types import PROVIDER_THE_ODDS_API


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _recent_bootstrap_exists(
    session: Session,
    *,
    provider: str,
    as_of: datetime,
    endpoint: str,
    min_interval_sec: int,
) -> bool:
    stamp = ensure_utc(as_of, field="as_of")
    floor = stamp - timedelta(seconds=int(min_interval_sec))
    row = session.scalar(
        select(OddsQuotaObservation)
        .where(
            OddsQuotaObservation.provider == provider,
            OddsQuotaObservation.endpoint == endpoint,
            OddsQuotaObservation.observed_at >= floor,
            OddsQuotaObservation.observed_at <= stamp,
        )
        .order_by(OddsQuotaObservation.observed_at.desc())
        .limit(1)
    )
    return row is not None


def bootstrap_quota_remaining(
    session: Session,
    *,
    provider: str = PROVIDER_THE_ODDS_API,
    as_of: datetime,
    contract: OddsScheduleContract | None = None,
    offline_fixtures: bool = False,
    fixture_dir: Path | None = None,
    force: bool = False,
) -> tuple[int | None, str]:
    """Bootstrap remaining via zero-cost endpoint; fail closed when headers missing.

    Returns ``(remaining, source)``. On rate-limit skip without a usable header,
    returns ``(None, "bootstrap_rate_limited")``. Never calls a paid endpoint.
    """
    sched = contract or load_default_schedule_contract()
    if not sched.quota.bootstrap_enabled:
        return None, "bootstrap_disabled"
    endpoint = sched.quota.bootstrap_endpoint
    stamp = ensure_utc(as_of, field="as_of")

    if (
        not force
        and _recent_bootstrap_exists(
            session,
            provider=provider,
            as_of=stamp,
            endpoint=endpoint,
            min_interval_sec=sched.quota.bootstrap_min_interval_sec,
        )
    ):
        # Re-read newest row (may still be stale/missing remaining).
        row = session.scalar(
            select(OddsQuotaObservation)
            .where(
                OddsQuotaObservation.provider == provider,
                OddsQuotaObservation.endpoint == endpoint,
                OddsQuotaObservation.observed_at <= stamp,
            )
            .order_by(
                OddsQuotaObservation.observed_at.desc(),
                OddsQuotaObservation.id.desc(),
            )
            .limit(1)
        )
        if row is None or row.requests_remaining is None:
            return None, "bootstrap_rate_limited"
        return int(row.requests_remaining), "bootstrap_cached"

    if endpoint != "events":
        return None, "bootstrap_unsupported_endpoint"

    try:
        client, _ = resolve_odds_client(
            provider=provider,
            fixture_dir=fixture_dir,
            offline_fixtures=offline_fixtures,
        )
    except Exception:  # noqa: BLE001 — bootstrap must fail closed
        return None, "bootstrap_client_unavailable"

    try:
        response = client.list_events()
    except Exception:  # noqa: BLE001
        return None, "bootstrap_request_failed"

    store = OddsQuoteStore(session)
    store.record_quota(
        provider=PROVIDER_THE_ODDS_API,
        endpoint=endpoint,
        observed_at=stamp,
        quota=response.quota,
        empty_response=response.empty,
    )
    session.flush()

    remaining = response.quota.requests_remaining
    if remaining is None:
        return None, "bootstrap_missing_remaining_header"
    if int(remaining) < 0:
        return None, "bootstrap_malformed_remaining"
    return int(remaining), "bootstrap_events"


__all__ = ["bootstrap_quota_remaining"]
