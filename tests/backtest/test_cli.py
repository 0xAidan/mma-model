"""CLI: protocol fixture, sealed holdout, read-only DB, live DB refusal."""

from __future__ import annotations

import json
from pathlib import Path

from mma_model.cli import main
from mma_model.quality.constants import EXIT_INTERNAL, EXIT_OK


def test_protocol_cli_runs_real_pipeline(tmp_path: Path, capsys) -> None:
    code = main(
        [
            "backtest",
            "run",
            "--contract",
            "config/evaluation/dwcs_v1.json",
            "--fixture",
            "protocol",
            "--output",
            str(tmp_path),
            "--bootstrap-replicates",
            "12",
            "--generated-at",
            "2026-08-13T16:00:00+00:00",
        ]
    )
    assert code == EXIT_OK
    text = capsys.readouterr().out
    payload = json.loads(text)
    assert payload["evidence"] is True
    assert payload["universe"]["cards"] == 5
    json_path = Path(payload["output"]["json_path"])
    saved = json.loads(json_path.read_text(encoding="utf-8"))
    pair = [
        row
        for row in saved["attempts"]
        if row["bout_id"] in {"2017-a", "2017-b"} and row["status"] == "predicted"
    ]
    assert len(pair) == 2
    assert pair[0]["prediction"]["estimator_hash"] == pair[1]["prediction"]["estimator_hash"]
    hold = [row for row in saved["attempts"] if row["season"] == 2025]
    assert hold
    assert all(row["exclusion_reason"] == "locked_not_accessed" for row in hold)


def test_cli_refuses_live_db(capsys) -> None:
    code = main(
        [
            "backtest",
            "run",
            "--contract",
            "config/evaluation/dwcs_v1.json",
            "--database-url",
            "sqlite:///data/mma.db",
            "--output",
            "output/backtests",
        ]
    )
    assert code == EXIT_INTERNAL
    captured = capsys.readouterr().out.lower()
    assert "mma.db" in captured or "refusing" in captured or "live" in captured


def test_cli_rejects_protocol_and_from_manifest(capsys) -> None:
    code = main(
        [
            "backtest",
            "run",
            "--fixture",
            "protocol",
            "--from-manifest",
            "--output",
            "output/backtests",
        ]
    )
    assert code == EXIT_INTERNAL
    assert "not both" in capsys.readouterr().out
