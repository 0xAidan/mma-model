"""DWCS manifest-first history ingest (DWCS-103)."""

from __future__ import annotations

__all__ = [
    "BoutCategory",
    "BoutClassification",
    "DWCS_UUID_NAMESPACE",
    "DurationStatus",
    "DwcsBoutManifestRow",
    "DwcsEventManifestRow",
    "ManifestValidationError",
    "SyncHistoryReport",
    "canonical_bout_id",
    "canonical_event_id",
    "canonical_fighter_id",
    "classify_bout",
    "classify_event_cancellation",
    "derive_elapsed_seconds",
    "load_dwcs_bout_manifest",
    "load_dwcs_event_manifest",
    "load_dwcs_mismatch_ledger",
    "sync_dwcs_history",
    "validate_expected_universe",
]


def __getattr__(name: str):
    if name in {
        "BoutCategory",
        "BoutClassification",
        "classify_bout",
        "classify_event_cancellation",
    }:
        from mma_model.dwcs import classification as mod

        return getattr(mod, name)
    if name in {"DurationStatus", "derive_elapsed_seconds"}:
        from mma_model.dwcs import duration as mod

        return getattr(mod, name)
    if name in {
        "DWCS_UUID_NAMESPACE",
        "canonical_bout_id",
        "canonical_event_id",
        "canonical_fighter_id",
    }:
        from mma_model.dwcs import ids as mod

        return getattr(mod, name)
    if name in {
        "DwcsBoutManifestRow",
        "DwcsEventManifestRow",
        "ManifestValidationError",
        "load_dwcs_bout_manifest",
        "load_dwcs_event_manifest",
        "load_dwcs_mismatch_ledger",
        "validate_expected_universe",
    }:
        from mma_model.dwcs import manifest as mod

        return getattr(mod, name)
    if name in {"SyncHistoryReport", "sync_dwcs_history"}:
        from mma_model.dwcs import ingest as mod

        return getattr(mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
