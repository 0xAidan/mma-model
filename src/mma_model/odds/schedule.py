"""Event-relative odds snapshot scheduling (DWCS-205).

Pure functions take explicit UTC ``as_of`` / event start / success flags — no
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
from typing import Any, Final

import yaml

from mma_model.odds.normalize import ensure_utc

EXPECTED_SCHEDULE_CONTRACT_VERSION = "1.0.0"
SCHEDULE_CONTRACT_ID = "dwcs_odds_schedule"
PINNED_SCHEDULE_CONTRACT_HASH: Final[str] = (
    "d966bdb1f1cbc14806001e2f11d6f273e7f93ceda25f49969e38a42eb3798b75"
)

# Exact plan cadence offsets/intervals (seconds).
_EXPECTED_WINDOWS: tuple[tuple[str, int, int, int, bool], ...] = (
    ("far", 259200, 86400, 1800, False),
    ("mid", 86400, 21600, 600, False),
    ("near", 21600, 3600, 300, False),
    ("final", 3600, 0, 120, True),
)
_EXPECTED_CHECKPOINTS: tuple[tuple[str, int], ...] = (
    ("t_minus_24h", 86400),
    ("t_minus_6h", 21600),
    ("t_minus_1h", 3600),
    ("close_proxy", 0),
)
_EXPECTED_COVERAGE_STATUSES: tuple[str, ...] = (
    "absent",
    "failed",
    "deferred_quota",
    "unmatched",
    "observed",
)


class ScheduleContractError(ValueError):
    """Raised when the schedule contract is missing or drifted."""


class SnapshotCutoffError(ValueError):
    """Historical snapshot_at / cutoff ordering fails closed."""


class DueAction(StrEnum):
    NO_OP = "no_op"
    DUE = "due"
    NOT_DUE = "not_due"
    DEFERRED_QUOTA = "deferred_quota"
    EXHAUSTED_QUOTA = "exhausted_quota"


class RequestPurpose(StrEnum):
    BACKFILL = "backfill"
    LIVE_ORDINARY = "live_ordinary"
    LIVE_FINAL = "live_final"


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
    preserve_reserve_for: tuple[str, ...]
    may_spend_reserve_for: tuple[str, ...]
    missing_remaining_policy: str


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
    window_start: datetime | None
    idempotency_key: str | None
    batch_key: str | None
    reason: str
    estimated_cost: int
    requires_quota_headroom: bool
    purpose: RequestPurpose | None


def package_schedule_resource_path() -> Path:
    root = resources.files("mma_model.odds")
    return Path(str(root.joinpath("schedule_v1.yaml")))


def compute_schedule_contract_hash(raw_bytes: bytes) -> str:
    return hashlib.sha256(raw_bytes).hexdigest()


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


def normalize_csv_tokens(
    value: str,
    *,
    field: str,
    allow_empty: bool = False,
) -> str:
    """Deduplicate, strip, lowercase-sort CSV tokens; reject empties/duplicates."""
    raw_parts = [part.strip() for part in str(value).split(",")]
    parts = [part for part in raw_parts if part]
    if not allow_empty and not parts:
        raise ValueError(f"{field} must list at least one non-empty token")
    if len(parts) != len(set(parts)):
        raise ValueError(f"{field} contains duplicate tokens: {value!r}")
    # Preserve case for market keys (h2h) but normalize region lower; sort stable.
    # Markets and regions are case-sensitive provider keys; sort casefold for order.
    ordered = sorted(parts, key=lambda item: item.casefold())
    return ",".join(ordered)


def normalize_markets(markets: str) -> str:
    return normalize_csv_tokens(markets, field="markets")


def normalize_regions(regions: str) -> str:
    return normalize_csv_tokens(regions, field="regions")


def _validate_cadence_semantics(windows: Sequence[CadenceWindow]) -> None:
    if len(windows) != len(_EXPECTED_WINDOWS):
        raise ScheduleContractError(
            f"expected {len(_EXPECTED_WINDOWS)} cadence windows; got {len(windows)}"
        )
    for actual, expected in zip(windows, _EXPECTED_WINDOWS, strict=True):
        name, start, end, interval, requires = expected
        if (
            actual.name != name
            or actual.offset_before_event_start_sec != start
            or actual.offset_before_event_end_sec != end
            or actual.interval_sec != interval
            or actual.requires_quota_headroom is not requires
        ):
            raise ScheduleContractError(
                f"cadence window drift at {actual.name!r}: got "
                f"({actual.offset_before_event_start_sec}, "
                f"{actual.offset_before_event_end_sec}, {actual.interval_sec}, "
                f"{actual.requires_quota_headroom}); expected {expected}"
            )
    # Contiguous half-open: each window end equals next window start offset.
    for left, right in zip(windows, windows[1:], strict=False):
        if left.offset_before_event_end_sec != right.offset_before_event_start_sec:
            raise ScheduleContractError(
                f"non-contiguous cadence between {left.name!r} and {right.name!r}"
            )
    names = [w.name for w in windows]
    if len(names) != len(set(names)):
        raise ScheduleContractError("cadence window names must be unique")


def load_schedule_contract(
    path: Path | None = None,
    *,
    raw_bytes: bytes | None = None,
    enforce_pinned_digest: bool = True,
) -> OddsScheduleContract:
    """Load and deeply validate the frozen odds schedule contract."""
    if raw_bytes is None:
        source = path if path is not None else package_schedule_resource_path()
        raw_bytes = Path(source).read_bytes()
    content_hash = compute_schedule_contract_hash(raw_bytes)
    if enforce_pinned_digest and content_hash != PINNED_SCHEDULE_CONTRACT_HASH:
        raise ScheduleContractError(
            f"content hash mismatch versus pinned digest: got {content_hash}, "
            f"expected {PINNED_SCHEDULE_CONTRACT_HASH}"
        )
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
    if int(payload.get("schema_version") or 0) != 1:
        raise ScheduleContractError("schema_version must be 1")
    if str(payload.get("ticket") or "") != "DWCS-205":
        raise ScheduleContractError("ticket must be DWCS-205")

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
    _validate_cadence_semantics(windows)

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
    if tuple((c.name, c.offset_before_event_sec) for c in checkpoints) != _EXPECTED_CHECKPOINTS:
        raise ScheduleContractError(
            f"sparse checkpoints drift: got "
            f"{[(c.name, c.offset_before_event_sec) for c in checkpoints]}; "
            f"expected {list(_EXPECTED_CHECKPOINTS)}"
        )
    if len({c.name for c in checkpoints}) != len(checkpoints):
        raise ScheduleContractError("sparse checkpoint names must be unique")

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
    monthly = int(quota_raw["monthly_limit"])
    reserve = int(quota_raw["run_reserve"])
    if monthly <= 0 or reserve < 0 or reserve >= monthly:
        raise ScheduleContractError(
            "quota requires monthly_limit > run_reserve >= 0"
        )
    preserve = tuple(str(x) for x in (quota_raw.get("preserve_reserve_for") or ()))
    spend = tuple(str(x) for x in (quota_raw.get("may_spend_reserve_for") or ()))
    missing_policy = str(quota_raw.get("missing_remaining_policy") or "")
    if missing_policy != "defer_fail_closed":
        raise ScheduleContractError(
            "quota.missing_remaining_policy must be defer_fail_closed"
        )
    if "backfill" not in preserve or "live_ordinary" not in preserve:
        raise ScheduleContractError("preserve_reserve_for must include backfill/live_ordinary")
    if "live_final" not in spend:
        raise ScheduleContractError("may_spend_reserve_for must include live_final")

    idem = payload.get("idempotency") or {}
    if not isinstance(idem, dict):
        raise ScheduleContractError("idempotency must be a mapping")

    statuses = tuple(str(s) for s in (payload.get("coverage_statuses") or []))
    if statuses != _EXPECTED_COVERAGE_STATUSES:
        raise ScheduleContractError(
            f"coverage_statuses drift: got {list(statuses)}; "
            f"expected {list(_EXPECTED_COVERAGE_STATUSES)}"
        )

    bfo = payload.get("bestfightodds_archive") or {}
    licensed = payload.get("licensed_bookmaker_history") or {}
    if not isinstance(bfo, dict) or not isinstance(licensed, dict):
        raise ScheduleContractError("archive/licensed policy blocks must be mappings")
    if bfo.get("never_stats_or_pit_evidence") is not True:
        raise ScheduleContractError(
            "bestfightodds_archive.never_stats_or_pit_evidence must be true"
        )
    if bfo.get("never_sportsbook_page_scrape") is not True:
        raise ScheduleContractError(
            "bestfightodds_archive.never_sportsbook_page_scrape must be true"
        )
    if bfo.get("require_polite_http") is not True:
        raise ScheduleContractError("bestfightodds_archive.require_polite_http must be true")
    if licensed.get("require_licensed_adapter_authorized") is not True:
        raise ScheduleContractError(
            "licensed_bookmaker_history.require_licensed_adapter_authorized must be true"
        )

    return OddsScheduleContract(
        contract_id=contract_id,
        contract_version=version,
        schema_version=int(payload.get("schema_version") or 0),
        ticket=str(payload.get("ticket") or ""),
        provider=str(payload.get("provider") or "the_odds_api"),
        default_markets=normalize_markets(str(payload.get("default_markets") or "h2h")),
        default_region=normalize_regions(str(payload.get("default_region") or "us")),
        series=str(payload.get("series") or "dwcs"),
        historical_available_from=_parse_utc_iso(
            str(payload["historical_available_from"]),
            field="historical_available_from",
        ),
        backfill_from_year=int(payload.get("backfill_from_year") or 2020),
        cadence_windows=tuple(windows),
        sparse_backfill_checkpoints=tuple(checkpoints),
        quota=QuotaContract(
            monthly_limit=monthly,
            run_reserve=reserve,
            cost_per_region_market=_freeze_int_map(cost_prm),
            cost_fixed=_freeze_int_map(cost_fixed),
            preserve_reserve_for=preserve,
            may_spend_reserve_for=spend,
            missing_remaining_policy=missing_policy,
        ),
        coverage_statuses=statuses,
        bestfightodds_archive=MappingProxyType(dict(bfo)),
        licensed_bookmaker_history=MappingProxyType(dict(licensed)),
        idempotency_key_version=int(idem.get("key_version") or 1),
        raw_bytes=raw_bytes,
    )


@lru_cache(maxsize=1)
def load_default_schedule_contract() -> OddsScheduleContract:
    return load_schedule_contract()


def assert_plan_visible_schedule_bytes_match(
    *,
    plan_path: Path | None = None,
    package_path: Path | None = None,
) -> None:
    """Fail closed unless plan-visible config bytes equal packaged bytes."""
    packaged = (package_path or package_schedule_resource_path()).read_bytes()
    visible = (
        plan_path
        if plan_path is not None
        else Path(__file__).resolve().parents[3] / "config" / "odds" / "schedule_v1.yaml"
    ).read_bytes()
    if packaged != visible:
        raise ScheduleContractError(
            "config/odds/schedule_v1.yaml bytes differ from packaged schedule_v1.yaml"
        )


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


def window_bounds(
    *,
    event_start: datetime,
    window: CadenceWindow,
) -> tuple[datetime, datetime]:
    start = ensure_utc(event_start, field="event_start")
    window_start = start - timedelta(seconds=window.offset_before_event_start_sec)
    window_end = start - timedelta(seconds=window.offset_before_event_end_sec)
    return window_start, window_end


def slot_floor_in_window(
    as_of: datetime,
    *,
    window_start: datetime,
    interval_sec: int,
) -> datetime:
    """Floor ``as_of`` onto the interval grid anchored at ``window_start``."""
    stamp = ensure_utc(as_of, field="as_of")
    anchor = ensure_utc(window_start, field="window_start")
    if interval_sec <= 0:
        raise ValueError("interval_sec must be positive")
    if stamp < anchor:
        raise ValueError("as_of is before window_start")
    elapsed = int((stamp - anchor).total_seconds())
    floored = elapsed - (elapsed % interval_sec)
    return anchor + timedelta(seconds=floored)


def slot_floor(as_of: datetime, *, interval_sec: int) -> datetime:
    """Deprecated epoch-grid helper retained only for explicit migration tests."""
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
    markets_norm = normalize_markets(markets)
    region_norm = normalize_regions(region)
    material = "|".join(
        [
            f"v{int(key_version)}",
            str(provider).strip(),
            region_norm,
            markets_norm,
            str(event_id).strip(),
            str(mode).strip(),
            stamp.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        ]
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"odds_snap:{digest}"


def compute_batch_key(
    *,
    provider: str,
    region: str,
    markets: str,
    mode: str,
    slot_or_cutoff: datetime,
    key_version: int = 1,
) -> str:
    """Sport-wide request identity (no event_id) for one provider call per slot."""
    stamp = ensure_utc(slot_or_cutoff, field="slot_or_cutoff")
    material = "|".join(
        [
            f"v{int(key_version)}",
            "batch",
            str(provider).strip(),
            normalize_regions(region),
            normalize_markets(markets),
            str(mode).strip(),
            stamp.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        ]
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"odds_batch:{digest}"


def estimate_endpoint_cost(
    *,
    endpoint: str,
    markets: str | None = None,
    regions: str | None = None,
    contract: OddsScheduleContract | None = None,
) -> int:
    """Estimate quota credits from the provider contract (never invent costs)."""
    sched = contract or load_default_schedule_contract()
    fixed = sched.quota.cost_fixed.get(endpoint)
    if fixed is not None:
        return int(fixed)

    if markets is None or regions is None:
        raise ValueError(
            f"endpoint {endpoint!r} requires markets and regions for cost estimation"
        )
    markets_norm = normalize_markets(markets)
    regions_norm = normalize_regions(regions)
    market_count = len(markets_norm.split(","))
    region_count = len(regions_norm.split(","))
    per = sched.quota.cost_per_region_market.get(endpoint)
    if per is None:
        raise ValueError(f"unknown endpoint cost mapping: {endpoint!r}")
    return int(per) * market_count * region_count


def assert_snapshot_at_or_before(
    *,
    snapshot_at: datetime | None,
    requested_cutoff: datetime,
    as_of: datetime | None = None,
) -> datetime:
    """Fail closed unless snapshot_at <= requested_cutoff (<= as_of when given)."""
    cutoff = ensure_utc(requested_cutoff, field="requested_cutoff")
    if as_of is not None:
        stamp = ensure_utc(as_of, field="as_of")
        if cutoff > stamp:
            raise SnapshotCutoffError(
                f"requested_cutoff {cutoff.isoformat()} is after as_of "
                f"{stamp.isoformat()}; refusing future leakage"
            )
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


def purpose_for_window(window: CadenceWindow | None, *, historical: bool) -> RequestPurpose:
    if historical:
        return RequestPurpose.BACKFILL
    if window is not None and window.requires_quota_headroom:
        return RequestPurpose.LIVE_FINAL
    return RequestPurpose.LIVE_ORDINARY


def compute_due_work(
    *,
    as_of: datetime,
    event_id: str,
    event_start: datetime,
    slot_already_succeeded: bool,
    provider: str,
    markets: str,
    region: str,
    contract: OddsScheduleContract | None = None,
    quota_state: str | None = None,
) -> DueWorkItem:
    """Deterministic due-work decision from explicit UTC inputs only.

    ``slot_already_succeeded`` must be keyed to this window+slot (not prior windows).
    ``quota_state`` is ``None``/``allowed``/``deferred``/``exhausted``.
    """
    sched = contract or load_default_schedule_contract()
    stamp = ensure_utc(as_of, field="as_of")
    start = ensure_utc(event_start, field="event_start")
    markets_n = normalize_markets(markets)
    region_n = normalize_regions(region)

    window = resolve_cadence_window(as_of=stamp, event_start=start, contract=sched)
    if window is None:
        return DueWorkItem(
            action=DueAction.NO_OP,
            event_id=event_id,
            event_start=start,
            as_of=stamp,
            provider=provider,
            markets=markets_n,
            region=region_n,
            window_name=None,
            interval_sec=None,
            slot_start=None,
            window_start=None,
            idempotency_key=None,
            batch_key=None,
            reason="outside_event_odds_window",
            estimated_cost=0,
            requires_quota_headroom=False,
            purpose=None,
        )

    window_start, _window_end = window_bounds(event_start=start, window=window)
    cost = estimate_endpoint_cost(
        endpoint="current_odds",
        markets=markets_n,
        regions=region_n,
        contract=sched,
    )
    slot = slot_floor_in_window(
        stamp, window_start=window_start, interval_sec=window.interval_sec
    )
    mode = f"live:{window.name}"
    key = compute_idempotency_key(
        provider=provider,
        region=region_n,
        markets=markets_n,
        event_id=event_id,
        mode=mode,
        slot_or_cutoff=slot,
        key_version=sched.idempotency_key_version,
    )
    batch = compute_batch_key(
        provider=provider,
        region=region_n,
        markets=markets_n,
        mode=mode,
        slot_or_cutoff=slot,
        key_version=sched.idempotency_key_version,
    )
    purpose = purpose_for_window(window, historical=False)

    if slot_already_succeeded:
        return DueWorkItem(
            action=DueAction.NOT_DUE,
            event_id=event_id,
            event_start=start,
            as_of=stamp,
            provider=provider,
            markets=markets_n,
            region=region_n,
            window_name=window.name,
            interval_sec=window.interval_sec,
            slot_start=slot,
            window_start=window_start,
            idempotency_key=key,
            batch_key=batch,
            reason="slot_already_satisfied",
            estimated_cost=cost,
            requires_quota_headroom=window.requires_quota_headroom,
            purpose=purpose,
        )

    if quota_state == "exhausted":
        return DueWorkItem(
            action=DueAction.EXHAUSTED_QUOTA,
            event_id=event_id,
            event_start=start,
            as_of=stamp,
            provider=provider,
            markets=markets_n,
            region=region_n,
            window_name=window.name,
            interval_sec=window.interval_sec,
            slot_start=slot,
            window_start=window_start,
            idempotency_key=key,
            batch_key=batch,
            reason="quota_exhausted",
            estimated_cost=cost,
            requires_quota_headroom=window.requires_quota_headroom,
            purpose=purpose,
        )
    if quota_state == "deferred":
        return DueWorkItem(
            action=DueAction.DEFERRED_QUOTA,
            event_id=event_id,
            event_start=start,
            as_of=stamp,
            provider=provider,
            markets=markets_n,
            region=region_n,
            window_name=window.name,
            interval_sec=window.interval_sec,
            slot_start=slot,
            window_start=window_start,
            idempotency_key=key,
            batch_key=batch,
            reason="quota_deferred",
            estimated_cost=cost,
            requires_quota_headroom=window.requires_quota_headroom,
            purpose=purpose,
        )

    return DueWorkItem(
        action=DueAction.DUE,
        event_id=event_id,
        event_start=start,
        as_of=stamp,
        provider=provider,
        markets=markets_n,
        region=region_n,
        window_name=window.name,
        interval_sec=window.interval_sec,
        slot_start=slot,
        window_start=window_start,
        idempotency_key=key,
        batch_key=batch,
        reason="interval_elapsed_or_first_window_slot",
        estimated_cost=cost,
        requires_quota_headroom=window.requires_quota_headroom,
        purpose=purpose,
    )


def compute_due_work_for_events(
    *,
    as_of: datetime,
    events: Sequence[Mapping[str, Any]],
    slot_succeeded_keys: Mapping[str, bool] | None = None,
    provider: str | None = None,
    markets: str | None = None,
    region: str | None = None,
    contract: OddsScheduleContract | None = None,
    quota_state: str | None = None,
) -> tuple[DueWorkItem, ...]:
    """Compute due work for a batch of events (explicit UTC ``as_of`` only)."""
    sched = contract or load_default_schedule_contract()
    succeeded = slot_succeeded_keys or {}
    items: list[DueWorkItem] = []
    for event in events:
        event_id = str(event["event_id"])
        event_start = event["event_start"]
        if isinstance(event_start, str):
            event_start = _parse_utc_iso(event_start, field="event_start")
        else:
            event_start = ensure_utc(event_start, field="event_start")
        # Provisional compute to obtain key, then apply success map.
        provisional = compute_due_work(
            as_of=as_of,
            event_id=event_id,
            event_start=event_start,
            slot_already_succeeded=False,
            provider=provider or sched.provider,
            markets=markets or sched.default_markets,
            region=region or sched.default_region,
            contract=sched,
            quota_state=None,
        )
        key = provisional.idempotency_key or ""
        items.append(
            compute_due_work(
                as_of=as_of,
                event_id=event_id,
                event_start=event_start,
                slot_already_succeeded=bool(succeeded.get(key, False)),
                provider=provider or sched.provider,
                markets=markets or sched.default_markets,
                region=region or sched.default_region,
                contract=sched,
                quota_state=quota_state,
            )
        )
    return tuple(items)


__all__ = [
    "EXPECTED_SCHEDULE_CONTRACT_VERSION",
    "PINNED_SCHEDULE_CONTRACT_HASH",
    "SCHEDULE_CONTRACT_ID",
    "CadenceWindow",
    "DueAction",
    "DueWorkItem",
    "OddsScheduleContract",
    "QuotaContract",
    "RequestPurpose",
    "ScheduleContractError",
    "SnapshotCutoffError",
    "SparseCheckpoint",
    "assert_plan_visible_schedule_bytes_match",
    "assert_snapshot_at_or_before",
    "compute_batch_key",
    "compute_due_work",
    "compute_due_work_for_events",
    "compute_idempotency_key",
    "compute_schedule_contract_hash",
    "estimate_endpoint_cost",
    "load_default_schedule_contract",
    "load_schedule_contract",
    "normalize_markets",
    "normalize_regions",
    "package_schedule_resource_path",
    "purpose_for_window",
    "resolve_cadence_window",
    "slot_floor",
    "slot_floor_in_window",
    "sparse_checkpoint_cutoff",
    "window_bounds",
]
