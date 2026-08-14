"""SQLite online backup API + recoverable bundle assembly (DWCS-505).

Never copy a live SQLite database file with shutil/cp. Snapshots use
``sqlite3.Connection.backup`` so WAL writers can keep running safely.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import stat
from collections.abc import Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

BUNDLE_SCHEMA_VERSION: Final = "dwcs_backup_bundle_v1"
INTEGRITY_OK: Final = "ok"
SQLITE_NAME: Final = "mma.db"
REQUIRED_DASHBOARD_FILES: Final = (
    "release.json",
    "manifest.json",
    "current-event.json",
    "matchups.json",
    "performance.json",
    "history.json",
    "health.json",
)

_SECRET_LINE = re.compile(
    r"(?i)^(\s*(?:export\s+)?"
    r"(?:.*(?:PASSWORD|SECRET|TOKEN|API[_-]?KEY|RESTIC_PASSWORD"
    r"|THE_ODDS_API_KEY|BALLDONTLIE_API_KEY|SPORTSDATAIO_API_KEY"
    r"|HC[_-]?.*URL|BEARER).*)"
    r"\s*[=:]\s*)(.+)$"
)
_URI_USERINFO = re.compile(r"(://[^:/@\s]+:)([^@/\s]+)(@)")
_HC_UUID = re.compile(
    r"(hc-ping\.com/|healthchecks\.io/ping/)[0-9a-f-]{8,}",
    re.IGNORECASE,
)


class BackupError(RuntimeError):
    """Backup or restore verification failed."""


@dataclass(frozen=True, slots=True)
class BackupPaths:
    """Host paths used to assemble a recoverable backup bundle."""

    database_path: Path
    output_dir: Path
    data_dir: Path | None = None
    public_dir: Path | None = None
    artifacts_dir: Path | None = None
    deploy_dir: Path | None = None
    env_file: Path | None = None
    config_dir: Path | None = None
    repo_root: Path | None = None


@dataclass(frozen=True, slots=True)
class BackupResult:
    """Result of a successful local bundle creation (pre-restic)."""

    bundle_dir: Path
    sqlite_path: Path
    integrity_check: str
    metadata_path: Path
    artifact_manifest_count: int
    release_ids: tuple[str, ...]
    started_at: str
    finished_at: str


def online_backup_sqlite(source: Path, destination: Path) -> Path:
    """Create a transactionally consistent SQLite snapshot via the backup API.

    Refuses to treat ``destination`` as a byte-copy of a live DB file. The
    destination parent is created; an existing destination file is replaced
    only after a successful backup completes into a temporary sibling.
    """
    src = Path(source)
    dest = Path(destination)
    if not src.is_file():
        raise BackupError(f"source database missing: {src}")
    if src.resolve() == dest.resolve():
        raise BackupError("refusing to online-backup a database onto itself")

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".online-backup-tmp")
    if tmp.exists():
        tmp.unlink()

    try:
        with (
            sqlite3.connect(f"file:{src}?mode=ro", uri=True) as src_conn,
            sqlite3.connect(tmp) as dst_conn,
        ):
            src_conn.backup(dst_conn)
            dst_conn.commit()
    except sqlite3.Error as exc:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise BackupError(f"SQLite online backup failed: {exc}") from exc

    tmp.replace(dest)
    return dest


def integrity_check(db_path: Path) -> str:
    """Run ``PRAGMA integrity_check`` and return the raw result string."""
    path = Path(db_path)
    if not path.is_file():
        raise BackupError(f"database missing for integrity_check: {path}")
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            rows = conn.execute("PRAGMA integrity_check").fetchall()
    except sqlite3.Error as exc:
        raise BackupError(f"integrity_check failed: {exc}") from exc
    if not rows:
        raise BackupError("integrity_check returned no rows")
    joined = "\n".join(str(row[0]) for row in rows).strip()
    if not joined:
        raise BackupError("integrity_check returned empty result")
    return joined


def create_backup_bundle(paths: BackupPaths) -> BackupResult:
    """Assemble a recoverable bundle under ``paths.output_dir``.

    Does **not** delete or mutate the live database, artifacts, or public tree
    on failure. Staging under ``output_dir`` may be replaced.
    """
    started = _utc_now()
    out = Path(paths.output_dir)
    if out.exists():
        if any(out.iterdir()):
            raise BackupError(
                f"backup output must be empty or absent (got non-empty {out})"
            )
    else:
        out.mkdir(parents=True, exist_ok=True)

    sqlite_dir = out / "sqlite"
    sqlite_dir.mkdir(parents=True, exist_ok=True)
    sqlite_dest = sqlite_dir / SQLITE_NAME
    online_backup_sqlite(paths.database_path, sqlite_dest)
    check = integrity_check(sqlite_dest)
    (sqlite_dir / "integrity_check.txt").write_text(check + "\n", encoding="utf-8")
    if check.splitlines()[0].strip().lower() != INTEGRITY_OK:
        raise BackupError(f"snapshot integrity_check failed: {check!r}")

    artifacts_dir = _resolve_artifacts_dir(paths)
    artifact_manifests = _copy_artifacts(artifacts_dir, out / "artifacts")
    hashes = _collect_hashes(artifact_manifests, paths.config_dir, paths.repo_root)
    (out / "hashes.json").write_text(
        json.dumps(hashes, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    release_ids = _copy_public_releases(paths.public_dir, out / "public")
    _copy_deploy_defs(paths.deploy_dir, out / "deploy")
    _write_redacted_env(paths.env_file, out / "recovery" / "env.redacted")
    metadata = _build_metadata(
        started_at=started,
        finished_at=_utc_now(),
        paths=paths,
        integrity=check,
        artifact_manifest_count=len(artifact_manifests),
        release_ids=release_ids,
        hashes=hashes,
    )
    meta_path = out / "recovery" / "metadata.json"
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    finished = str(metadata["finished_at"])
    return BackupResult(
        bundle_dir=out,
        sqlite_path=sqlite_dest,
        integrity_check=check,
        metadata_path=meta_path,
        artifact_manifest_count=len(artifact_manifests),
        release_ids=tuple(release_ids),
        started_at=started,
        finished_at=finished,
    )


def verify_restored_bundle(
    target: Path,
    *,
    require_artifacts: bool = False,
    load_artifacts: bool = False,
) -> dict[str, Any]:
    """Validate a restored empty-target tree.

    Checks SQLite integrity, optional Alembic head reachability is left to
    callers (deploy/restore.sh). When ``load_artifacts`` is true, attempts to
    load JSON artifacts via the modeling loader.
    """
    root = Path(target)
    if not root.is_dir():
        raise BackupError(f"restore target missing: {root}")
    db = root / "sqlite" / SQLITE_NAME
    if not db.is_file():
        raise BackupError(f"restored sqlite missing: {db}")
    check = integrity_check(db)
    if check.splitlines()[0].strip().lower() != INTEGRITY_OK:
        raise BackupError(f"restored integrity_check failed: {check!r}")

    meta_path = root / "recovery" / "metadata.json"
    if not meta_path.is_file():
        raise BackupError("restored recovery/metadata.json missing")
    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    if metadata.get("schema_version") != BUNDLE_SCHEMA_VERSION:
        raise BackupError(
            f"unexpected bundle schema_version: {metadata.get('schema_version')!r}"
        )

    public = root / "public"
    live = public / "live"
    current = public / "current"
    missing_live: list[str] = []
    if live.is_dir():
        for name in REQUIRED_DASHBOARD_FILES:
            if not (live / name).is_file():
                missing_live.append(name)
    else:
        missing_live = list(REQUIRED_DASHBOARD_FILES)

    artifacts_root = root / "artifacts"
    artifact_files = sorted(artifacts_root.glob("*.json")) if artifacts_root.is_dir() else []
    sidecars = [p for p in artifact_files if p.name.endswith(".manifest.json")]
    payloads = [
        p
        for p in artifact_files
        if not p.name.endswith(".manifest.json") and p.suffix == ".json"
    ]
    if require_artifacts and not sidecars and not payloads:
        raise BackupError("restored bundle has no model artifacts")

    loaded: list[str] = []
    if load_artifacts and payloads:
        # Import contract first to avoid modeling ↔ backtest circular import
        # when this module is the first modeling entrypoint in a fresh process.
        import mma_model.backtest.contract  # noqa: F401
        from mma_model.modeling.artifacts import load_artifact

        for payload in payloads:
            side = payload.with_suffix(".manifest.json")
            if not side.is_file():
                continue
            load_artifact(payload)
            loaded.append(payload.name)

    return {
        "integrity_check": check,
        "metadata": metadata,
        "missing_live_files": missing_live,
        "current_release": current.read_text(encoding="utf-8").strip()
        if current.is_file()
        else None,
        "artifact_payloads": [p.name for p in payloads],
        "artifact_manifests": [p.name for p in sidecars],
        "loaded_artifacts": loaded,
        "deploy_present": (root / "deploy").is_dir(),
    }


def redact_text(text: str) -> str:
    """Redact secret-looking assignments and Healthchecks UUIDs."""
    lines: list[str] = []
    for line in text.splitlines():
        m = _SECRET_LINE.match(line)
        if m:
            lines.append(f"{m.group(1)}[REDACTED]")
            continue
        cleaned = _URI_USERINFO.sub(r"\1[REDACTED]\3", line)
        cleaned = _HC_UUID.sub(r"\1[REDACTED]", cleaned)
        lines.append(cleaned)
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_artifacts_dir(paths: BackupPaths) -> Path | None:
    if paths.artifacts_dir is not None:
        return Path(paths.artifacts_dir)
    if paths.data_dir is not None:
        candidate = Path(paths.data_dir) / "artifacts"
        if candidate.is_dir():
            return candidate
    return None


def _copy_artifacts(source: Path | None, dest: Path) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    if source is None or not source.is_dir():
        (dest / "README.txt").write_text(
            "No model artifacts directory present at backup time.\n",
            encoding="utf-8",
        )
        return []
    manifests: list[Path] = []
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(source)
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        if path.name.endswith(".manifest.json"):
            manifests.append(target)
    return manifests


def _collect_hashes(
    manifests: Sequence[Path],
    config_dir: Path | None,
    repo_root: Path | None,
) -> dict[str, Any]:
    artifact_hashes: list[dict[str, Any]] = []
    for path in manifests:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            artifact_hashes.append({"path": path.name, "error": "unreadable"})
            continue
        if not isinstance(payload, Mapping):
            continue
        artifact_hashes.append(
            {
                "path": path.name,
                "payload_sha256": payload.get("payload_sha256"),
                "config_hash": payload.get("config_hash"),
                "contract_hash": payload.get("contract_hash"),
                "feature_spec_hash": payload.get("feature_spec_hash"),
                "code_hash": payload.get("code_hash"),
                "data_hash": payload.get("data_hash"),
                "splits_config_hash": payload.get("splits_config_hash"),
            }
        )

    config_hashes: dict[str, str] = {}
    roots: list[Path] = []
    if config_dir is not None and Path(config_dir).is_dir():
        roots.append(Path(config_dir))
    if repo_root is not None:
        cfg = Path(repo_root) / "config"
        if cfg.is_dir() and cfg not in roots:
            roots.append(cfg)
    for root in roots:
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix.lower() not in {".json", ".yaml", ".yml", ".toml"}:
                continue
            rel = str(path.relative_to(root))
            config_hashes[f"{root.name}/{rel}"] = _sha256_file(path)

    return {
        "artifact_manifests": artifact_hashes,
        "configuration_files": config_hashes,
    }


def _copy_public_releases(public_dir: Path | None, dest: Path) -> list[str]:
    dest.mkdir(parents=True, exist_ok=True)
    if public_dir is None or not Path(public_dir).is_dir():
        (dest / "README.txt").write_text(
            "No public directory present at backup time.\n",
            encoding="utf-8",
        )
        return []

    src = Path(public_dir)
    release_ids: list[str] = []
    current_id: str | None = None
    current_file = src / "current"
    if current_file.is_file():
        current_id = current_file.read_text(encoding="utf-8").strip()
        (dest / "current").write_text(current_id + "\n", encoding="utf-8")

    releases_src = src / "releases"
    releases_dst = dest / "releases"
    releases_dst.mkdir(parents=True, exist_ok=True)
    available: list[str] = []
    if releases_src.is_dir():
        available = sorted(
            p.name for p in releases_src.iterdir() if p.is_dir() and not p.name.startswith(".")
        )

    selected: list[str] = []
    if current_id and current_id in available:
        selected.append(current_id)
    # Prefer one prior release (lexicographic previous when ids are time-like).
    priors = [name for name in available if name != current_id]
    if priors:
        # Pick the last prior that is not current (stable for bootstrap-a/b).
        selected.append(priors[-1] if priors[-1] != current_id else priors[0])
    # Deduplicate while preserving order.
    seen: set[str] = set()
    for name in selected:
        if name in seen:
            continue
        seen.add(name)
        release_ids.append(name)
        _copy_tree(releases_src / name, releases_dst / name)

    live_src = src / "live"
    if live_src.is_dir():
        _copy_tree(live_src, dest / "live")
    # Keep index/assets pointers lightweight: copy top-level index if present.
    for name in ("index.html", "favicon.ico"):
        candidate = src / name
        if candidate.is_file():
            shutil.copy2(candidate, dest / name)
    assets = src / "assets"
    if assets.is_dir():
        _copy_tree(assets, dest / "assets")
    return release_ids


def _copy_deploy_defs(deploy_dir: Path | None, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    if deploy_dir is None or not Path(deploy_dir).is_dir():
        (dest / "README.txt").write_text(
            "No deploy directory present at backup time.\n",
            encoding="utf-8",
        )
        return
    src = Path(deploy_dir)
    wanted = (
        "compose.yaml",
        "compose.ci.yaml",
        "image-digest.txt",
        "Caddyfile.mma",
        "backup-hook.sh",
        "backup.sh",
        "restore.sh",
        "run-job.sh",
        "monitor-check.sh",
        "rollback.sh",
        "install.sh",
        "README.md",
    )
    for name in wanted:
        path = src / name
        if path.is_file():
            shutil.copy2(path, dest / name)
    systemd_src = src / "systemd"
    if systemd_src.is_dir():
        _copy_tree(systemd_src, dest / "systemd")
    logrotate_src = src / "logrotate"
    if logrotate_src.is_dir():
        _copy_tree(logrotate_src, dest / "logrotate")


def _write_redacted_env(env_file: Path | None, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if env_file is None or not Path(env_file).is_file():
        dest.write_text(
            "# No env file present at backup time.\n",
            encoding="utf-8",
        )
        return
    raw = Path(env_file).read_text(encoding="utf-8", errors="replace")
    dest.write_text(redact_text(raw), encoding="utf-8")
    with suppress(OSError):
        dest.chmod(stat.S_IRUSR | stat.S_IWUSR)


def _build_metadata(
    *,
    started_at: str,
    finished_at: str,
    paths: BackupPaths,
    integrity: str,
    artifact_manifest_count: int,
    release_ids: Sequence[str],
    hashes: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "ticket": "DWCS-505",
        "started_at": started_at,
        "finished_at": finished_at,
        "sqlite": {
            "filename": SQLITE_NAME,
            "integrity_check": integrity,
            "method": "sqlite3.Connection.backup",
            "source_basename": Path(paths.database_path).name,
        },
        "artifact_manifest_count": artifact_manifest_count,
        "release_ids": list(release_ids),
        "paths_present": {
            "data_dir": paths.data_dir is not None,
            "public_dir": paths.public_dir is not None,
            "artifacts_dir": paths.artifacts_dir is not None
            or (
                paths.data_dir is not None
                and (Path(paths.data_dir) / "artifacts").is_dir()
            ),
            "deploy_dir": paths.deploy_dir is not None,
            "env_file": paths.env_file is not None and Path(paths.env_file).is_file(),
        },
        "hash_summary": {
            "artifact_manifest_count": len(hashes.get("artifact_manifests", [])),
            "configuration_file_count": len(hashes.get("configuration_files", {})),
        },
        "notes": [
            "Live SQLite was snapshotted via the online backup API (never raw cp).",
            "Secrets in recovery/env.redacted are replaced with [REDACTED].",
            "restic password and repository credentials are never stored in the bundle.",
        ],
    }


def _copy_tree(src: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def assert_not_live_byte_copy(source: Path, destination: Path) -> None:
    """Test helper: ensure destination is not a hardlink/byte-identical live copy path."""
    src = Path(source).resolve()
    dest = Path(destination).resolve()
    if src == dest:
        raise BackupError("destination resolves to the live database path")


def iter_bundle_files(bundle_dir: Path) -> Iterable[Path]:
    root = Path(bundle_dir)
    for path in sorted(root.rglob("*")):
        if path.is_file():
            yield path
