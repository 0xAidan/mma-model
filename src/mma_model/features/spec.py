"""Ordered PIT feature schema, roles, and hashes (DWCS-301)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping, Never, Sequence

from mma_model.quality.schema import canonical_json_bytes, sha256_canonical

SPEC_VERSION = "dwcs_pit_v1.1"


class FeatureRole(StrEnum):
    DIFF = "diff"
    PAIRED = "paired"
    SHARED = "shared"


class DataQualityFlag(StrEnum):
    HEALTHY = "healthy"
    PARTIAL = "partial"
    SPARSE = "sparse"


@dataclass(frozen=True)
class FeatureField:
    name: str
    role: FeatureRole
    pair: str | None = None


def _paired(stem: str) -> tuple[FeatureField, FeatureField]:
    a_name = f"{stem}_a"
    b_name = f"{stem}_b"
    return (
        FeatureField(a_name, FeatureRole.PAIRED, b_name),
        FeatureField(b_name, FeatureRole.PAIRED, a_name),
    )


def _paired_with_missing(stem: str) -> tuple[FeatureField, ...]:
    return _paired(stem) + _paired(f"{stem}_missing")


def _diff(name: str) -> FeatureField:
    return FeatureField(name, FeatureRole.DIFF)


def _shared(name: str) -> FeatureField:
    return FeatureField(name, FeatureRole.SHARED)


FEATURE_FIELDS: tuple[FeatureField, ...] = (
    _diff("rating_diff"),
    _shared("rating_sd_sum"),
    *_paired("rating"),
    *_paired("rating_sd"),
    *_paired("prior_decisive_bouts"),
    *_paired("rating_missing"),
    *_paired("prior_fights"),
    *_paired("prior_minutes"),
    *_paired("prior_rounds"),
    *_paired_with_missing("layoff_days"),
    *_paired("activity_365d"),
    *_paired("debut"),
    *_paired("pro_bouts"),
    *_paired("amateur_bouts"),
    *_paired("ufc_dwcs_bouts"),
    *_paired("regional_bouts"),
    *_paired_with_missing("ko_win_rate"),
    *_paired_with_missing("sub_win_rate"),
    *_paired_with_missing("dec_win_rate"),
    *_paired_with_missing("ko_loss_rate"),
    *_paired_with_missing("sub_loss_rate"),
    *_paired_with_missing("dec_loss_rate"),
    *_paired_with_missing("finish_elapsed_mean"),
    *_paired_with_missing("ufc_dwcs_share"),
    *_paired_with_missing("sig_str_landed_pm"),
    _diff("sig_str_landed_pm_diff"),
    *_paired_with_missing("sig_str_acc"),
    _diff("sig_str_acc_diff"),
    *_paired_with_missing("opp_sig_str_landed_pm"),
    *_paired_with_missing("td_landed_per_15"),
    *_paired_with_missing("td_acc"),
    *_paired_with_missing("td_att_per_15"),
    _diff("td_landed_per_15_diff"),
    *_paired_with_missing("td_absorbed_per_15"),
    *_paired_with_missing("sub_att_per_15"),
    *_paired_with_missing("ctrl_per_min"),
    *_paired("perf_missing"),
    *_paired("opp_perf_missing"),
    *_paired_with_missing("age"),
    _diff("age_diff"),
    *_paired_with_missing("reach"),
    _diff("reach_diff"),
    *_paired_with_missing("height"),
    _diff("height_diff"),
    *_paired("stance_orthodox"),
    *_paired("stance_southpaw"),
    *_paired("stance_switch"),
    *_paired("stance_missing"),
    *_paired_with_missing("short_notice"),
    _shared("scheduled_rounds"),
    _shared("scheduled_rounds_missing"),
    _shared("weight_class_missing"),
    _shared("is_proxy_cutoff"),
    _shared("data_completeness"),
)

FEATURE_NAMES: tuple[str, ...] = tuple(field.name for field in FEATURE_FIELDS)
FEATURE_BY_NAME: dict[str, FeatureField] = {field.name: field for field in FEATURE_FIELDS}


def _require_role(role: FeatureRole) -> None:
    if role is FeatureRole.DIFF:
        return
    if role is FeatureRole.PAIRED:
        return
    if role is FeatureRole.SHARED:
        return
    never_role: Never = role
    raise ValueError(f"unhandled feature role: {never_role!r}")


def spec_payload() -> dict[str, object]:
    return {
        "names": list(FEATURE_NAMES),
        "version": SPEC_VERSION,
        "roles": [
            {"name": field.name, "pair": field.pair, "role": field.role.value}
            for field in FEATURE_FIELDS
        ],
    }


def spec_hash() -> str:
    return sha256_canonical(spec_payload())


def canon_float(value: float) -> float:
    rounded = round(float(value), 10)
    if rounded == 0.0:
        return 0.0
    return rounded


def vector_from_mapping(values: Mapping[str, float]) -> tuple[float, ...]:
    missing = [name for name in FEATURE_NAMES if name not in values]
    if missing:
        raise KeyError(f"feature mapping missing fields: {missing[:8]!r}")
    extra = [name for name in values if name not in FEATURE_BY_NAME]
    if extra:
        raise KeyError(f"feature mapping has unknown fields: {extra[:8]!r}")
    return tuple(canon_float(values[name]) for name in FEATURE_NAMES)


def row_payload(values: Sequence[float]) -> dict[str, object]:
    if len(values) != len(FEATURE_NAMES):
        raise ValueError(
            f"feature row length {len(values)} != spec length {len(FEATURE_NAMES)}"
        )
    return {"values": [canon_float(v) for v in values], "version": SPEC_VERSION}


def row_bytes(values: Sequence[float]) -> bytes:
    return canonical_json_bytes(row_payload(values))


def row_hash(values: Sequence[float]) -> str:
    return sha256_canonical(row_payload(values))


def swap_values(values: Sequence[float]) -> tuple[float, ...]:
    """Negate diffs and swap paired fields. Shared fields stay put."""
    by_name = {name: canon_float(values[idx]) for idx, name in enumerate(FEATURE_NAMES)}
    swapped: dict[str, float] = {}
    for field in FEATURE_FIELDS:
        _require_role(field.role)
        current = by_name[field.name]
        if field.role is FeatureRole.DIFF:
            swapped[field.name] = canon_float(-current)
            continue
        if field.role is FeatureRole.PAIRED:
            if field.pair is None:
                raise ValueError(f"paired field {field.name} has no pair")
            swapped[field.name] = by_name[field.pair]
            continue
        if field.role is FeatureRole.SHARED:
            swapped[field.name] = current
            continue
        never_role: Never = field.role
        raise ValueError(f"unhandled feature role: {never_role!r}")
    return vector_from_mapping(swapped)


def safe_diff(left: float, right: float, left_missing: float, right_missing: float) -> float:
    """A-minus-B that is 0 when either side is missing (not a fake zero gap)."""
    if left_missing or right_missing:
        return 0.0
    return left - right


def quality_flag_for(completeness: float) -> DataQualityFlag:
    if completeness >= 0.75:
        return DataQualityFlag.HEALTHY
    if completeness >= 0.40:
        return DataQualityFlag.PARTIAL
    return DataQualityFlag.SPARSE


def missing_field_names() -> tuple[str, ...]:
    names: list[str] = []
    for name in FEATURE_NAMES:
        if name.endswith("_missing") or name.endswith("_missing_a") or name.endswith("_missing_b"):
            names.append(name)
            continue
        if name in {
            "perf_missing_a",
            "perf_missing_b",
            "opp_perf_missing_a",
            "opp_perf_missing_b",
            "rating_missing_a",
            "rating_missing_b",
            "stance_missing_a",
            "stance_missing_b",
        }:
            names.append(name)
    return tuple(names)
