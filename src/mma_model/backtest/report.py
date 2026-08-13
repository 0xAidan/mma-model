"""Immutable JSON/Markdown walk-forward evidence (DWCS-306).

Canonical JSON + content hash. ``generated_at`` is omitted from the hashed
payload (or supplied explicitly for byte-stable files). Writes are atomic
(temp then replace) and refuse to overwrite a different existing file.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from mma_model.backtest.gates import EvidenceOverwriteError, EvidenceTamperError
from mma_model.quality.schema import canonical_json_bytes, sha256_canonical

EVIDENCE_SCHEMA_VERSION: Final = "dwcs_backtest_evidence_v1"
JSON_NAME: Final = "backtest.json"
MARKDOWN_NAME: Final = "backtest.md"
HASHED_OMIT_KEYS: Final = frozenset({"generated_at"})


def canonical_dumps(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload,
        sort_keys=True,
        indent=2,
        ensure_ascii=True,
        default=str,
    ) + "\n"


def hashed_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Copy payload without generated_at (and without content_hash itself)."""
    return {
        key: value
        for key, value in payload.items()
        if key not in HASHED_OMIT_KEYS and key != "content_hash"
    }


def compute_evidence_hash(payload: Mapping[str, Any]) -> str:
    return sha256_canonical(hashed_payload(payload))


def attach_content_hash(payload: Mapping[str, Any]) -> dict[str, Any]:
    body = dict(payload)
    body.pop("content_hash", None)
    digest = compute_evidence_hash(body)
    body["content_hash"] = digest
    return body


def verify_evidence_payload(payload: Mapping[str, Any]) -> str:
    digest = compute_evidence_hash(payload)
    stored = str(payload.get("content_hash") or "")
    if stored != digest:
        raise EvidenceTamperError(
            f"evidence content hash mismatch: got {stored}, expected {digest}"
        )
    return digest


def markdown_from_payload(payload: Mapping[str, Any]) -> str:
    universe = payload.get("universe", {})
    selection = payload.get("metrics", {}).get("all_dwcs", {}).get("selection", {})
    betting = payload.get("metrics", {}).get("all_dwcs", {}).get("betting", {})
    holdout = payload.get("holdout", {})
    hashes = payload.get("hashes", {})
    lines = [
        "# DWCS-306 walk-forward backtest evidence",
        "",
        f"- schema: `{payload.get('schema_version')}`",
        f"- content_hash: `{payload.get('content_hash')}`",
        f"- git_commit: `{payload.get('git_commit')}`",
        f"- sealed_holdout: `{holdout.get('sealed_holdout')}`",
        f"- holdout_accessed: `{holdout.get('holdout_accessed')}`",
        (
            f"- universe cards/bouts: {universe.get('cards')}/"
            f"{universe.get('bouts')} "
            f"(standard {universe.get('standard_cards')}/{universe.get('standard_bouts')}, "
            f"brazil {universe.get('brazil_cards')}/{universe.get('brazil_bouts')})"
        ),
        (
            f"- attempted: {selection.get('attempted', {}).get('value')} "
            f"predicted: {selection.get('predicted', {}).get('value')} "
            f"excluded: {selection.get('excluded', {}).get('value')} "
            f"locked_not_accessed: {selection.get('locked_not_accessed', {}).get('value')}"
        ),
        (
            f"- priced: {selection.get('priced', {}).get('value')} "
            f"threshold_only: {selection.get('threshold_only', {}).get('value')} "
            f"pre_policy_candidates: {selection.get('pre_policy_candidates', {}).get('value')}"
        ),
        (
            f"- flat 1-unit ROI: {betting.get('flat_1_unit_roi', {}).get('value')} "
            f"(n={betting.get('flat_1_unit_roi', {}).get('denominator')})"
        ),
        (
            f"- quarter-Kelly ROI: {betting.get('quarter_kelly_roi', {}).get('value')} "
            f"drawdown: {betting.get('maximum_drawdown', {}).get('value')}"
        ),
        f"- cutoff_policy: `{payload.get('cutoff_policy')}`",
        f"- bootstrap_seed: `{payload.get('bootstrap', {}).get('seed')}`",
        f"- bootstrap_replicates: `{payload.get('bootstrap', {}).get('n_replicates')}`",
        "",
        "## Hashes",
        "",
    ]
    for key in sorted(hashes):
        lines.append(f"- `{key}`: `{hashes[key]}`")
    lines.extend(["", "## Exclusion reasons", ""])
    reasons = selection.get("exclusion_reasons") or {}
    if not reasons:
        lines.append("- none")
    else:
        for reason in sorted(reasons):
            lines.append(f"- `{reason}`: {reasons[reason]}")
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- Threshold-only rows receive no synthetic EV/ROI/CLV/profit/stake.",
            "- `pre_policy_candidate` uses frozen contract thresholds; not a recommendation.",
            "- 2025 is locked unless `--sealed-holdout` and never enters training.",
            "- Fight-by-fight walk-forward is not betting evidence.",
            "",
        ]
    )
    return "\n".join(lines)


def run_directory(output_dir: Path, content_hash: str) -> Path:
    return Path(output_dir) / f"run_{content_hash[:16]}"


def _atomic_write_bytes(path: Path, blob: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise


def _guard_existing(path: Path, blob: bytes) -> None:
    if not path.exists():
        return
    existing = path.read_bytes()
    if existing == blob:
        return
    raise EvidenceOverwriteError(
        f"refusing to overwrite different evidence at {path}"
    )


def write_evidence_files(
    output_dir: Path,
    payload: Mapping[str, Any],
) -> dict[str, str]:
    """Write versioned JSON + Markdown. Same bytes are idempotent; other bytes fail."""
    attached = attach_content_hash(dict(payload))
    verify_evidence_payload(attached)
    digest = str(attached["content_hash"])
    target = run_directory(output_dir, digest)
    json_path = target / JSON_NAME
    md_path = target / MARKDOWN_NAME
    json_blob = canonical_dumps(attached).encode("utf-8")
    md_blob = markdown_from_payload(attached).encode("utf-8")
    _guard_existing(json_path, json_blob)
    _guard_existing(md_path, md_blob)
    if not json_path.exists():
        _atomic_write_bytes(json_path, json_blob)
    if not md_path.exists():
        _atomic_write_bytes(md_path, md_blob)
    roundtrip = json.loads(json_path.read_text(encoding="utf-8"))
    verify_evidence_payload(roundtrip)
    return {
        "content_hash": digest,
        "directory": str(target),
        "json_path": str(json_path),
        "markdown_path": str(md_path),
    }


def load_evidence(path: Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise EvidenceTamperError("evidence root must be a JSON object")
    verify_evidence_payload(payload)
    return payload


def utc_now() -> datetime:
    return datetime.now(UTC)


def isoformat_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def metric_definitions() -> dict[str, str]:
    return {
        "accuracy_descriptive_only": "Hit rate of moneyline P>=0.5; descriptive only",
        "brier": "Mean squared error of moneyline probabilities on decisive bouts",
        "clv": "Mean same-selection probability CLV (close implied - bet implied)",
        "flat_1_unit_roi": "Mean flat 1-unit profit on settled qualifying priced bets",
        "joint_log_loss": "Mean terminal-atom negative log likelihood",
        "longest_losing_run": "Longest consecutive LOSS streak (push/void reset)",
        "market_log_loss": "Binary moneyline negative log likelihood",
        "maximum_drawdown": "Peak-to-trough fraction of capped quarter-Kelly bankroll",
        "quarter_kelly_roi": "Bankroll change from 1.0 under capped quarter-Kelly",
        "turnover": "Sum of flat 1-unit stakes on settled qualifying bets",
    }


__all__ = [
    "EVIDENCE_SCHEMA_VERSION",
    "HASHED_OMIT_KEYS",
    "JSON_NAME",
    "MARKDOWN_NAME",
    "attach_content_hash",
    "canonical_dumps",
    "canonical_json_bytes",
    "compute_evidence_hash",
    "isoformat_utc",
    "load_evidence",
    "markdown_from_payload",
    "metric_definitions",
    "run_directory",
    "utc_now",
    "verify_evidence_payload",
    "write_evidence_files",
]
