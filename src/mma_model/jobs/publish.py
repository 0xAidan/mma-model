"""Versioned release publisher seam (DWCS-401 / DWCS-403).

Failed/partial publish must not replace the last-known-good ``current`` pointer.
Filesystem-backed LKG lives in ``observability.publish_guard``.
"""

from __future__ import annotations

from mma_model.jobs.handlers import handle_publish
from mma_model.observability.publish_guard import (
    FilesystemPublishPointer,
    PublishOutcome,
    PublishValidationError,
)

__all__ = [
    "FilesystemPublishPointer",
    "PublishOutcome",
    "PublishValidationError",
    "handle_publish",
]
