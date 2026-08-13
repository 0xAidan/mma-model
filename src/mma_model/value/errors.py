"""Typed errors for DWCS-204 value math."""

from __future__ import annotations


class OddsMathError(ValueError):
    """Base error for validated odds / probability / value math."""


class InvalidOddsError(OddsMathError):
    """Decimal or American odds failed validation."""


class InvalidProbabilityError(OddsMathError):
    """Probability is outside the allowed open/closed interval."""


class IncompleteMarketSetError(OddsMathError):
    """De-vig requires a complete outcome set; the supplied set is incomplete."""


class UnpricedMetricsError(OddsMathError):
    """EV / ROI / CLV / stake requested for an unpriced price-target row."""


class IneligiblePriceError(OddsMathError):
    """Observed price is present but failed product or quote-level eligibility."""
