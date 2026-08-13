"""Manifest accounting: exactly 89 cards / 440 bouts with typed exclusions."""

from __future__ import annotations

from pathlib import Path

from mma_model.backtest.engine import execute_backtest_run
from mma_model.cli import main
from mma_model.evaluation.contract import load_evaluation_contract
from mma_model.quality.constants import EXIT_OK


def test_manifest_run_accounts_89_cards_440_bouts(tmp_path: Path) -> None:
    contract = load_evaluation_contract()
    payload = execute_backtest_run(
        contract_path=Path("config/evaluation/dwcs_v1.json"),
        output_dir=tmp_path,
        from_manifest=True,
        bootstrap_replicates=8,
    )
    universe = payload["universe"]
    assert universe["cards"] == 89
    assert universe["bouts"] == 440
    assert universe["standard_cards"] == 86
    assert universe["standard_bouts"] == 425
    assert universe["brazil_cards"] == 3
    assert universe["brazil_bouts"] == 15
    assert payload["n_attempts"] == 440
    attempts = payload["attempts"]
    assert len(attempts) == 440
    event_ids = {row["event_id"] for row in attempts}
    assert len(event_ids) == 89
    reasons = {
        row["exclusion_reason"]
        for row in attempts
        if row["exclusion_reason"] is not None
    }
    assert "missing_database" in reasons
    assert "locked_not_accessed" in reasons
    locked = [row for row in attempts if row["exclusion_reason"] == "locked_not_accessed"]
    missing = [row for row in attempts if row["exclusion_reason"] == "missing_database"]
    assert len(locked) == 51
    assert len(missing) == 389
    assert len(locked) + len(missing) == 440
    for row in attempts:
        assert row["status"] in {"predicted", "abstained", "unavailable", "excluded"}
        assert row["exclusion_reason"] is not None
    selection = payload["metrics"]["all_dwcs"]["selection"]
    assert selection["attempted"]["denominator"] == 440
    assert selection["attempted"]["numerator"] == 440
    assert selection["locked_not_accessed"]["numerator"] == 51
    assert payload["holdout"]["sealed_holdout"] is False
    assert payload["holdout"]["holdout_accessed"] is False
    assert payload["holdout"]["train_includes_2025"] is False
    assert contract.universe.all_dwcs.bouts == 440


def test_manifest_cli_from_manifest(tmp_path: Path, capsys) -> None:
    code = main(
        [
            "backtest",
            "run",
            "--contract",
            "config/evaluation/dwcs_v1.json",
            "--from-manifest",
            "--output",
            str(tmp_path),
            "--bootstrap-replicates",
            "8",
        ]
    )
    assert code == EXIT_OK
    captured = capsys.readouterr().out
    assert "440" in captured
    assert "89" in captured
