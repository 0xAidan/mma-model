"""Versioned release publisher seam (DWCS-401).

Failed/partial publish must not replace the last-known-good ``current`` pointer.
"""

from __future__ import annotations

from mma_model.jobs.handlers import handle_publish

__all__ = ["handle_publish"]
