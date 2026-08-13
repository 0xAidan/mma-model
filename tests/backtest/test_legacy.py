"""Legacy fight-by-fight command is callable but not betting evidence."""

from __future__ import annotations

from mma_model.cli import main
from mma_model.predict.backtest import walk_forward_backtest
from mma_model.quality.constants import EXIT_STRICT_BLOCKERS


def test_legacy_walk_forward_returns_deprecation_record() -> None:
    out = walk_forward_backtest(None, min_train_fights=5, min_prior_fights=0)
    assert out["evidence"] is False
    assert out["unsafe_evaluator_ran"] is False
    assert out["same_card_leakage"] is False
    assert out["method"] == "disabled_unsafe_fight_by_fight"
    assert "fight-by-fight" in out["note"]


def test_legacy_cli_is_callable_and_fail_closed(capsys) -> None:
    code = main(["backtest", "--min-train", "30"])
    assert code == EXIT_STRICT_BLOCKERS
    captured = capsys.readouterr().out
    compact = captured.lower().replace(" ", "")
    assert "disabled_unsafe_fight_by_fight" in captured
    assert '"evidence":false' in compact
    assert "backtest run" in captured
