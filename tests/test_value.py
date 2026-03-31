from mma_model.value.ev import american_to_implied_prob, ev_vs_fair
from mma_model.value.kelly import fractional_kelly


def test_american_implied():
    assert abs(american_to_implied_prob(-150) - 0.6) < 1e-6
    assert abs(american_to_implied_prob(150) - 0.4) < 1e-6


def test_ev_positive_edge():
    ev = ev_vs_fair(0.55, -110)
    assert ev > 0


def test_fractional_kelly_non_negative():
    k = fractional_kelly(0.55, -110, fraction=0.25, cap=0.05)
    assert k >= 0
    assert k <= 0.05
