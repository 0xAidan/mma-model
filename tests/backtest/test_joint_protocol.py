"""Joint protocol: one calibrated atom distribution for all markets."""

from __future__ import annotations

from mma_model.backtest.engine import ProtocolWalkForwardScorer, run_walk_forward
from mma_model.backtest.walk_forward_scorer import JointProtocolWalkForwardScorer
from mma_model.domain.markets import MarketFamily
from mma_model.modeling.joint import EXPECTED_JOINT_MODEL_ID, joint_protocol_fixture_cards
from mma_model.modeling.splits import protocol_fixture_cards
from tests.backtest.helpers import CONTRACT


def test_joint_protocol_later_card_uses_one_calibrated_estimator() -> None:
    cards = joint_protocol_fixture_cards()
    scorer = JointProtocolWalkForwardScorer(
        CONTRACT, bootstrap_replicates=4, bootstrap_seed=306001
    )
    payload = run_walk_forward(
        contract=CONTRACT,
        cards=cards,
        scorer=scorer,
        sealed_holdout=True,
        require_target_cards=False,
        bootstrap_replicates=4,
        bootstrap_seed=306001,
    )
    joint_rows = [
        row
        for row in payload["attempts"]
        if row.get("prediction") is not None
        and row["prediction"]["model_id"] == EXPECTED_JOINT_MODEL_ID
        and row["prediction"].get("joint_atoms")
    ]
    assert joint_rows, "later joint-protocol cards must emit calibrated M2 atoms"
    by_event: dict[str, list[dict]] = {}
    for row in joint_rows:
        by_event.setdefault(row["event_id"], []).append(row)
    event_id, rows = next(iter(by_event.items()))
    hashes = {item["prediction"]["estimator_hash"] for item in rows}
    cal_hashes = {item["prediction"]["calibrator_hash"] for item in rows}
    assert len(hashes) == 1
    assert len(cal_hashes) == 1
    sample = rows[0]["prediction"]
    atoms = sample["joint_atoms"]
    assert abs(sum(atoms.values()) - 1.0) < 1e-8
    p_a = sum(value for key, value in atoms.items() if key.startswith("a_"))
    p_b = sum(value for key, value in atoms.items() if key.startswith("b_"))
    p_draw = float(atoms.get("draw", 0.0))
    assert abs(p_a + p_b + p_draw - 1.0) < 1e-8
    markets = {item["family"]: [] for item in sample["markets"] if item["available"]}
    for item in sample["markets"]:
        if item["available"]:
            markets.setdefault(item["family"], []).append(item)
    for family in (
        MarketFamily.MONEYLINE.value,
        MarketFamily.METHOD.value,
        MarketFamily.GOES_DISTANCE.value,
        MarketFamily.EXACT_ROUND.value,
    ):
        assert family in markets
        assert markets[family]
    ml_a = next(
        item
        for item in sample["markets"]
        if item["family"] == MarketFamily.MONEYLINE.value
        and item["outcome_key"] == "fighter_a"
    )
    ml_b = next(
        item
        for item in sample["markets"]
        if item["family"] == MarketFamily.MONEYLINE.value
        and item["outcome_key"] == "fighter_b"
    )
    assert abs(ml_a["p50"] - p_a) < 1e-8
    assert abs(ml_b["p50"] - p_b) < 1e-8
    assert ml_a["p25"] is not None
    assert ml_b["p25"] is not None
    totals = [
        item
        for item in sample["markets"]
        if item["family"] == MarketFamily.TOTALS.value and item["available"]
    ]
    assert totals
    p25s = [
        item["p25"]
        for item in sample["markets"]
        if item["available"] and item["p25"] is not None
    ]
    assert len(set(round(value, 8) for value in p25s)) > 1
    assert event_id


def test_m1_protocol_is_fallback_not_multi_market_proof() -> None:
    payload = run_walk_forward(
        contract=CONTRACT,
        cards=protocol_fixture_cards(),
        scorer=ProtocolWalkForwardScorer(CONTRACT, bootstrap_replicates=4),
        sealed_holdout=True,
        require_target_cards=False,
        bootstrap_replicates=4,
    )
    predicted = [row for row in payload["attempts"] if row.get("prediction") is not None]
    assert predicted
    m1 = [row for row in predicted if row["prediction"]["model_id"] == "M1"]
    assert m1
    for row in m1:
        unsupported = [
            item
            for item in row["prediction"]["markets"]
            if item["family"] != MarketFamily.MONEYLINE.value
        ]
        assert unsupported
        assert all(item["available"] is False for item in unsupported)
