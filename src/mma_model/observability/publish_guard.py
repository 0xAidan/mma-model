"""Filesystem-backed last-known-good publish pointer (DWCS-403).

Failed or partial publication must not replace the ``current`` release pointer
or destroy the files it points at. Unvalidated payloads are written only to a
staging directory; promotion happens after validation succeeds.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class PublishValidationError(ValueError):
    """Candidate release failed validation; current pointer unchanged."""


@dataclass(frozen=True)
class PublishOutcome:
    release_id: str
    current_release_id: str
    replaced: bool
    path: Path
    detail: str = ""


class FilesystemPublishPointer:
    """Versioned releases under ``root/releases/<id>/`` with atomic ``current``.

    Candidates land in ``releases/<id>.candidate`` (or ``.staging/<id>`` fallback
    naming). Validation never mutates ``releases/<id>`` or ``current``. On
    success the staging dir is promoted into ``releases/<id>``, then ``current``
    is updated via ``os.replace``.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.releases_dir = self.root / "releases"
        self.current_path = self.root / "current"
        self.releases_dir.mkdir(parents=True, exist_ok=True)

    @property
    def current_release_id(self) -> str | None:
        if not self.current_path.is_file():
            return None
        text = self.current_path.read_text(encoding="utf-8").strip()
        return text or None

    def release_path(self, release_id: str) -> Path:
        return self.releases_dir / release_id

    def staging_path(self, release_id: str) -> Path:
        """Unvalidated payload directory (never the live release path)."""
        return self.releases_dir / f"{release_id}.candidate"

    def _assert_safe_release_id(self, release_id: str) -> None:
        if (
            not release_id
            or "/" in release_id
            or "\\" in release_id
            or release_id.endswith(".candidate")
            or release_id.endswith(".old")
            or ".." in release_id
        ):
            raise PublishValidationError(f"invalid release_id: {release_id!r}")

    def write_candidate(
        self,
        release_id: str,
        files: Mapping[str, str | bytes],
    ) -> Path:
        """Write files into a staging directory (does not touch live release)."""
        self._assert_safe_release_id(release_id)
        staging = self.staging_path(release_id)
        # Never rmtree the live release path — only (re)create staging.
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True, exist_ok=True)
        for name, body in files.items():
            if not name or name.startswith("/") or ".." in Path(name).parts:
                raise PublishValidationError(f"invalid release file name: {name!r}")
            dest = staging / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(body, bytes):
                dest.write_bytes(body)
            else:
                dest.write_text(str(body), encoding="utf-8")
        return staging

    def validate_candidate(
        self,
        release_dir: Path,
        *,
        required_files: Sequence[str] = ("release.json",),
        validator: Callable[[Path], None] | None = None,
    ) -> None:
        missing = [name for name in required_files if not (release_dir / name).is_file()]
        if missing:
            raise PublishValidationError(
                f"candidate missing required files: {','.join(missing)}"
            )
        for name in required_files:
            path = release_dir / name
            if path.suffix == ".json":
                try:
                    json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError as exc:
                    raise PublishValidationError(
                        f"invalid JSON in {name}: {exc}"
                    ) from exc
        if validator is not None:
            validator(release_dir)

    def _promote_staging(self, release_id: str, staging: Path) -> Path:
        """Move validated staging into ``releases/<id>`` without losing LKG.

        If a live release already exists (including the current LKG), it is
        renamed aside only after staging is validated, then restored if the
        staging→final rename fails. Current LKG files are never deleted first.
        """
        final = self.release_path(release_id)
        backup: Path | None = None
        if final.exists():
            backup = self.releases_dir / f"{release_id}.old"
            if backup.exists():
                shutil.rmtree(backup)
            os.replace(final, backup)
        try:
            os.replace(staging, final)
        except OSError:
            if backup is not None and backup.exists() and not final.exists():
                os.replace(backup, final)
            raise
        if backup is not None and backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
        return final

    def promote(self, release_id: str) -> PublishOutcome:
        """Atomically point ``current`` at an already-present release directory."""
        self._assert_safe_release_id(release_id)
        release_dir = self.release_path(release_id)
        if not release_dir.is_dir():
            raise PublishValidationError(f"release directory missing: {release_id}")
        tmp = self.root / f".current.{os.getpid()}.tmp"
        tmp.write_text(release_id + "\n", encoding="utf-8")
        os.replace(tmp, self.current_path)
        return PublishOutcome(
            release_id=release_id,
            current_release_id=release_id,
            replaced=True,
            path=release_dir,
            detail="current pointer updated",
        )

    def publish_release(
        self,
        release_id: str,
        files: Mapping[str, str | bytes],
        *,
        required_files: Sequence[str] = ("release.json",),
        validator: Callable[[Path], None] | None = None,
    ) -> PublishOutcome:
        """Stage → validate → promote. Validation failure keeps prior LKG files."""
        staging = self.write_candidate(release_id, files)
        try:
            self.validate_candidate(
                staging,
                required_files=required_files,
                validator=validator,
            )
        except PublishValidationError:
            # Delete only staging. Live release + current pointer stay intact.
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            raise
        self._promote_staging(release_id, staging)
        return self.promote(release_id)

    def as_dict(self) -> dict[str, Any]:
        return {
            "current_release_id": self.current_release_id,
            "root": str(self.root),
        }


__all__ = [
    "FilesystemPublishPointer",
    "PublishOutcome",
    "PublishValidationError",
]
