"""Calibration slope/intercept, ECE, and reliability bins (DWCS-305).

These metrics grade already-produced probabilities. They do not settle bets
and they never read locked 2025 holdout labels on their own.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

import numpy as np
from scipy.optimize import minimize

PROBABILITY_EPS: Final = 1e-15
MIN_RELIABLE_BIN_COUNT: Final = 20
DEFAULT_ECE_BINS: Final = 10
MIN_SLOPE_SAMPLES: Final = 8
ATOM_SUM_ATOL: Final = 1e-10


class ReliabilityStatus(StrEnum):
    RELIABLE = "reliable"
    WEAK = "weak"
    EMPTY = "empty"


class SlopeStatus(StrEnum):
    FITTED = "fitted"
    ONE_CLASS = "one_class"
    TOO_SMALL = "too_small"
    NON_FINITE = "non_finite"


class MetricsError(ValueError):
    """Calibration metrics cannot be computed from the supplied scores."""


def stable_sigmoid(logit: float | np.ndarray) -> float | np.ndarray:
    """Numerically stable sigmoid. Accepts a scalar or array."""
    arr = np.asarray(logit, dtype=np.float64)
    if not np.all(np.isfinite(arr)):
        raise MetricsError("logit contains a non-finite value")
    out = np.empty_like(arr, dtype=np.float64)
    positive = arr >= 0.0
    out[positive] = 1.0 / (1.0 + np.exp(-arr[positive]))
    exp_x = np.exp(arr[~positive])
    out[~positive] = exp_x / (1.0 + exp_x)
    if np.ndim(logit) == 0:
        return float(out)
    return out


def stable_logit(probability: float | np.ndarray) -> float | np.ndarray:
    """Logit with probabilities clipped to ``(eps, 1-eps)``."""
    arr = np.asarray(probability, dtype=np.float64)
    if not np.all(np.isfinite(arr)):
        raise MetricsError("probability contains a non-finite value")
    if np.any(arr <= 0.0) or np.any(arr >= 1.0):
        raise MetricsError("raw probability must be in (0, 1)")
    clipped = np.clip(arr, PROBABILITY_EPS, 1.0 - PROBABILITY_EPS)
    out = np.log(clipped) - np.log(1.0 - clipped)
    if not np.all(np.isfinite(out)):
        raise MetricsError("logit is not finite")
    if np.ndim(probability) == 0:
        return float(out)
    return out


def _as_binary_arrays(
    y: Sequence[int],
    p: Sequence[float],
) -> tuple[np.ndarray, np.ndarray]:
    if len(y) != len(p):
        raise MetricsError("y and p must have the same length")
    y_arr = np.asarray(list(y), dtype=np.int64)
    p_arr = np.asarray(list(p), dtype=np.float64)
    if y_arr.size == 0:
        return y_arr, p_arr
    if not np.all((y_arr == 0) | (y_arr == 1)):
        raise MetricsError("y must be binary 0/1")
    if not np.all(np.isfinite(p_arr)):
        raise MetricsError("predicted probabilities must be finite")
    if np.any(p_arr <= 0.0) or np.any(p_arr >= 1.0):
        raise MetricsError("raw probability must be in (0, 1)")
    return y_arr, p_arr


def binary_nll(y: Sequence[int], p: Sequence[float]) -> float:
    y_arr, p_arr = _as_binary_arrays(y, p)
    if y_arr.size == 0:
        raise MetricsError("cannot compute NLL on an empty sample")
    clipped = np.clip(p_arr, PROBABILITY_EPS, 1.0 - PROBABILITY_EPS)
    nll = float(
        -np.mean(y_arr * np.log(clipped) + (1.0 - y_arr) * np.log(1.0 - clipped))
    )
    if not math.isfinite(nll):
        raise MetricsError("binary NLL is not finite")
    return nll


def binary_brier(y: Sequence[int], p: Sequence[float]) -> float:
    y_arr, p_arr = _as_binary_arrays(y, p)
    if y_arr.size == 0:
        raise MetricsError("cannot compute Brier on an empty sample")
    return float(np.mean((p_arr - y_arr) ** 2))


def fit_logistic_recalibration(
    logits: Sequence[float],
    y: Sequence[int],
) -> tuple[float, float]:
    """Fit ``P(y=1) = sigmoid(a * logit + b)``. Returns ``(a, b)``."""
    logit_arr = np.asarray(list(logits), dtype=np.float64)
    y_arr = np.asarray(list(y), dtype=np.int64)
    if logit_arr.size != y_arr.size:
        raise MetricsError("logits and y must have the same length")
    if logit_arr.size == 0:
        raise MetricsError("cannot fit logistic recalibration on an empty sample")
    if not np.all(np.isfinite(logit_arr)):
        raise MetricsError("logits must be finite")
    if not np.all((y_arr == 0) | (y_arr == 1)):
        raise MetricsError("y must be binary 0/1")

    def nll(params: np.ndarray) -> float:
        a = float(params[0])
        b = float(params[1])
        z = a * logit_arr + b
        p = np.asarray(stable_sigmoid(z), dtype=np.float64)
        p = np.clip(p, PROBABILITY_EPS, 1.0 - PROBABILITY_EPS)
        return float(-np.mean(y_arr * np.log(p) + (1.0 - y_arr) * np.log(1.0 - p)))

    result = minimize(
        nll,
        x0=np.array([1.0, 0.0], dtype=np.float64),
        method="L-BFGS-B",
    )
    if not bool(result.success):
        raise MetricsError(f"logistic recalibration failed: {result.message}")
    a = float(result.x[0])
    b = float(result.x[1])
    if not math.isfinite(a) or not math.isfinite(b):
        raise MetricsError("logistic recalibration produced a non-finite parameter")
    return a, b


@dataclass(frozen=True)
class CalibrationSlopeIntercept:
    slope: float | None
    intercept: float | None
    status: SlopeStatus
    n: int
    n_positive: int
    n_negative: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "intercept": self.intercept,
            "n": self.n,
            "n_negative": self.n_negative,
            "n_positive": self.n_positive,
            "slope": self.slope,
            "status": self.status.value,
        }


def calibration_slope_intercept(
    y: Sequence[int],
    p: Sequence[float],
    *,
    min_samples: int = MIN_SLOPE_SAMPLES,
) -> CalibrationSlopeIntercept:
    """Logistic recalibration of the outcome on ``logit(p)``."""
    y_arr, p_arr = _as_binary_arrays(y, p)
    n = int(y_arr.size)
    n_positive = int(np.sum(y_arr == 1))
    n_negative = int(np.sum(y_arr == 0))
    if n < min_samples:
        return CalibrationSlopeIntercept(
            slope=None,
            intercept=None,
            status=SlopeStatus.TOO_SMALL,
            n=n,
            n_positive=n_positive,
            n_negative=n_negative,
        )
    if n_positive == 0 or n_negative == 0:
        return CalibrationSlopeIntercept(
            slope=None,
            intercept=None,
            status=SlopeStatus.ONE_CLASS,
            n=n,
            n_positive=n_positive,
            n_negative=n_negative,
        )
    try:
        slope, intercept = fit_logistic_recalibration(stable_logit(p_arr), y_arr)
    except MetricsError:
        return CalibrationSlopeIntercept(
            slope=None,
            intercept=None,
            status=SlopeStatus.NON_FINITE,
            n=n,
            n_positive=n_positive,
            n_negative=n_negative,
        )
    return CalibrationSlopeIntercept(
        slope=slope,
        intercept=intercept,
        status=SlopeStatus.FITTED,
        n=n,
        n_positive=n_positive,
        n_negative=n_negative,
    )


@dataclass(frozen=True)
class ReliabilityRow:
    lower: float
    upper: float
    mean_predicted: float | None
    observed_frequency: float | None
    count: int
    status: ReliabilityStatus

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "lower": self.lower,
            "mean_predicted": self.mean_predicted,
            "observed_frequency": self.observed_frequency,
            "status": self.status.value,
            "upper": self.upper,
        }


@dataclass(frozen=True)
class EceReport:
    ece: float | None
    n_bins: int
    min_reliable_bin_count: int
    n_total: int
    n_reliable: int
    n_weak: int
    n_ece_used: int
    n_suppressed_display: int
    n_events: int
    n_empty_bins: int
    n_reliable_bins: int
    n_weak_bins: int
    rows: tuple[ReliabilityRow, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ece": self.ece,
            "min_reliable_bin_count": self.min_reliable_bin_count,
            "n_bins": self.n_bins,
            "n_ece_used": self.n_ece_used,
            "n_empty_bins": self.n_empty_bins,
            "n_events": self.n_events,
            "n_reliable": self.n_reliable,
            "n_reliable_bins": self.n_reliable_bins,
            "n_suppressed_display": self.n_suppressed_display,
            "n_total": self.n_total,
            "n_weak": self.n_weak,
            "n_weak_bins": self.n_weak_bins,
            "rows": [row.to_dict() for row in self.rows],
        }


def expected_calibration_error(
    y: Sequence[int],
    p: Sequence[float],
    *,
    event_ids: Sequence[str] | None = None,
    n_bins: int = DEFAULT_ECE_BINS,
    min_reliable_bin_count: int = MIN_RELIABLE_BIN_COUNT,
    omit_empty: bool = True,
) -> EceReport:
    """Equal-width ECE over all samples in nonempty bins.

    Weak bins stay ``status=weak`` and omit ``observed_frequency`` from the
    public row, but their ``n_b * |obs - pred|`` term still enters ECE.
    ECE is ``None`` only for an empty sample, never because every bin is weak.
    """
    if n_bins < 1:
        raise MetricsError("n_bins must be >= 1")
    if min_reliable_bin_count < 1:
        raise MetricsError("min_reliable_bin_count must be >= 1")
    y_arr, p_arr = _as_binary_arrays(y, p)
    n_total = int(y_arr.size)
    if event_ids is None:
        n_events = n_total
    else:
        if len(event_ids) != n_total:
            raise MetricsError("event_ids must match the prediction count")
        n_events = len(set(event_ids))
    if n_total == 0:
        return EceReport(
            ece=None,
            n_bins=n_bins,
            min_reliable_bin_count=min_reliable_bin_count,
            n_total=0,
            n_reliable=0,
            n_weak=0,
            n_ece_used=0,
            n_suppressed_display=0,
            n_events=n_events,
            n_empty_bins=n_bins if omit_empty else n_bins,
            n_reliable_bins=0,
            n_weak_bins=0,
            rows=(),
        )
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    rows: list[ReliabilityRow] = []
    n_empty = 0
    n_weak_bins = 0
    n_reliable_bins = 0
    n_reliable = 0
    n_weak = 0
    weighted = 0.0
    for idx in range(n_bins):
        lower = float(edges[idx])
        upper = float(edges[idx + 1])
        if idx == n_bins - 1:
            mask = (p_arr >= lower) & (p_arr <= upper)
        else:
            mask = (p_arr >= lower) & (p_arr < upper)
        count = int(np.sum(mask))
        if count == 0:
            n_empty += 1
            if not omit_empty:
                rows.append(
                    ReliabilityRow(
                        lower=lower,
                        upper=upper,
                        mean_predicted=None,
                        observed_frequency=None,
                        count=0,
                        status=ReliabilityStatus.EMPTY,
                    )
                )
            continue
        mean_predicted = float(np.mean(p_arr[mask]))
        observed = float(np.mean(y_arr[mask]))
        weighted += count * abs(observed - mean_predicted)
        if count < min_reliable_bin_count:
            n_weak_bins += 1
            n_weak += count
            rows.append(
                ReliabilityRow(
                    lower=lower,
                    upper=upper,
                    mean_predicted=mean_predicted,
                    observed_frequency=None,
                    count=count,
                    status=ReliabilityStatus.WEAK,
                )
            )
            continue
        n_reliable_bins += 1
        n_reliable += count
        rows.append(
            ReliabilityRow(
                lower=lower,
                upper=upper,
                mean_predicted=mean_predicted,
                observed_frequency=observed,
                count=count,
                status=ReliabilityStatus.RELIABLE,
            )
        )
    if n_reliable + n_weak != n_total:
        raise MetricsError(
            "reliability counts do not reconcile: "
            f"n_total={n_total} n_reliable={n_reliable} n_weak={n_weak}"
        )
    ece = float(weighted / n_total)
    if not math.isfinite(ece):
        raise MetricsError("ECE is not finite")
    return EceReport(
        ece=ece,
        n_bins=n_bins,
        min_reliable_bin_count=min_reliable_bin_count,
        n_total=n_total,
        n_reliable=n_reliable,
        n_weak=n_weak,
        n_ece_used=n_total,
        n_suppressed_display=n_weak,
        n_events=n_events,
        n_empty_bins=n_empty,
        n_reliable_bins=n_reliable_bins,
        n_weak_bins=n_weak_bins,
        rows=tuple(rows),
    )


@dataclass(frozen=True)
class BinaryCalibrationReport:
    n_total: int
    n_reliable: int
    n_weak: int
    n_ece_used: int
    n_suppressed_display: int
    n_events: int
    log_loss: float | None
    brier: float | None
    slope: CalibrationSlopeIntercept
    ece: EceReport

    def to_dict(self) -> dict[str, Any]:
        return {
            "brier": self.brier,
            "ece": self.ece.to_dict(),
            "log_loss": self.log_loss,
            "n_ece_used": self.n_ece_used,
            "n_events": self.n_events,
            "n_reliable": self.n_reliable,
            "n_suppressed_display": self.n_suppressed_display,
            "n_total": self.n_total,
            "n_weak": self.n_weak,
            "slope": self.slope.to_dict(),
        }


def binary_calibration_report(
    y: Sequence[int],
    p: Sequence[float],
    *,
    event_ids: Sequence[str] | None = None,
    min_reliable_bin_count: int = MIN_RELIABLE_BIN_COUNT,
    n_bins: int = DEFAULT_ECE_BINS,
) -> BinaryCalibrationReport:
    y_arr, p_arr = _as_binary_arrays(y, p)
    ece = expected_calibration_error(
        y_arr,
        p_arr,
        event_ids=event_ids,
        n_bins=n_bins,
        min_reliable_bin_count=min_reliable_bin_count,
    )
    slope = calibration_slope_intercept(y_arr, p_arr)
    log_loss = None if y_arr.size == 0 else binary_nll(y_arr, p_arr)
    brier = None if y_arr.size == 0 else binary_brier(y_arr, p_arr)
    return BinaryCalibrationReport(
        n_total=ece.n_total,
        n_reliable=ece.n_reliable,
        n_weak=ece.n_weak,
        n_ece_used=ece.n_ece_used,
        n_suppressed_display=ece.n_suppressed_display,
        n_events=ece.n_events,
        log_loss=log_loss,
        brier=brier,
        slope=slope,
        ece=ece,
    )


@dataclass(frozen=True)
class JointCalibrationReport:
    n_total: int
    n_ece_used: int
    n_suppressed_display: int
    n_events: int
    terminal_nll: float | None
    n_draw: int
    n_no_contest: int
    n_decisive: int
    moneyline: BinaryCalibrationReport | None
    draw_nc_note: str
    moneyline_probability: str = "conditional_pA_given_decisive"

    def to_dict(self) -> dict[str, Any]:
        return {
            "draw_nc_note": self.draw_nc_note,
            "moneyline": None if self.moneyline is None else self.moneyline.to_dict(),
            "moneyline_probability": self.moneyline_probability,
            "n_decisive": self.n_decisive,
            "n_draw": self.n_draw,
            "n_ece_used": self.n_ece_used,
            "n_events": self.n_events,
            "n_no_contest": self.n_no_contest,
            "n_suppressed_display": self.n_suppressed_display,
            "n_total": self.n_total,
            "terminal_nll": self.terminal_nll,
        }


def joint_terminal_nll(
    probabilities: Sequence[Mapping[str, float]],
    observed_atoms: Sequence[str],
) -> float:
    if len(probabilities) != len(observed_atoms):
        raise MetricsError("joint probabilities and observed atoms must align")
    if not probabilities:
        raise MetricsError("cannot compute terminal NLL on an empty sample")
    total = 0.0
    for dist, atom in zip(probabilities, observed_atoms, strict=True):
        mass = float(sum(float(value) for value in dist.values()))
        if not math.isfinite(mass) or abs(mass - 1.0) > ATOM_SUM_ATOL:
            raise MetricsError(
                f"joint distribution sums to {mass}, expected 1 ± {ATOM_SUM_ATOL}"
            )
        if atom not in dist:
            raise MetricsError(f"observed atom {atom!r} is missing from the joint distribution")
        p_obs = float(dist[atom])
        if not math.isfinite(p_obs) or p_obs <= 0.0:
            raise MetricsError("observed atom probability must be finite and > 0")
        total += -math.log(max(p_obs, PROBABILITY_EPS))
    nll = total / len(probabilities)
    if not math.isfinite(nll):
        raise MetricsError("terminal NLL is not finite")
    return nll


def _moneyline_side(atom: str) -> str | None:
    if atom == "draw" or atom == "no_contest" or atom == "nc":
        return None
    if atom.startswith("a_"):
        return "a"
    if atom.startswith("b_"):
        return "b"
    return None


def _side_mass(dist: Mapping[str, float], prefix: str) -> float:
    return float(sum(float(value) for key, value in dist.items() if str(key).startswith(prefix)))


def conditional_fighter_a_given_decisive(dist: Mapping[str, float]) -> float:
    """Bernoulli probability of A among decisive outcomes: ``pA / (pA + pB)``."""
    p_a = _side_mass(dist, "a_")
    p_b = _side_mass(dist, "b_")
    denom = p_a + p_b
    if not math.isfinite(denom) or denom <= 0.0:
        raise MetricsError("decisive moneyline requires finite pA+pB > 0")
    p_cond = p_a / denom
    if not math.isfinite(p_cond) or p_cond <= 0.0 or p_cond >= 1.0:
        raise MetricsError("conditional pA_decisive must be in (0, 1)")
    return p_cond


def joint_calibration_report(
    probabilities: Sequence[Mapping[str, float]],
    observed_atoms: Sequence[str],
    *,
    event_ids: Sequence[str] | None = None,
    min_reliable_bin_count: int = MIN_RELIABLE_BIN_COUNT,
) -> JointCalibrationReport:
    """Terminal NLL on the full fine distribution plus decisive A-vs-B metrics.

    Draws are excluded from the Bernoulli outcome and from ``pA+pB``. Slope and
    ECE use ``pA / (pA + pB)``, never unconditional ``pA``.
    """
    if len(probabilities) != len(observed_atoms):
        raise MetricsError("joint probabilities and observed atoms must align")
    n_total = len(observed_atoms)
    n_draw = 0
    n_nc = 0
    decisive_y: list[int] = []
    decisive_p: list[float] = []
    decisive_events: list[str] = []
    for idx, atom in enumerate(observed_atoms):
        lowered = atom.lower()
        if lowered in {"draw"}:
            n_draw += 1
            continue
        if lowered in {"no_contest", "nc", "void"}:
            n_nc += 1
            continue
        side = _moneyline_side(atom)
        if side is None:
            raise MetricsError(f"unhandled observed atom {atom!r}")
        p_cond = conditional_fighter_a_given_decisive(probabilities[idx])
        decisive_y.append(1 if side == "a" else 0)
        decisive_p.append(p_cond)
        if event_ids is not None:
            decisive_events.append(event_ids[idx])
    n_decisive = len(decisive_y)
    nll = None if n_total == 0 else joint_terminal_nll(probabilities, observed_atoms)
    moneyline = None
    if n_decisive:
        moneyline = binary_calibration_report(
            decisive_y,
            decisive_p,
            event_ids=None if event_ids is None else decisive_events,
            min_reliable_bin_count=min_reliable_bin_count,
        )
    n_events = n_total if event_ids is None else len(set(event_ids))
    n_ece_used = 0 if moneyline is None else moneyline.n_ece_used
    n_suppressed_display = 0 if moneyline is None else moneyline.n_suppressed_display
    return JointCalibrationReport(
        n_total=n_total,
        n_ece_used=n_ece_used,
        n_suppressed_display=n_suppressed_display,
        n_events=n_events,
        terminal_nll=nll,
        n_draw=n_draw,
        n_no_contest=n_nc,
        n_decisive=n_decisive,
        moneyline=moneyline,
        draw_nc_note=(
            "Draws are excluded from the decisive Bernoulli outcome and from "
            "pA+pB; no-contest/void rows are counted separately and never "
            "scored as wins. Slope/ECE use pA/(pA+pB)."
        ),
    )
