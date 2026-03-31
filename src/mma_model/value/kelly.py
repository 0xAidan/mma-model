"""Fractional Kelly with caps."""

from __future__ import annotations


def kelly_fraction(model_prob: float, offered_american: float) -> float:
    """Return fraction of bankroll (may be negative)."""
    imp = _implied_from_american(offered_american)
    b = (1.0 / imp) - 1.0 if imp > 0 else 0.0
    q = 1.0 - model_prob
    return (model_prob * b - q) / b if b > 0 else 0.0


def _implied_from_american(a: float) -> float:
    if a > 0:
        return 100.0 / (a + 100.0)
    aa = abs(a)
    return aa / (aa + 100.0)


def fractional_kelly(
    model_prob: float,
    offered_american: float,
    fraction: float = 0.25,
    cap: float = 0.05,
) -> float:
    k = kelly_fraction(model_prob, offered_american) * fraction
    if k < 0:
        return 0.0
    return min(k, cap)
