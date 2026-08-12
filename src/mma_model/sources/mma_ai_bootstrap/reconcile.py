"""mma-ai bootstrap reconciliation gates (DWCS-102)."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class BootstrapReject(ValueError):
    """Kill-reason reject for mma_ai_bootstrap dumps."""


OPAQUE_FEATURE_HEADERS = frozenset(
    {
        "feature_elo",
        "feature_reach_diff",
        "opaque_vector",
        "precomputed_feature",
        "model_feature_matrix",
    }
)

REQUIRED_SCHEMA_FIELDS = frozenset(
    {
        "fight_id",
        "event_id",
        "fighter_a_id",
        "fighter_b_id",
        "result",
    }
)


@dataclass(frozen=True)
class ReconcileReport:
    path: Path
    row_count: int
    hash_agreement: float
    schema_fields: tuple[str, ...]
    rows: tuple[dict[str, Any], ...]
    kill_reason: str | None = None


def reconcile_mma_ai_dump(
    *,
    normalized_path: Path,
    ufcstats_sample_hashes: dict[str, str],
    expected_counts: dict[str, int],
) -> ReconcileReport:
    path = Path(normalized_path)
    _reject_opaque_path(path)

    if path.suffix.lower() == ".csv":
        rows, fields = _read_csv(path)
    elif path.suffix.lower() in {".jsonl", ".json"}:
        rows, fields = _read_jsonl(path)
    else:
        raise BootstrapReject(
            f"mma_ai_bootstrap: unsupported dump format {path.suffix!r}"
        )

    if OPAQUE_FEATURE_HEADERS.intersection(fields):
        raise BootstrapReject(
            "mma_ai_bootstrap: opaque_precomputed_feature columns present"
        )

    missing_schema = sorted(REQUIRED_SCHEMA_FIELDS - set(fields))
    if missing_schema:
        raise BootstrapReject(
            f"mma_ai_bootstrap: schema mismatch missing={missing_schema}"
        )

    if "fights" in expected_counts and expected_counts["fights"] != len(rows):
        raise BootstrapReject(
            f"mma_ai_bootstrap: count mismatch fights "
            f"expected={expected_counts['fights']} got={len(rows)}"
        )
    for key, expected in expected_counts.items():
        if key == "fights":
            continue
        actual = sum(1 for row in rows if str(row.get(key, "")) != "")
        if actual != expected:
            raise BootstrapReject(
                f"mma_ai_bootstrap: count mismatch {key} "
                f"expected={expected} got={actual}"
            )

    hash_agreement = 1.0
    if ufcstats_sample_hashes:
        matched = 0
        compared = 0
        for fight_id, expected_hash in sorted(ufcstats_sample_hashes.items()):
            compared += 1
            row = next((r for r in rows if str(r.get("fight_id")) == fight_id), None)
            if row is None:
                continue
            actual_hash = str(row.get("payload_hash") or "")
            if actual_hash == expected_hash:
                matched += 1
        hash_agreement = matched / compared if compared else 0.0
        if hash_agreement < 0.99:
            raise BootstrapReject(
                f"mma_ai_bootstrap: hash reconciliation failed "
                f"agreement={hash_agreement:.4f}"
            )

    ordered = tuple(sorted(rows, key=lambda r: str(r.get("fight_id", ""))))
    return ReconcileReport(
        path=path,
        row_count=len(ordered),
        hash_agreement=hash_agreement,
        schema_fields=tuple(sorted(fields)),
        rows=ordered,
    )


def _reject_opaque_path(path: Path) -> None:
    name = path.name.lower()
    if "feature" in name and path.suffix.lower() == ".csv":
        raise BootstrapReject(
            "mma_ai_bootstrap: opaque_precomputed_feature path rejected"
        )


def _read_csv(path: Path) -> tuple[list[dict[str, Any]], set[str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise BootstrapReject("mma_ai_bootstrap: csv missing header")
        fields = {name.strip() for name in reader.fieldnames if name}
        rows = [dict(row) for row in reader]
    return rows, fields


def _read_jsonl(path: Path) -> tuple[list[dict[str, Any]], set[str]]:
    rows: list[dict[str, Any]] = []
    fields: set[str] = set()
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return [], set()
    # Accept either JSONL or a single JSON array/object dump.
    if text.startswith("["):
        payload = json.loads(text)
        if not isinstance(payload, list):
            raise BootstrapReject("mma_ai_bootstrap: json root must be array")
        for item in payload:
            if not isinstance(item, dict):
                raise BootstrapReject("mma_ai_bootstrap: json rows must be objects")
            rows.append(item)
            fields.update(item.keys())
        return rows, fields
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # Support CSV saved with .jsonl extension in fixtures: detect header commas.
        if "fight_id" in line and "event_id" in line and "," in line and not line.startswith("{"):
            # Treat as CSV content mistakenly given jsonl suffix.
            return _read_csv(path)
        item = json.loads(line)
        if not isinstance(item, dict):
            raise BootstrapReject("mma_ai_bootstrap: jsonl rows must be objects")
        rows.append(item)
        fields.update(item.keys())
    return rows, fields
