"""Replay: frozen policy over protocol fixture and tamper-closed evidence."""

from __future__ import annotations

import json
from pathlib import Path

from mma_model.backtest.report import attach_content_hash
from mma_model.cli import main
from mma_model.domain.markets import RecommendationState
from mma_model.quality.constants import EXIT_INTERNAL, EXIT_OK
from mma_model.recommend.policy import PINNED_POLICY_HASH, NoBetReason, SelectionCandidate
from mma_model.recommend.replay import (
    RecommendReplayError,
    candidates_from_backtest_payload,
    execute_recommend_replay,
)
from tests.recommend.helpers import HASH_C, HASH_D, POLICY


def test_protocol_fixture_covers_required_cases() -> None:
    payload = execute_recommend_replay(fixture="protocol", policy=POLICY)
    assert payload["policy_hash"] == PINNED_POLICY_HASH
    assert payload["priced_policy"]["roi"] is None
    coverage = payload["unpriced_target_coverage"]
    assert coverage["unpriced_target_is_not_best_available_market"] is True
    confirmed = payload["confirmed_value"]
    watch = payload["price_target_watchlist"]
    no_bet = payload["no_bet"]
    bout_ids = {row["bout_id"] for row in confirmed + watch + no_bet}
    assert "bout-confirmed" in {row["bout_id"] for row in confirmed}
    assert "bout-exact-boundary" in {row["bout_id"] for row in confirmed}
    assert "bout-unpriced" in {row["bout_id"] for row in watch}
    assert "bout-stale" in {row["bout_id"] for row in no_bet}
    assert "bout-ambiguous" in {row["bout_id"] for row in no_bet}
    assert "bout-below" in {row["bout_id"] for row in no_bet}
    assert "bout-gates" in {row["bout_id"] for row in no_bet}
    tie = [row for row in confirmed + no_bet if row["bout_id"] == "bout-tie"]
    confirmed_ties = [
        row
        for row in tie
        if row["classification"] == RecommendationState.CONFIRMED_VALUE
    ]
    assert len(confirmed_ties) == 1
    assert any(
        NoBetReason.LOWER_RANKED_ELIGIBLE_SELECTION.value in row["reasons"] for row in tie
    )
    stale = next(row for row in no_bet if row["bout_id"] == "bout-stale")
    assert stale["classification"] == RecommendationState.NO_BET
    unpriced = next(row for row in watch if row["bout_id"] == "bout-unpriced")
    assert unpriced["median_ev"] is None
    assert unpriced["is_best_available_market"] is False
    assert "bout-void" in bout_ids
    first = execute_recommend_replay(fixture="protocol", policy=POLICY)
    second = execute_recommend_replay(fixture="protocol", policy=POLICY)
    assert first["content_hash"] == second["content_hash"]
    assert [row["selection_id"] for row in first["confirmed_value"]] == [
        row["selection_id"] for row in second["confirmed_value"]
    ]


def test_replay_source_evidence_tamper_fails(tmp_path: Path) -> None:
    honest = attach_content_hash(
        {"schema_version": "dwcs_backtest_evidence_v1.1", "attempts": []}
    )
    path = tmp_path / "backtest.json"
    path.write_text(json.dumps({**honest, "attempts": [{"tampered": True}]}), encoding="utf-8")
    try:
        execute_recommend_replay(backtest_json=path, policy=POLICY)
        raised = False
    except RecommendReplayError:
        raised = True
    assert raised


def test_replay_missing_confidence_is_honest_not_invented(tmp_path: Path) -> None:
    payload = attach_content_hash(
        {
            "schema_version": "dwcs_backtest_evidence_v1.1",
            "attempts": [
                {
                    "event_id": "dev-2017",
                    "bout_id": "2017-a",
                    "cutoff": "2017-07-11T18:00:00+00:00",
                    "prediction": {
                        "estimator_hash": "a" * 64,
                        "calibrator_hash": "b" * 64,
                        "p50": 0.55,
                        "p25": 0.50,
                        "markets": [
                            {
                                "family": "moneyline",
                                "outcome_key": "fighter_a",
                                "line_point": None,
                                "p50": 0.55,
                                "p25": 0.50,
                                "available": True,
                            }
                        ],
                    },
                    "priced_rows": [],
                    "threshold_only_rows": [],
                }
            ],
            "bootstrap": {"n_replicates": 12},
            "hashes": {
                "contract": POLICY.evaluation_contract_hash,
                "data": "c" * 64,
                "config": "d" * 64,
            },
        }
    )
    path = tmp_path / "backtest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    mapped = candidates_from_backtest_payload(payload, POLICY)
    for row in mapped:
        if isinstance(row, SelectionCandidate):
            assert row.production_uncertainty is False
        else:
            assert row.get("data_hash") not in {HASH_C, HASH_D}
            assert row.get("config_hash") not in {HASH_C, HASH_D}
    report = execute_recommend_replay(backtest_json=path, policy=POLICY)
    assert report["source_backtest_hash"] == payload["content_hash"]
    assert not report["confirmed_value"]
    assert not report["price_target_watchlist"]
    assert report["no_bet"]


def test_cli_protocol_replay(capsys) -> None:
    code = main(
        [
            "recommend",
            "replay",
            "--contract",
            "config/evaluation/dwcs_v1.json",
            "--fixture",
            "protocol",
        ]
    )
    assert code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["policy_hash"] == PINNED_POLICY_HASH
    assert payload["priced_policy"]["clv"] is None


def test_cli_rejects_protocol_and_backtest(tmp_path: Path, capsys) -> None:
    code = main(
        [
            "recommend",
            "replay",
            "--fixture",
            "protocol",
            "--backtest-json",
            str(tmp_path / "missing.json"),
        ]
    )
    assert code == EXIT_INTERNAL
    assert "not both" in capsys.readouterr().out
