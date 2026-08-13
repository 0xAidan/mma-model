"""Filesystem-backed last-known-good publish pointer (DWCS-403).

Failed or partial publication must not replace the ``current`` release pointer.
In-memory ``PublishPointer`` remains for orchestrator unit tests; this module
is the production/filesystem seam for 404/500.
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

    ``current`` is a plain text pointer file (release id) updated via
    ``os.replace`` so validation failure never swaps the live pointer.
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

    def write_candidate(
        self,
        release_id: str,
        files: Mapping[str, str | bytes],
    ) -> Path:
        """Write files into a candidate release directory (does not promote)."""
        if not release_id or "/" in release_id or "\\" in release_id:
            raise PublishValidationError(f"invalid release_id: {release_id!r}")
        target = self.release_path(release_id)
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)
        for name, body in files.items():
            if not name or name.startswith("/") or ".." in Path(name).parts:
                raise PublishValidationError(f"invalid release file name: {name!r}")
            dest = target / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(body, bytes):
                dest.write_bytes(body)
            else:
                dest.write_text(str(body), encoding="utf-8")
        return target

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

    def promote(self, release_id: str) -> PublishOutcome:
        """Atomically point ``current`` at a validated release."""
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
        """Write → validate → promote. On validation failure, keep prior current."""
        prior = self.current_release_id
        candidate = self.write_candidate(release_id, files)
        try:
            self.validate_candidate(
                candidate,
                required_files=required_files,
                validator=validator,
            )
        except PublishValidationError:
            # Leave prior current intact; drop broken candidate.
            if candidate.exists():
                shutil.rmtree(candidate, ignore_errors=True)
            raise
        outcome = self.promote(release_id)
        if prior is not None and prior == outcome.current_release_id:
            return outcome
        return outcome

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
