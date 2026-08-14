"""Validate staged dashboard release directories (DWCS-500)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mma_model.observability.publish_guard import PublishValidationError
from mma_model.publish.constants import DASHBOARD_RELEASE_FILES
from mma_model.publish.schema import DOCUMENT_MODELS, validate_document
from mma_model.publish.secrets import SecretScanError, scan_json_text_for_secrets


def validate_dashboard_release_dir(release_dir: Path) -> None:
    """Schema-validate every dashboard JSON file and reject secrets."""
    for name in DASHBOARD_RELEASE_FILES:
        path = release_dir / name
        if not path.is_file():
            raise PublishValidationError(f"candidate missing required files: {name}")
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise PublishValidationError(f"unable to read {name}: {exc}") from exc
        try:
            scan_json_text_for_secrets(text, path=name)
        except SecretScanError as exc:
            raise PublishValidationError(str(exc)) from exc
        try:
            payload: Any = json.loads(text)
        except json.JSONDecodeError as exc:
            raise PublishValidationError(f"invalid JSON in {name}: {exc}") from exc
        try:
            validate_document(name, payload)
        except Exception as exc:  # noqa: BLE001 — surface as publish validation
            raise PublishValidationError(f"{name} failed schema validation: {exc}") from exc

    # Reject unexpected extra JSON files that could leak payloads.
    for path in sorted(release_dir.glob("*.json")):
        if path.name not in DOCUMENT_MODELS:
            raise PublishValidationError(
                f"unexpected release file not in contract: {path.name}"
            )


__all__ = ["validate_dashboard_release_dir"]
