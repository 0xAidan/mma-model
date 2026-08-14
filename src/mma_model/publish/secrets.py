"""Secret / licensed-raw payload scanning for published dashboard JSON."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from mma_model.publish.constants import SECRET_SCAN_PATTERNS

_SECRET_RE = re.compile(
    "|".join(re.escape(p) for p in SECRET_SCAN_PATTERNS),
    re.IGNORECASE,
)

# Nested keys that look like licensed provider dumps.
_FORBIDDEN_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "the_odds_api_key",
        "odds_api_key",
        "bearer",
        "authorization",
        "raw_payload",
        "licensed_raw",
        "provider_payload",
        "x-api-key",
        "access_token",
        "refresh_token",
    }
)


class SecretScanError(ValueError):
    """Published payload contains secrets or licensed raw fields."""


def scan_text_for_secrets(text: str, *, path: str = "$") -> None:
    if _SECRET_RE.search(text):
        raise SecretScanError(f"{path}: secret or licensed-raw pattern detected")


def scan_payload_for_secrets(payload: object, *, path: str = "$") -> None:
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            key_l = str(key).lower().replace("-", "_")
            child = f"{path}.{key}"
            if key_l in _FORBIDDEN_KEYS or any(
                pat.replace(" ", "_").replace("-", "_") in key_l
                for pat in ("api_key", "raw_payload", "licensed_raw", "provider_payload")
            ):
                raise SecretScanError(f"{child}: forbidden secret/raw key")
            if isinstance(value, str):
                scan_text_for_secrets(value, path=child)
            else:
                scan_payload_for_secrets(value, path=child)
        return
    if isinstance(payload, list):
        for index, item in enumerate(payload):
            scan_payload_for_secrets(item, path=f"{path}[{index}]")
        return
    if isinstance(payload, str):
        scan_text_for_secrets(payload, path=path)


def scan_json_text_for_secrets(text: str, *, path: str = "$") -> None:
    scan_text_for_secrets(text, path=path)
    try:
        loaded: Any = json.loads(text)
    except json.JSONDecodeError:
        return
    scan_payload_for_secrets(loaded, path=path)


__all__ = [
    "SecretScanError",
    "scan_json_text_for_secrets",
    "scan_payload_for_secrets",
    "scan_text_for_secrets",
]
