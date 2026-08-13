"""Per-bet stake caps (DWCS-204). Portfolio selection beyond the cap is out of scope."""

from __future__ import annotations

from mma_model.value.kelly import DEFAULT_BANKROLL_CAP_FRACTION


def capped_stake_fraction(
    raw_fraction: float,
    *,
    cap_fraction: float = DEFAULT_BANKROLL_CAP_FRACTION,
) -> float:
    """Clamp a stake fraction to ``[0, cap_fraction]``."""
    if cap_fraction < 0.0:
        raise ValueError("cap_fraction must be non-negative")
    if raw_fraction <= 0.0:
        return 0.0
    return min(raw_fraction, cap_fraction)


def stake_amount(
    *,
    stake_fraction: float,
    bankroll: float,
    cap_fraction: float = DEFAULT_BANKROLL_CAP_FRACTION,
) -> float:
    """Absolute stake from bankroll; never exceeds the configured fraction cap."""
    if bankroll < 0.0:
        raise ValueError("bankroll must be non-negative")
    fraction = capped_stake_fraction(stake_fraction, cap_fraction=cap_fraction)
    return fraction * bankroll
