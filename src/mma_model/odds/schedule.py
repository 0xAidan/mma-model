"""Event-relative odds snapshot scheduling (DWCS-205).

Pure functions take explicit UTC ``as_of`` / event start / last success — no
hidden wall-clock reads. Cadence matches plan § Refresh cadence.
"""

from __future__ import annotations

import hashlib
import importlib.resources as resources
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from mma_model.odds.normalize import ensure_utc

EXPECTED_SCHEDULE_CONTRACT_VERSION = "1.0.0"
SCHEDULE_CONTRACT_ID = "dwcs_odds_schedule"


class ScheduleContractError(ValueError):
    """Raised when the schedule contract is missing or drifted."""


class SnapshotCutoffError(ValueError):
    """Historical snapshot_at is after the requested cutoff (fail closed)."""


class DueAction(StrEnum):
    NO_OP = "no_op"
    DUE = "due"
    NOT_DUE = "not_due"
    DEFERRED_QUOTA = "deferred_quota"


@dataclass(frozen=True)
class CadenceWindow:
    name: str
    offset_before_event_start_sec: int
    offset_before_event_end_sec: int
    interval_sec: int
    requires_quota_headroom: bool


@dataclass(frozen=True)
class SparseCheckpoint:
    name: str
    offset_before_event_sec: int


@dataclass(frozen=True)
class QuotaContract:
    monthly_limit: int
    run_reserve: int
    cost_per_region_market: Mapping[str, int]
    cost_fixed: Mapping[str, int]


@dataclass(frozen=True)
class OddsScheduleContract:
    contract_id: str
    contract_version: str
    schema_version: int
    ticket: str
    provider: str
    default_markets: str
    default_region: str
    series: str
    historical_available_from: datetime
    backfill_from_year: int
    cadence_windows: tuple[CadenceWindow, ...]
    sparse_backfill_checkpoints: tuple[SparseCheckpoint, ...]
    quota: QuotaContract
    coverage_statuses: tuple[str, ...]
    bestfightodds_archive: Mapping[str, Any]
    licensed_bookmaker_history: Mapping[str, Any]
    idempotency_key_version: int
    raw_bytes: bytes

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.raw_bytes).hexdigest()


@dataclass(frozen=True)
class DueWorkItem:
    action: DueAction
    event_id: str
    event_start: datetime
    as_of: datetime
    provider: str
    markets: str
    region: str
    window_name: str | None
    interval_sec: int | None
    slot_start: datetime | None
    idempotency_key: str | None
    reason: str
    estimated_cost: int
    requires_quota_headroom: bool


def package_schedule_resource_path() -> Path:
    root = resources.files("mma_model.odds")
    return Path(str(root.joinpath("schedule_v1.yaml")))


def _parse_utc_iso(value: str, *, field: str) -> datetime:
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ScheduleContractError(f"{field} is not ISO-8601: {value!r}") from exc
    return ensure_utc(parsed, field=field)


def _freeze_int_map(values: Mapping[str, Any]) -> Mapping[str, int]:
    return MappingProxyType({str(k): int(v) for k, v in values.items()})


def load_schedule_contract(
    path: Path | None = None,
    *,
    raw_bytes: bytes | None = None,
) -> OddsScheduleContract:
    """Load and validate the frozen odds schedule contract."""
    if raw_bytes is None:
        source = path if path is not None else package_schedule_resource_path()
        raw_bytes = Path(source).read_bytes()
    try:
        payload = yaml.safe_load(raw_bytes.decode("utf-8"))
    except yaml.YAMLError as exc:
        raise ScheduleContractError(f"invalid schedule YAML: {exc}") from exc
    if not isinstance(payload, dict):
        raise ScheduleContractError("schedule contract root must be a mapping")

    contract_id = str(payload.get("contract_id") or "")
    version = str(payload.get("contract_version") or "")
    if contract_id != SCHEDULE_CONTRACT_ID:
        raise ScheduleContractError(
            f"unexpected contract_id {contract_id!r}; expected {SCHEDULE_CONTRACT_ID!r}"
        )
    if version != EXPECTED_SCHEDULE_CONTRACT_VERSION:
        raise ScheduleContractError(
            f"unexpected contract_version {version!r}; "
            f"expected {EXPECTED_SCHEDULE_CONTRACT_VERSION!r}"
        )

    windows_raw = payload.get("cadence_windows")
    if not isinstance(windows_raw, list) or not windows_raw:
        raise ScheduleContractError("cadence_windows must be a non-empty list")
    windows: list[CadenceWindow] = []
    for item in windows_raw:
        if not isinstance(item, dict):
            raise ScheduleContractError("cadence window must be a mapping")
        start = int(item["offset_before_event_start_sec"])
        end = int(item["offset_before_event_end_sec"])
        interval = int(item["interval_sec"])
        if start <= end:
            raise ScheduleContractError(
                f"window {item.get('name')!r} start offset must be > end offset"
            )
        if interval <= 0:
            raise ScheduleContractError(
                f"window {item.get('name')!r} interval_sec must be positive"
            )
        windows.append(
            CadenceWindow(
                name=str(item["name"]),
                offset_before_event_start_sec=start,
                offset_before_event_end_sec=end,
                interval_sec=interval,
                requires_quota_headroom=bool(item.get("requires_quota_headroom", False)),
            )
        )

    checkpoints_raw = payload.get("sparse_backfill_checkpoints")
    if not isinstance(checkpoints_raw, list) or not checkpoints_raw:
        raise ScheduleContractError("sparse_backfill_checkpoints must be non-empty")
    checkpoints: list[SparseCheckpoint] = []
    for item in checkpoints_raw:
        if not isinstance(item, dict):
            raise ScheduleContractError("sparse checkpoint must be a mapping")
        checkpoints.append(
            SparseCheckpoint(
                name=str(item["name"]),
                offset_before_event_sec=int(item["offset_before_event_sec"]),
            )
        )

    quota_raw = payload.get("quota")
    if not isinstance(quota_raw, dict):
        raise ScheduleContractError("quota must be a mapping")
    cost_prm = quota_raw.get("cost_per_region_market")
    cost_fixed = quota_raw.get("cost_fixed") or {}
    if not isinstance(cost_prm, dict) or not isinstance(cost_fixed, dict):
        raise ScheduleContractError("quota cost maps must be mappings")
    if "current_odds" not in cost_prm or "historical_odds" not in cost_prm:
        raise ScheduleContractError(
            "quota.cost_per_region_market requires current_odds and historical_odds"
        )

    idem = payload.get("idempotency") or {}
    if not isinstance(idem, dict):
        raise ScheduleContractError("idempotency must be a mapping")

    statuses = payload.get("coverage_statuses") or []
    if not isinstance(statuses, list) or not statuses:
        raise ScheduleContractError("coverage_statuses must be a non-empty list")

    bfo = payload.get("bestfightodds_archive") or {}
    licensed = payload.get("licensed_bookmaker_history") or {}
    if not isinstance(bfo, dict) or not isinstance(licensed, dict):
        raise ScheduleContractError("archive/licensed policy blocks must be mappings")

    return OddsScheduleContract(
        contract_id=contract_id,
        contract_version=version,
        schema_version=int(payload.get("schema_version") or 0),
        ticket=str(payload.get("ticket") or ""),
        provider=str(payload.get("provider") or "the_odds_api"),
        default_markets=str(payload.get("default_markets") or "h2h"),
        default_region=str(payload.get("default_region") or "us"),
        series=str(payload.get("series") or "dwcs"),
        historical_available_from=_parse_utc_iso(
            str(payload["historical_available_from"]),
            field="historical_available_from",
        ),
        backfill_from_year=int(payload.get("backfill_from_year") or 2020),
        cadence_windows=tuple(windows),
        sparse_backfill_checkpoints=tuple(checkpoints),
        quota=QuotaContract(
            monthly_limit=int(quota_raw["monthly_limit"]),
            run_reserve=int(quota_raw["run_reserve"]),
            cost_per_region_market=_freeze_int_map(cost_prm),
            cost_fixed=_freeze_int_map(cost_fixed),
        ),
        coverage_statuses=tuple(str(s) for s in statuses),
        bestfightodds_archive=MappingProxyType(dict(bfo)),
        licensed_bookmaker_history=MappingProxyType(dict(licensed)),
        idempotency_key_version=int(idem.get("key_version") or 1),
        raw_bytes=raw_bytes,
    )


@lru_cache(maxsize=1)
def load_default_schedule_contract() -> OddsScheduleContract:
    return load_schedule_contract()


def resolve_cadence_window(
    *,
    as_of: datetime,
    event_start: datetime,
    contract: OddsScheduleContract | None = None,
) -> CadenceWindow | None:
    """Return the half-open cadence window containing ``as_of``, else None."""
    sched = contract or load_default_schedule_contract()
    stamp = ensure_utc(as_of, field="as_of")
    start = ensure_utc(event_start, field="event_start")
    for window in sched.cadence_windows:
        window_start = start - timedelta(seconds=window.offset_before_event_start_sec)
        window_end = start - timedelta(seconds=window.offset_before_event_end_sec)
        if window_start <= stamp < window_end:
            return window
    return None


def slot_floor(as_of: datetime, *, interval_sec: int) -> datetime:
    """Floor ``as_of`` onto an interval grid anchored at Unix epoch (UTC)."""
    stamp = ensure_utc(as_of, field="as_of")
    if interval_sec <= 0:
        raise ValueError("interval_sec must be positive")
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    elapsed = int((stamp - epoch).total_seconds())
    floored = elapsed - (elapsed % interval_sec)
    return epoch + timedelta(seconds=floored)


def compute_idempotency_key(
    *,
    provider: str,
    region: str,
    markets: str,
    event_id: str,
    mode: str,
    slot_or_cutoff: datetime,
    key_version: int = 1,
) -> str:
    """Durable logical snapshot identity so retries do not duplicate work."""
    stamp = ensure_utc(slot_or_cutoff, field="slot_or_cutoff")
    markets_norm = ",".join(
        sorted(m.strip() for m in str(markets).split(",") if m.strip())
    )
    material = "|".join(
        [
            f"v{int(key_version)}",
            str(provider).strip(),
            str(region).strip().lower(),
            markets_norm,
            str(event_id).strip(),
            str(mode).strip(),
            stamp.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        ]
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"odds_snap:{digest}"


def estimate_endpoint_cost(
    *,
    endpoint: str,
    markets: str,
    regions: str,
    contract: OddsScheduleContract | None = None,
) -> int:
    """Estimate quota credits from the provider contract (never invent costs)."""
    sched = contract or load_default_schedule_contract()
    market_count = len([m for m in str(markets).split(",") if m.strip()])
    region_count = len([r for r in str(regions).split(",") if r.strip()])
    if market_count < 1:
        raise ValueError("markets must list at least one market")
    if region_count < 1:
        raise ValueError("regions must list at least one region")

    fixed = sched.quota.cost_fixed.get(endpoint)
    if fixed is not None:
        return int(fixed)

    per = sched.quota.cost_per_region_market.get(endpoint)
    if per is None:
        raise ValueError(f"unknown endpoint cost mapping: {endpoint!r}")
    return int(per) * market_count * region_count


def assert_snapshot_at_or_before(
    *,
    snapshot_at: datetime | None,
    requested_cutoff: datetime,
) -> datetime:
    """Fail closed unless provider snapshot_at is <= requested cutoff (UTC)."""
    cutoff = ensure_utc(requested_cutoff, field="requested_cutoff")
    if snapshot_at is None:
        raise SnapshotCutoffError(
            "historical response missing snapshot_at; refusing leak-prone persist"
        )
    snap = ensure_utc(snapshot_at, field="snapshot_at")
    if snap > cutoff:
        raise SnapshotCutoffError(
            f"snapshot_at {snap.isoformat()} is after requested cutoff "
            f"{cutoff.isoformat()}; refusing future leakage"
        )
    return snap


def sparse_checkpoint_cutoff(
    *,
    event_start: datetime,
    checkpoint: SparseCheckpoint,
) -> datetime:
    start = ensure_utc(event_start, field="event_start")
    return start - timedelta(seconds=int(checkpoint.offset_before_event_sec))


def compute_due_work(
    *,
    as_of: datetime,
    event_id: str,
    event_start: datetime,
    last_success_at: datetime | None,
    provider: str,
    markets: str,
    region: str,
    contract: OddsScheduleContract | None = None,
    quota_allows: bool = True,
) -> DueWorkItem:
    """Deterministic due-work decision from explicit UTC inputs only."""
    sched = contract or load_default_schedule_contract()
    stamp = ensure_utc(as_of, field="as_of")
    start = ensure_utc(event_start, field="event_start")
    if last_success_at is not None:
        last_success_at = ensure_utc(last_success_at, field="last_success_at")

    window = resolve_cadence_window(as_of=stamp, event_start=start, contract=sched)
    if window is None:
        return DueWorkItem(
            action=DueAction.NO_OP,
            event_id=event_id,
            event_start=start,
            as_of=stamp,
            provider=provider,
            markets=markets,
            region=region,
            window_name=None,
            interval_sec=None,
            slot_start=None,
            idempotency_key=None,
            reason="outside_event_odds_window",
            estimated_cost=0,
            requires_quota_headroom=False,
        )

    cost = estimate_endpoint_cost(
        endpoint="current_odds",
        markets=markets,
        regions=region,
        contract=sched,
    )
    slot = slot_floor(stamp, interval_sec=window.interval_sec)
    key = compute_idempotency_key(
        provider=provider,
        region=region,
        markets=markets,
        event_id=event_id,
        mode=f"live:{window.name}",
        slot_or_cutoff=slot,
        key_version=sched.idempotency_key_version,
    )

    if window.requires_quota_headroom and not quota_allows:
        return DueWorkItem(
            action=DueAction.DEFERRED_QUOTA,
            event_id=event_id,
            event_start=start,
            as_of=stamp,
            provider=provider,
            markets=markets,
            region=region,
            window_name=window.name,
            interval_sec=window.interval_sec,
            slot_start=slot,
            idempotency_key=key,
            reason="final_hour_requires_quota_headroom",
            estimated_cost=cost,
            requires_quota_headroom=True,
        )

    if last_success_at is not None and last_success_at >= slot:
        return DueWorkItem(
            action=DueAction.NOT_DUE,
            event_id=event_id,
            event_start=start,
            as_of=stamp,
            provider=provider,
            markets=markets,
            region=region,
            window_name=window.name,
            interval_sec=window.interval_sec,
            slot_start=slot,
            idempotency_key=key,
            reason="slot_already_satisfied",
            estimated_cost=cost,
            requires_quota_headroom=window.requires_quota_headroom,
        )

    return DueWorkItem(
        action=DueAction.DUE,
        event_id=event_id,
        event_start=start,
        as_of=stamp,
        provider=provider,
        markets=markets,
        region=region,
        window_name=window.name,
        interval_sec=window.interval_sec,
        slot_start=slot,
        idempotency_key=key,
        reason="interval_elapsed_or_first_success",
        estimated_cost=cost,
        requires_quota_headroom=window.requires_quota_headroom,
    )


def compute_due_work_for_events(
    *,
    as_of: datetime,
    events: Sequence[Mapping[str, Any]],
    last_success_by_event: Mapping[str, datetime] | None = None,
    provider: str | None = None,
    markets: str | None = None,
    region: str | None = None,
    contract: OddsScheduleContract | None = None,
    quota_allows: bool = True,
) -> tuple[DueWorkItem, ...]:
    """Compute due work for a batch of events (explicit UTC ``as_of`` only)."""
    sched = contract or load_default_schedule_contract()
    successes = last_success_by_event or {}
    items: list[DueWorkItem] = []
    for event in events:
        event_id = str(event["event_id"])
        event_start = event["event_start"]
        if isinstance(event_start, str):
            event_start = _parse_utc_iso(event_start, field="event_start")
        else:
            event_start = ensure_utc(event_start, field="event_start")
        items.append(
            compute_due_work(
                as_of=as_of,
                event_id=event_id,
                event_start=event_start,
                last_success_at=successes.get(event_id),
                provider=provider or sched.provider,
                markets=markets or sched.default_markets,
                region=region or sched.default_region,
                contract=sched,
                quota_allows=quota_allows,
            )
        )
    return tuple(items)


__all__ = [
    "EXPECTED_SCHEDULE_CONTRACT_VERSION",
    "SCHEDULE_CONTRACT_ID",
    "CadenceWindow",
    "DueAction",
    "DueWorkItem",
    "OddsScheduleContract",
    "QuotaContract",
    "ScheduleContractError",
    "SnapshotCutoffError",
    "SparseCheckpoint",
    "assert_snapshot_at_or_before",
    "compute_due_work",
    "compute_due_work_for_events",
    "compute_idempotency_key",
    "estimate_endpoint_cost",
    "load_default_schedule_contract",
    "load_schedule_contract",
    "package_schedule_resource_path",
    "resolve_cadence_window",
    "slot_floor",
    "sparse_checkpoint_cutoff",
]
