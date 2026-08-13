"""Cutoff-aware point-in-time matchup features (DWCS-301).

Legacy ``matchup.py`` remains for old train/predict paths and is not the
production PIT builder.
"""

from mma_model.features.as_of import (
    AsOfCutoff,
    CutoffKind,
    CutoffMismatchError,
    FeatureCutoffError,
    cutoff_for_event,
    observation_admitted,
)
from mma_model.features.builder import FeatureBuilder, FeatureRow, FeatureRowCache
from mma_model.features.spec import FEATURE_NAMES, SPEC_VERSION, spec_hash, swap_values
from mma_model.features.strength import FighterStrength, strengths_before_event

__all__ = [
    "FEATURE_NAMES",
    "SPEC_VERSION",
    "AsOfCutoff",
    "CutoffKind",
    "CutoffMismatchError",
    "FeatureBuilder",
    "FeatureCutoffError",
    "FeatureRow",
    "FeatureRowCache",
    "FighterStrength",
    "cutoff_for_event",
    "observation_admitted",
    "spec_hash",
    "strengths_before_event",
    "swap_values",
]
