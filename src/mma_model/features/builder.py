"""Symmetric point-in-time matchup feature builder (DWCS-301).

``build(a, b, cutoff)`` and ``build(b, a, cutoff)`` negate diffs and swap
paired fields. Cache keys are ``(bout_id, cutoff, spec_hash, a, b)`` and store
immutable row bytes computed only from admitted observations.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Never

from mma_model.features.as_of import (
    AsOfCutoff,
    CutoffKind,
    EventCutoffRegistry,
    observation_admitted,
)
from mma_model.features.career import matchup_career
from mma_model.features.performance import matchup_performance
from mma_model.features.snapshot import FeatureSnapshot, SnapshotProfileObservation
from mma_model.features.spec import (
    FEATURE_NAMES,
    SPEC_VERSION,
    DataQualityFlag,
    canon_float,
    missing_field_names,
    quality_flag_for,
    row_bytes,
    row_hash,
    spec_hash,
    vector_from_mapping,
)
from mma_model.features.strength import matchup_strength

DAYS_PER_YEAR = 365.25

PROFILE_HEIGHT = frozenset({"height", "height_in", "height_cm"})
PROFILE_REACH = frozenset({"reach", "reach_in", "reach_cm"})
PROFILE_STANCE = frozenset({"stance"})
PROFILE_DOB = frozenset({"dob", "date_of_birth", "born"})
PROFILE_SHORT_NOTICE = frozenset({"short_notice", "replacement", "late_replacement"})


@dataclass(frozen=True)
class FeatureRow:
    bout_id: str | None
    fighter_a_id: str
    fighter_b_id: str
    cutoff: AsOfCutoff
    spec_version: str
    spec_hash: str
    names: tuple[str, ...]
    values: tuple[float, ...]
    row_hash: str
    row_bytes: bytes
    data_completeness: float
    quality_flag: DataQualityFlag
    weight_class: str | None = None


class FeatureRowCache:
    """Immutable PIT row bytes keyed by bout, cutoff, spec, and corner order."""

    def __init__(self) -> None:
        self._store: dict[tuple[str, str, str, str, str], bytes] = {}

    @staticmethod
    def make_key(
        *,
        bout_id: str,
        cutoff: AsOfCutoff,
        spec: str,
        fighter_a_id: str,
        fighter_b_id: str,
    ) -> tuple[str, str, str, str, str]:
        cutoff_fp = f"{cutoff.event_id}|{cutoff.cutoff.isoformat()}|{cutoff.cutoff_kind.value}"
        return (bout_id, cutoff_fp, spec, fighter_a_id, fighter_b_id)

    def get(self, key: tuple[str, str, str, str, str]) -> bytes | None:
        return self._store.get(key)

    def put(self, key: tuple[str, str, str, str, str], payload: bytes) -> None:
        self._store[key] = payload


def _latest_profile(
    snapshot: FeatureSnapshot,
    fighter_id: str,
    attributes: frozenset[str],
    cutoff: AsOfCutoff,
) -> SnapshotProfileObservation | None:
    eligible: list[SnapshotProfileObservation] = []
    for row in snapshot.profiles:
        if row.fighter_id != fighter_id:
            continue
        if row.attribute not in attributes:
            continue
        if not observation_admitted(
            effective_at=row.effective_at,
            observed_at=row.observed_at,
            cutoff=cutoff,
        ):
            continue
        eligible.append(row)
    if not eligible:
        return None
    return max(eligible, key=lambda row: (row.effective_at, row.observed_at, row.attribute))


def _num_or_none(row: SnapshotProfileObservation | None) -> float | None:
    if row is None:
        return None
    if row.value_num is not None:
        return float(row.value_num)
    if row.value_text is None:
        return None
    text = row.value_text.strip().replace('"', "")
    try:
        return float(text)
    except ValueError:
        return None


def _age_years(dob: date | None, cutoff: datetime) -> float | None:
    if dob is None:
        return None
    days = (cutoff.date() - dob).days
    if days < 0:
        return None
    return days / DAYS_PER_YEAR


def _stance_flags(text: str | None) -> tuple[float, float, float, float]:
    if text is None or not text.strip():
        return 0.0, 0.0, 0.0, 1.0
    key = " ".join(text.lower().split())
    orthodox = 1.0 if key in {"orthodox", "right"} else 0.0
    southpaw = 1.0 if key in {"southpaw", "left"} else 0.0
    switch = 1.0 if key in {"switch", "switch-stance", "switch stance"} else 0.0
    if orthodox == 0.0 and southpaw == 0.0 and switch == 0.0:
        return 0.0, 0.0, 0.0, 1.0
    return orthodox, southpaw, switch, 0.0


def _truthy_notice(row: SnapshotProfileObservation | None) -> tuple[float, float]:
    if row is None:
        return 0.0, 1.0
    if row.value_num is not None:
        return (1.0 if row.value_num else 0.0), 0.0
    if row.value_text is None:
        return 0.0, 1.0
    key = row.value_text.strip().lower()
    if key in {"1", "true", "yes", "short_notice", "replacement"}:
        return 1.0, 0.0
    if key in {"0", "false", "no"}:
        return 0.0, 0.0
    return 0.0, 1.0


def _physical_for_fighter(
    snapshot: FeatureSnapshot,
    fighter_id: str,
    cutoff: AsOfCutoff,
    *,
    prefix: str,
) -> dict[str, float]:
    height_row = _latest_profile(snapshot, fighter_id, PROFILE_HEIGHT, cutoff)
    reach_row = _latest_profile(snapshot, fighter_id, PROFILE_REACH, cutoff)
    stance_row = _latest_profile(snapshot, fighter_id, PROFILE_STANCE, cutoff)
    dob_row = _latest_profile(snapshot, fighter_id, PROFILE_DOB, cutoff)
    notice_row = _latest_profile(snapshot, fighter_id, PROFILE_SHORT_NOTICE, cutoff)

    height = _num_or_none(height_row)
    reach = _num_or_none(reach_row)
    dob = dob_row.value_date if dob_row is not None else None
    age = _age_years(dob, cutoff.cutoff)
    orthodox, southpaw, switch, stance_missing = _stance_flags(
        stance_row.value_text if stance_row is not None else None
    )
    short_notice, short_missing = _truthy_notice(notice_row)

    return {
        f"age_{prefix}": 0.0 if age is None else age,
        f"age_missing_{prefix}": 1.0 if age is None else 0.0,
        f"reach_{prefix}": 0.0 if reach is None else reach,
        f"reach_missing_{prefix}": 1.0 if reach is None else 0.0,
        f"height_{prefix}": 0.0 if height is None else height,
        f"height_missing_{prefix}": 1.0 if height is None else 0.0,
        f"stance_orthodox_{prefix}": orthodox,
        f"stance_southpaw_{prefix}": southpaw,
        f"stance_switch_{prefix}": switch,
        f"stance_missing_{prefix}": stance_missing,
        f"short_notice_{prefix}": short_notice,
        f"short_notice_missing_{prefix}": short_missing,
    }


def _safe_diff(left: float, right: float, left_missing: float, right_missing: float) -> float:
    if left_missing or right_missing:
        return 0.0
    return left - right


def _require_cutoff_kind(kind: CutoffKind) -> float:
    if kind is CutoffKind.SCHEDULED_MINUS_60M:
        return 0.0
    if kind is CutoffKind.PROXY_SCHEDULED_START:
        return 1.0
    never_kind: Never = kind
    raise ValueError(f"unhandled cutoff kind: {never_kind!r}")


def _completeness(values: dict[str, float]) -> float:
    flags = missing_field_names()
    if not flags:
        return 1.0
    total = sum(values[name] for name in flags)
    return 1.0 - (total / len(flags))


@dataclass
class FeatureBuilder:
    snapshot: FeatureSnapshot
    cache: FeatureRowCache = field(default_factory=FeatureRowCache)
    _cutoffs: EventCutoffRegistry = field(default_factory=EventCutoffRegistry)

    def build(
        self,
        fighter_a_id: str,
        fighter_b_id: str,
        cutoff: AsOfCutoff,
        *,
        bout_id: str | None = None,
        use_cache: bool = True,
    ) -> FeatureRow:
        self._cutoffs.register(cutoff)
        spec = spec_hash()
        cache_key = None
        if use_cache and bout_id is not None:
            cache_key = FeatureRowCache.make_key(
                bout_id=bout_id,
                cutoff=cutoff,
                spec=spec,
                fighter_a_id=fighter_a_id,
                fighter_b_id=fighter_b_id,
            )
            cached = self.cache.get(cache_key)
            if cached is not None:
                return self._row_from_cached(
                    cached,
                    fighter_a_id=fighter_a_id,
                    fighter_b_id=fighter_b_id,
                    cutoff=cutoff,
                    bout_id=bout_id,
                    spec=spec,
                )

        values = self._compute(fighter_a_id, fighter_b_id, cutoff, bout_id=bout_id)
        vector = vector_from_mapping(values)
        payload = row_bytes(vector)
        if cache_key is not None:
            self.cache.put(cache_key, payload)
        completeness = canon_float(values["data_completeness"])
        bout = self.snapshot.bout_by_id(bout_id) if bout_id is not None else None
        return FeatureRow(
            bout_id=bout_id,
            fighter_a_id=fighter_a_id,
            fighter_b_id=fighter_b_id,
            cutoff=cutoff,
            spec_version=SPEC_VERSION,
            spec_hash=spec,
            names=FEATURE_NAMES,
            values=vector,
            row_hash=row_hash(vector),
            row_bytes=payload,
            data_completeness=completeness,
            quality_flag=quality_flag_for(completeness),
            weight_class=bout.weight_class if bout is not None else None,
        )

    def _row_from_cached(
        self,
        payload: bytes,
        *,
        fighter_a_id: str,
        fighter_b_id: str,
        cutoff: AsOfCutoff,
        bout_id: str,
        spec: str,
    ) -> FeatureRow:
        decoded = json.loads(payload.decode("utf-8"))
        raw_values = decoded["values"]
        vector = tuple(canon_float(float(v)) for v in raw_values)
        completeness_idx = FEATURE_NAMES.index("data_completeness")
        completeness = vector[completeness_idx]
        bout = self.snapshot.bout_by_id(bout_id)
        return FeatureRow(
            bout_id=bout_id,
            fighter_a_id=fighter_a_id,
            fighter_b_id=fighter_b_id,
            cutoff=cutoff,
            spec_version=SPEC_VERSION,
            spec_hash=spec,
            names=FEATURE_NAMES,
            values=vector,
            row_hash=row_hash(vector),
            row_bytes=payload,
            data_completeness=completeness,
            quality_flag=quality_flag_for(completeness),
            weight_class=bout.weight_class if bout is not None else None,
        )

    def _compute(
        self,
        fighter_a_id: str,
        fighter_b_id: str,
        cutoff: AsOfCutoff,
        *,
        bout_id: str | None,
    ) -> dict[str, float]:
        strength = matchup_strength(self.snapshot, fighter_a_id, fighter_b_id, cutoff)
        career = matchup_career(self.snapshot, fighter_a_id, fighter_b_id, cutoff)
        performance = matchup_performance(self.snapshot, fighter_a_id, fighter_b_id, cutoff)
        physical_a = _physical_for_fighter(self.snapshot, fighter_a_id, cutoff, prefix="a")
        physical_b = _physical_for_fighter(self.snapshot, fighter_b_id, cutoff, prefix="b")
        bout = self.snapshot.bout_by_id(bout_id) if bout_id is not None else None
        scheduled = float(bout.scheduled_rounds) if bout is not None else 3.0
        weight_missing = 1.0 if bout is None or not bout.weight_class else 0.0
        values: dict[str, float] = {
            **strength,
            **career,
            **performance,
            **physical_a,
            **physical_b,
            "age_diff": _safe_diff(
                physical_a["age_a"],
                physical_b["age_b"],
                physical_a["age_missing_a"],
                physical_b["age_missing_b"],
            ),
            "reach_diff": _safe_diff(
                physical_a["reach_a"],
                physical_b["reach_b"],
                physical_a["reach_missing_a"],
                physical_b["reach_missing_b"],
            ),
            "height_diff": _safe_diff(
                physical_a["height_a"],
                physical_b["height_b"],
                physical_a["height_missing_a"],
                physical_b["height_missing_b"],
            ),
            "scheduled_rounds": scheduled,
            "weight_class_missing": weight_missing,
            "is_proxy_cutoff": _require_cutoff_kind(cutoff.cutoff_kind),
            "data_completeness": 0.0,
        }
        values["data_completeness"] = _completeness(values)
        return values
