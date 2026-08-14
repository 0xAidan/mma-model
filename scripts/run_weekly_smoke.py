#!/usr/bin/env python3
"""DWCS-404 weekly lifecycle smoke runner (fixture-only, no live network)."""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mma_model.observability.health import (  # noqa: E402
    HEALTH_COMPONENT_NAMES,
    load_health_state,
)
from tests.fixtures.week_lifecycle.runner import (  # noqa: E402
    FIXTURE_ROOT,
    MAX_DB_GROWTH_BYTES,
    MAX_RUNTIME_SEC,
    assert_not_live_db,
    run_week_lifecycle,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run DWCS-404 weekly lifecycle smoke")
    parser.add_argument(
        "--fixture",
        type=Path,
        default=FIXTURE_ROOT,
        help="Path to tests/fixtures/week_lifecycle",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help="Optional work directory (default: temp dir)",
    )
    args = parser.parse_args(argv)

    fixture_dir = args.fixture.resolve()
    if not (fixture_dir / "card.json").is_file():
        print(f"FAIL: fixture card.json missing under {fixture_dir}", file=sys.stderr)
        return 2

    work_dir = args.work_dir
    tmp: tempfile.TemporaryDirectory[str] | None = None
    if work_dir is None:
        tmp = tempfile.TemporaryDirectory(prefix="dwcs404-smoke-")
        work_dir = Path(tmp.name)
    else:
        work_dir = work_dir.resolve()
        work_dir.mkdir(parents=True, exist_ok=True)

    db_probe = work_dir / "lifecycle.db"
    try:
        assert_not_live_db(db_probe)
    except RuntimeError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2

    try:
        result = run_week_lifecycle(work_dir, fixture_dir=fixture_dir)
    except Exception as exc:  # noqa: BLE001 — smoke prints and exits non-zero
        print(f"FAIL: lifecycle raised {type(exc).__name__}: {exc}", file=sys.stderr)
        if tmp is not None:
            tmp.cleanup()
        return 1

    health_report = load_health_state(result.health_state_path)
    health_names = {c.name for c in health_report.components}
    # Lazy import avoids script-entry circular import through modeling.__init__.
    from mma_model.modeling.registry import load_model_registry

    registry_digest = load_model_registry(
        path=result.model_registry_path, enforce_pinned_digest=False
    ).champion.artifact_digest
    checks = [
        ("runtime_bounded", result.runtime_sec < MAX_RUNTIME_SEC),
        ("db_growth_bounded", 0 < result.db_bytes < MAX_DB_GROWTH_BYTES),
        (
            "champion_unchanged_after_retrain_fail",
            result.champion_digest_before
            == result.champion_digest_after
            == registry_digest,
        ),
        ("registry_reject_recorded", result.registry_reject_count >= 1),
        ("auth_non_retryable", result.auth_attempts == 1),
        ("schema_non_retryable", result.schema_attempts == 1),
        ("lkg_survived", result.lkg_release_id is not None),
        ("publish_advanced", result.final_release_id not in {None, result.lkg_release_id}),
        ("health_state_persisted", result.health_state_path.is_file()),
        ("health_components_complete", health_names == set(HEALTH_COMPONENT_NAMES)),
        (
            "health_five_statuses",
            {
                "healthy",
                "missing",
                "stale",
                "blocked",
                "failed",
            }.issubset(set(result.health_statuses.values())),
        ),
        ("event_night_void_recorded", bool(result.event_night_cv_settlement)),
        (
            "current_overturn_differs",
            result.current_cv_settlement.get("id")
            != result.event_night_cv_settlement.get("id")
            and result.current_cv_settlement.get("reason_code")
            != result.event_night_cv_settlement.get("reason_code"),
        ),
        ("price_target_snapshot", bool(result.price_target_snapshot.get("thresholds_hash"))),
        ("predictions_seeded", len(result.prediction_ids) >= 5),
        ("publications_present", len(result.publication_ids) >= 5),
    ]
    failed = [name for name, ok in checks if not ok]
    print("DWCS-404 weekly lifecycle smoke")
    print(f"  fixture: {fixture_dir}")
    print(f"  db: {result.db_path} ({result.db_bytes} bytes)")
    print(f"  runtime: {result.runtime_sec:.2f}s")
    print(f"  champion: {result.champion_digest_before} -> {result.champion_digest_after}")
    print(f"  publish: lkg={result.lkg_release_id} current={result.final_release_id}")
    for name, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")

    if tmp is not None:
        tmp.cleanup()

    if failed:
        print(f"FAIL: {len(failed)} check(s) failed: {', '.join(failed)}")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
