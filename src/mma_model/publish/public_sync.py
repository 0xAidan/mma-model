"""Public static root coexistence helpers (DWCS-502).

The host Caddy ``file_server`` root (``/srv/mma/public`` → container ``/public``)
must hold:

- Web dashboard assets (``index.html`` + hashed ``assets/``)
- Dashboard JSON under ``live/`` (atomic directory swap for LKG)
- ``releases/<id>/`` plus the last-known-good ``current`` pointer

Failed sync/promote must leave last-known-good ``live/`` and previous assets
in place. Versioned ``releases/`` + ``current`` may advance independently of
``live/`` (``live/`` is the SPA-facing rollback surface).
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from mma_model.publish.constants import DASHBOARD_RELEASE_FILES

LIVE_DIR_NAME: str = "live"
LIVE_CANDIDATE_DIR_NAME: str = "live.candidate"

# Top-level names that must never be deleted by web-asset sync.
PROTECTED_TOP_LEVEL: frozenset[str] = frozenset(
    {
        "releases",
        "current",
        LIVE_DIR_NAME,
        LIVE_CANDIDATE_DIR_NAME,
        *DASHBOARD_RELEASE_FILES,
    }
)

# Vite copies web/public/*.json into dist; never promote those fixtures over LKG.
SKIP_WEB_SYNC_NAMES: frozenset[str] = frozenset(
    {
        *DASHBOARD_RELEASE_FILES,
        "releases",
        "current",
        LIVE_DIR_NAME,
        LIVE_CANDIDATE_DIR_NAME,
    }
)


class PublicSyncError(RuntimeError):
    """Public root sync or JSON promote failed; LKG left intact."""


@dataclass(frozen=True)
class SyncAssetsResult:
    copied: tuple[str, ...]
    skipped: tuple[str, ...]
    public_root: Path


@dataclass(frozen=True)
class PromoteJsonResult:
    files: tuple[str, ...]
    release_id: str | None
    public_root: Path
    live_dir: Path


def _fsync_file(path: Path) -> None:
    with open(path, "rb+") as handle:
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        return
    finally:
        os.close(fd)


def _atomic_replace_file(src: Path, dest: Path) -> None:
    """Replace ``dest`` with ``src`` via temp sibling + ``os.replace``."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / f".{dest.name}.{os.getpid()}.tmp"
    if tmp.exists():
        if tmp.is_dir():
            shutil.rmtree(tmp)
        else:
            tmp.unlink()
    shutil.copy2(src, tmp)
    _fsync_file(tmp)
    os.replace(tmp, dest)
    _fsync_dir(dest.parent)


def _copy_tree_atomic(src_dir: Path, dest_dir: Path) -> None:
    """Replace a directory tree without deleting sibling protected paths."""
    parent = dest_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging = parent / f".{dest_dir.name}.{os.getpid()}.stage"
    backup = parent / f".{dest_dir.name}.{os.getpid()}.old"
    if staging.exists():
        shutil.rmtree(staging)
    if backup.exists():
        shutil.rmtree(backup)
    shutil.copytree(src_dir, staging)
    _fsync_dir(staging)
    try:
        if dest_dir.exists():
            os.replace(dest_dir, backup)
        os.replace(staging, dest_dir)
    except OSError:
        if backup.exists() and not dest_dir.exists():
            os.replace(backup, dest_dir)
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise
    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)
    _fsync_dir(parent)


def sync_web_assets(web_dist: Path | str, public_root: Path | str) -> SyncAssetsResult:
    """Copy production web build into ``public_root`` without wiping LKG.

    Does not delete ``releases/``, ``current``, or ``live/``.
    Skips fixture JSON that Vite embeds from ``web/public/``.
    On failure, previous assets and JSON remain.
    """
    src = Path(web_dist)
    dest_root = Path(public_root)
    if not src.is_dir():
        raise PublicSyncError(f"web dist missing or not a directory: {src}")
    dest_root.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []
    skipped: list[str] = []
    stage_root = dest_root / f".web-sync.{os.getpid()}.tmp"
    if stage_root.exists():
        shutil.rmtree(stage_root)
    stage_root.mkdir(parents=True, exist_ok=True)
    try:
        for entry in sorted(src.iterdir()):
            name = entry.name
            if name in SKIP_WEB_SYNC_NAMES or name in PROTECTED_TOP_LEVEL:
                skipped.append(name)
                continue
            if name.startswith("."):
                skipped.append(name)
                continue
            target = stage_root / name
            if entry.is_dir():
                shutil.copytree(entry, target)
            else:
                shutil.copy2(entry, target)
                _fsync_file(target)
            copied.append(name)
        _fsync_dir(stage_root)

        for name in copied:
            staged = stage_root / name
            final = dest_root / name
            if staged.is_dir():
                _copy_tree_atomic(staged, final)
            else:
                _atomic_replace_file(staged, final)
    except OSError as exc:
        raise PublicSyncError(f"web asset sync failed; LKG left intact: {exc}") from exc
    finally:
        if stage_root.exists():
            shutil.rmtree(stage_root, ignore_errors=True)

    return SyncAssetsResult(
        copied=tuple(copied),
        skipped=tuple(skipped),
        public_root=dest_root,
    )


def promote_release_json_to_public_root(
    public_root: Path | str,
    release_dir: Path | str,
    *,
    release_id: str | None = None,
) -> PromoteJsonResult:
    """Atomically place release dashboard JSON under ``public_root/live/``.

    Writes the full file set into ``live.candidate/``, then swaps that directory
    into ``live/`` with a single ``os.replace``. If staging or the swap fails,
    the previous ``live/`` directory (complete set) remains untouched — never a
    mixed new/old root JSON tree.
    """
    root = Path(public_root)
    rel = Path(release_dir)
    if not rel.is_dir():
        raise PublicSyncError(f"release directory missing: {rel}")
    root.mkdir(parents=True, exist_ok=True)

    missing = [name for name in DASHBOARD_RELEASE_FILES if not (rel / name).is_file()]
    if missing:
        raise PublicSyncError(
            f"release missing required JSON for public root: {','.join(missing)}"
        )

    live_dir = root / LIVE_DIR_NAME
    candidate = root / LIVE_CANDIDATE_DIR_NAME
    backup = root / f".{LIVE_DIR_NAME}.{os.getpid()}.old"

    if candidate.exists():
        shutil.rmtree(candidate)
    if backup.exists():
        shutil.rmtree(backup)

    candidate.mkdir(parents=True, exist_ok=True)
    try:
        for name in DASHBOARD_RELEASE_FILES:
            src = rel / name
            dest = candidate / name
            shutil.copy2(src, dest)
            _fsync_file(dest)
        _fsync_dir(candidate)

        # Single directory swap: previous live/ stays intact until replace succeeds.
        if live_dir.exists():
            os.replace(live_dir, backup)
        try:
            os.replace(candidate, live_dir)
        except OSError:
            if backup.exists() and not live_dir.exists():
                os.replace(backup, live_dir)
            raise
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
        _fsync_dir(root)
    except OSError as exc:
        if candidate.exists():
            shutil.rmtree(candidate, ignore_errors=True)
        raise PublicSyncError(
            f"live/ JSON promote failed; prior live/ retained: {exc}"
        ) from exc

    return PromoteJsonResult(
        files=tuple(DASHBOARD_RELEASE_FILES),
        release_id=release_id,
        public_root=root,
        live_dir=live_dir,
    )


def promote_current_release_json(public_root: Path | str) -> PromoteJsonResult:
    """Promote the ``current`` pointer's release JSON into ``live/``."""
    root = Path(public_root)
    current_path = root / "current"
    if not current_path.is_file():
        raise PublicSyncError("no current pointer; cannot promote live/ JSON")
    release_id = current_path.read_text(encoding="utf-8").strip()
    if not release_id:
        raise PublicSyncError("empty current pointer; cannot promote live/ JSON")
    release_dir = root / "releases" / release_id
    return promote_release_json_to_public_root(
        root, release_dir, release_id=release_id
    )


__all__ = [
    "LIVE_CANDIDATE_DIR_NAME",
    "LIVE_DIR_NAME",
    "PROTECTED_TOP_LEVEL",
    "PromoteJsonResult",
    "PublicSyncError",
    "SKIP_WEB_SYNC_NAMES",
    "SyncAssetsResult",
    "promote_current_release_json",
    "promote_release_json_to_public_root",
    "sync_web_assets",
]
