"""Codegen freshness: Python ↔ JSON Schema ↔ TypeScript (DWCS-500)."""

from __future__ import annotations

from mma_model.publish.codegen import generated_artifacts_are_current, write_generated_artifacts


def test_generated_artifacts_are_committed_and_current() -> None:
    ok, problems = generated_artifacts_are_current()
    assert ok, "stale generated contracts: " + "; ".join(problems)


def test_regenerate_is_idempotent(tmp_path_factory) -> None:
    # Sanity: writer produces the same bytes as the check expectation.
    _ = tmp_path_factory
    write_generated_artifacts()
    ok, problems = generated_artifacts_are_current()
    assert ok, problems
