"""Atomic dashboard publisher used by jobs and CLI (DWCS-500)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from mma_model.observability.health import HealthReport
from mma_model.observability.publish_guard import (
    FilesystemPublishPointer,
    PublishOutcome,
    PublishValidationError,
)
from mma_model.publish.builder import build_release_files
from mma_model.publish.constants import DASHBOARD_RELEASE_FILES
from mma_model.publish.validator import validate_dashboard_release_dir


def publish_dashboard(
    session: Session,
    *,
    output_root: Path | str,
    release_id: str,
    event_id: str | None = None,
    window_slot: str | None = None,
    publications: int | None = None,
    as_of: datetime | None = None,
    health: HealthReport | None = None,
    files_override: Mapping[str, str | bytes] | None = None,
) -> PublishOutcome:
    """Build (or override) dashboard files and publish via LKG pointer."""
    pointer = FilesystemPublishPointer(output_root)
    if files_override is not None:
        files: Mapping[str, str | bytes] = files_override
    else:
        files = build_release_files(
            session,
            release_id=release_id,
            event_id=event_id,
            window_slot=window_slot,
            publications=publications,
            as_of=as_of,
            health=health,
        )
    return pointer.publish_release(
        release_id,
        files,
        required_files=DASHBOARD_RELEASE_FILES,
        validator=validate_dashboard_release_dir,
    )


def publish_dashboard_from_bodies(
    *,
    output_root: Path | str,
    release_id: str,
    files: Mapping[str, str | bytes],
) -> PublishOutcome:
    """Publish pre-built bodies with full dashboard validation."""
    pointer = FilesystemPublishPointer(output_root)
    return pointer.publish_release(
        release_id,
        files,
        required_files=DASHBOARD_RELEASE_FILES,
        validator=validate_dashboard_release_dir,
    )


def describe_publish_root(root: Path | str) -> dict[str, Any]:
    pointer = FilesystemPublishPointer(root)
    return pointer.as_dict()


__all__ = [
    "PublishOutcome",
    "PublishValidationError",
    "describe_publish_root",
    "publish_dashboard",
    "publish_dashboard_from_bodies",
]
