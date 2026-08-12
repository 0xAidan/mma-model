"""Content-addressed compressed raw payload store (outside Git)."""

from __future__ import annotations

import gzip
import hashlib
import os
import tempfile
from pathlib import Path


class PayloadCorruptionError(RuntimeError):
    """Raised when on-disk bytes do not match the expected content hash."""


class ContentAddressedRawStore:
    """Store gzip-compressed payloads keyed by SHA-256 of the uncompressed bytes.

    Writes are atomic (temp file + ``os.replace``) and idempotent for identical
    content. Existing files are hash-verified before being treated as a hit.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, content_hash: str) -> Path:
        if len(content_hash) != 64 or any(c not in "0123456789abcdef" for c in content_hash):
            raise ValueError(f"invalid content hash: {content_hash!r}")
        return self.root / content_hash[:2] / f"{content_hash}.gz"

    def put(self, payload: bytes) -> str:
        digest = hashlib.sha256(payload).hexdigest()
        target = self.path_for(digest)
        if target.exists():
            self.verify(digest)
            return digest
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{digest}.", suffix=".tmp", dir=target.parent)
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                with gzip.GzipFile(fileobj=handle, mode="wb", mtime=0) as gz:
                    gz.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, target)
        except Exception:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            raise
        # Another writer may have won the replace race; verify final bytes.
        self.verify(digest)
        return digest

    def get(self, content_hash: str) -> bytes:
        self.verify(content_hash)
        with gzip.open(self.path_for(content_hash), "rb") as handle:
            return handle.read()

    def verify(self, content_hash: str) -> None:
        path = self.path_for(content_hash)
        if not path.is_file():
            raise PayloadCorruptionError(f"missing raw payload for hash {content_hash}")
        try:
            with gzip.open(path, "rb") as handle:
                data = handle.read()
        except OSError as exc:
            raise PayloadCorruptionError(
                f"unreadable raw payload for hash {content_hash}"
            ) from exc
        actual = hashlib.sha256(data).hexdigest()
        if actual != content_hash:
            raise PayloadCorruptionError(
                f"content hash mismatch for {content_hash}: got {actual}"
            )

    def exists(self, content_hash: str) -> bool:
        return self.path_for(content_hash).is_file()
