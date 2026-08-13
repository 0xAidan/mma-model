"""Per-bet stake caps (DWCS-204). Portfolio selection beyond the cap is out of scope."""

from __future__ import annotations

from mma_model.value.kelly import (
    DEFAULT_BANKROLL_CAP_FRACTION,
    MAX_BANKROLL_CAP_FRACTION,
)
from mma_model.value.odds import validate_nonnegative_fraction


def capped_stake_fraction(
    raw_fraction: float,
    *,
    cap_fraction: float = DEFAULT_BANKROLL_CAP_FRACTION,
) -> float:
    """Clamp a stake fraction to ``[0, cap_fraction]`` with hard max 1%."""
    cap_fraction = validate_nonnegative_fraction(
        cap_fraction,
        field="cap_fraction",
        maximum=MAX_BANKROLL_CAP_FRACTION,
    )
    raw = validate_nonnegative_fraction(raw_fraction, field="raw_fraction")
    if raw <= 0.0:
        return 0.0
    return min(raw, cap_fraction)


def stake_amount(
    *,
    stake_fraction: float,
    bankroll: float,
    cap_fraction: float = DEFAULT_BANKROLL_CAP_FRACTION,
) -> float:
    """Absolute stake from bankroll; never exceeds the configured fraction cap."""
    bankroll = validate_nonnegative_fraction(bankroll, field="bankroll")
    fraction = capped_stake_fraction(stake_fraction, cap_fraction=cap_fraction)
    return fraction * bankroll
