"""Load and validate frozen DWCS event/bout manifests and expected-universe contract."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_EVENTS_PATH = REPO_ROOT / "data" / "manifests" / "dwcs_events_v1.jsonl"
DEFAULT_BOUTS_PATH = REPO_ROOT / "data" / "manifests" / "dwcs_bouts_v1.jsonl"
DEFAULT_MISMATCHES_PATH = REPO_ROOT / "data" / "manifests" / "dwcs_mismatches_v1.json"
DEFAULT_EXPECTED_PATH = (
    REPO_ROOT / "config" / "manifests" / "dwcs_expected_universe_v1.json"
)

# Keep in sync with scripts/spikes/build_dwcs_manifest.py
PINNED_EXPECTED_UNIVERSE_HASH = (
    "e27626347016cf9d8a648f405eb3d4808be1a3ccb83ce19bd67c57da01c64c6f"
)


class ManifestValidationError(ValueError):
    """Raised when frozen manifests fail contract/hash/count validation."""


def _canonical_json_hash(payload: Mapping[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise ManifestValidationError(f"manifest missing: {path}")
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ManifestValidationError(
                    f"malformed JSONL at {path}:{line_no}: {exc}"
                ) from exc
            if not isinstance(payload, dict):
                raise ManifestValidationError(
                    f"JSONL row must be object at {path}:{line_no}"
                )
            rows.append(payload)
    return rows


class DwcsParticipant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    display_name: str
    espn_athlete_id: str
    normalized_name: str
    current_winner_flag: bool


class DwcsResultPayload(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True, populate_by_name=True)

    class_: str = Field(alias="class")
    winner_display_name: str | None = None
    winner_espn_athlete_id: str | None = None
    espn_result_display: str | None = None
    espn_result_name: str | None = None


class DwcsBoutManifestRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    bout_id: str
    espn_competition_id: str
    espn_event_id: str
    event_id: str
    calendar_year: int
    series_variant: str
    status: str
    version_state: str
    weight_class: str | None = None
    participants: tuple[DwcsParticipant, ...]
    event_night_result: DwcsResultPayload
    current_result: DwcsResultPayload
    occurrence_timestamp: str
    occurrence_end_timestamp: str | None = None
    publication_timestamp: str | None = None
    ufcstats_bout_id: str | None = None
    source_ids: Mapping[str, Any]
    data_quality_flags: tuple[str, ...] = ()
    schema_version: int
    manifest_id: str
    manifest_version: str
    built_at: str
    season_number: int | None = None
    week_number: int | None = None
    canonical_participant_pair: tuple[str, ...] | None = None
    reconciliation_evidence_urls: tuple[str, ...] = ()
    reconciliation_notes: str | None = None
    reconciliation_provenance: Any = None
    source_urls: Mapping[str, Any] = Field(default_factory=dict)

    @field_validator("participants")
    @classmethod
    def _two_participants(cls, value: Sequence[DwcsParticipant]) -> tuple[DwcsParticipant, ...]:
        if len(value) != 2:
            raise ValueError(f"bout requires exactly 2 participants, got {len(value)}")
        ids = [p.espn_athlete_id for p in value]
        if ids[0] == ids[1]:
            raise ValueError("bout participants must be distinct espn athlete ids")
        return tuple(value)


class DwcsEventManifestRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    espn_event_id: str
    name: str
    calendar_year: int
    series_variant: str
    status: str
    occurrence_timestamp: str
    publication_timestamp: str | None = None
    ufcstats_event_id: str | None = None
    source_ids: Mapping[str, Any]
    cancellations_replacements: tuple[Mapping[str, Any], ...] = ()
    data_quality_flags: tuple[str, ...] = ()
    schema_version: int
    manifest_id: str
    manifest_version: str
    built_at: str
    bout_count_source: int | None = None
    occurred_bout_count: int | None = None
    season_number: int | None = None
    week_number: int | None = None
    short_name: str | None = None
    venue: Mapping[str, Any] | None = None
    source_urls: Mapping[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class MismatchLedger:
    ok: bool
    mismatch_count: int
    mismatches: tuple[Mapping[str, Any], ...]
    open_gaps: tuple[Mapping[str, Any], ...]
    raw: Mapping[str, Any]


def load_dwcs_bout_manifest(path: Path | None = None) -> list[DwcsBoutManifestRow]:
    target = path or DEFAULT_BOUTS_PATH
    rows = _read_jsonl(target)
    seen: set[str] = set()
    parsed: list[DwcsBoutManifestRow] = []
    for raw in rows:
        bout_id = str(raw.get("bout_id") or "")
        espn_id = str(raw.get("espn_competition_id") or "")
        key = espn_id or bout_id
        if key in seen:
            raise ManifestValidationError(f"duplicate bout row for {key!r}")
        seen.add(key)
        try:
            parsed.append(DwcsBoutManifestRow.model_validate(raw))
        except Exception as exc:  # noqa: BLE001 - fail closed
            raise ManifestValidationError(f"invalid bout row {key!r}: {exc}") from exc
    return parsed


def load_dwcs_event_manifest(path: Path | None = None) -> list[DwcsEventManifestRow]:
    target = path or DEFAULT_EVENTS_PATH
    rows = _read_jsonl(target)
    seen: set[str] = set()
    parsed: list[DwcsEventManifestRow] = []
    for raw in rows:
        event_id = str(raw.get("event_id") or "")
        espn_id = str(raw.get("espn_event_id") or "")
        key = espn_id or event_id
        if key in seen:
            raise ManifestValidationError(f"duplicate event row for {key!r}")
        seen.add(key)
        try:
            parsed.append(DwcsEventManifestRow.model_validate(raw))
        except Exception as exc:  # noqa: BLE001
            raise ManifestValidationError(f"invalid event row {key!r}: {exc}") from exc
    return parsed


def load_dwcs_mismatch_ledger(path: Path | None = None) -> MismatchLedger:
    target = path or DEFAULT_MISMATCHES_PATH
    if not target.is_file():
        raise ManifestValidationError(f"mismatch ledger missing: {target}")
    raw = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ManifestValidationError("mismatch ledger must be an object")
    mismatches = tuple(raw.get("mismatches") or ())
    open_gaps = tuple(raw.get("open_gaps") or ())
    return MismatchLedger(
        ok=bool(raw.get("ok")),
        mismatch_count=int(raw.get("mismatch_count") or 0),
        mismatches=mismatches,  # type: ignore[arg-type]
        open_gaps=open_gaps,  # type: ignore[arg-type]
        raw=raw,
    )


def validate_expected_universe(
    *,
    events: Sequence[DwcsEventManifestRow],
    bouts: Sequence[DwcsBoutManifestRow],
    expected_path: Path | None = None,
    expected_hash: str = PINNED_EXPECTED_UNIVERSE_HASH,
) -> Mapping[str, Any]:
    path = expected_path or DEFAULT_EXPECTED_PATH
    if not path.is_file():
        raise ManifestValidationError(f"expected-universe contract missing: {path}")
    expected = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(expected, dict):
        raise ManifestValidationError("expected-universe must be an object")
    digest = _canonical_json_hash(expected)
    if digest != expected_hash:
        raise ManifestValidationError(
            f"pinned expected-universe hash mismatch: got {digest}, want {expected_hash}"
        )

    cards_all = int(expected["cards"]["all"])
    bouts_all = int(expected["bouts"]["all"])
    if len(events) != cards_all:
        raise ManifestValidationError(
            f"event count {len(events)} != expected cards.all {cards_all}"
        )
    if len(bouts) != bouts_all:
        raise ManifestValidationError(
            f"bout count {len(bouts)} != expected bouts.all {bouts_all}"
        )

    from collections import Counter

    event_variants = Counter(e.series_variant for e in events)
    bout_variants = Counter(b.series_variant for b in bouts)
    if event_variants.get("standard", 0) != int(expected["cards"]["standard"]):
        raise ManifestValidationError("standard card count mismatch vs expected universe")
    if event_variants.get("brazil", 0) != int(expected["cards"]["brazil"]):
        raise ManifestValidationError("brazil card count mismatch vs expected universe")
    if bout_variants.get("standard", 0) != int(expected["bouts"]["standard"]):
        raise ManifestValidationError("standard bout count mismatch vs expected universe")
    if bout_variants.get("brazil", 0) != int(expected["bouts"]["brazil"]):
        raise ManifestValidationError("brazil bout count mismatch vs expected universe")

    en = Counter(b.event_night_result.class_ for b in bouts)
    cur = Counter(b.current_result.class_ for b in bouts)
    en_exp = expected["event_night_results"]
    cur_exp = expected["current_results"]
    for key in ("decisive", "draw", "no_contest"):
        if en.get(key, 0) != int(en_exp[key]):
            raise ManifestValidationError(
                f"event_night_results.{key} mismatch: {en.get(key, 0)} != {en_exp[key]}"
            )
        if cur.get(key, 0) != int(cur_exp[key]):
            raise ManifestValidationError(
                f"current_results.{key} mismatch: {cur.get(key, 0)} != {cur_exp[key]}"
            )

    return expected
