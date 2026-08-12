"""Shared polite HTTP helpers for public source adapters."""

from mma_model.sources.http.block_signals import SourceBlockedError, detect_block_signal

__all__ = [
    "SourceBlockedError",
    "detect_block_signal",
]
