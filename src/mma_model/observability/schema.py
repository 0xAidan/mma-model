"""JSON schema validation for the DWCS-403 health contract."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
HEALTH_SCHEMA_PATH = REPO_ROOT / "output" / "contracts" / "health.schema.json"


class HealthSchemaError(ValueError):
    """Raised when health JSON fails the published schema."""


def load_health_schema(path: Path | None = None) -> dict[str, Any]:
    schema_path = path or HEALTH_SCHEMA_PATH
    try:
        raw = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HealthSchemaError(f"unable to load health schema: {exc}") from exc
    if not isinstance(raw, dict):
        raise HealthSchemaError("health schema root must be an object")
    return raw


def validate_health_payload(
    payload: Mapping[str, Any],
    schema: Mapping[str, Any] | None = None,
) -> None:
    resolved = schema or load_health_schema()
    _validate(payload, resolved, resolved, path="$")


def _validate(
    instance: object,
    schema: Mapping[str, Any],
    root: Mapping[str, Any],
    *,
    path: str,
) -> None:
    if "$ref" in schema:
        ref = str(schema["$ref"])
        schema = _resolve_ref(root, ref)
    types = schema.get("type")
    if types is not None:
        allowed = types if isinstance(types, list) else [types]
        if not any(_type_matches(instance, item) for item in allowed):
            raise HealthSchemaError(
                f"{path}: expected type {allowed!r}, got {type(instance).__name__}"
            )
    enum = schema.get("enum")
    if enum is not None and instance not in enum:
        raise HealthSchemaError(f"{path}: {instance!r} not in enum {enum!r}")
    const = schema.get("const", _MISSING)
    if const is not _MISSING and instance != const:
        raise HealthSchemaError(f"{path}: expected const {const!r}")
    if isinstance(instance, dict):
        _validate_object(instance, schema, root, path=path)
    elif isinstance(instance, list):
        _validate_array(instance, schema, root, path=path)
    elif isinstance(instance, str) and "minLength" in schema:
        if len(instance) < int(schema["minLength"]):
            raise HealthSchemaError(f"{path}: string shorter than minLength")
    elif isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            raise HealthSchemaError(f"{path}: below minimum")
        if "maximum" in schema and instance > schema["maximum"]:
            raise HealthSchemaError(f"{path}: above maximum")


def _validate_object(
    instance: dict[str, Any],
    schema: Mapping[str, Any],
    root: Mapping[str, Any],
    *,
    path: str,
) -> None:
    required = list(schema.get("required") or [])
    for key in required:
        if key not in instance:
            raise HealthSchemaError(f"{path}: missing required field {key!r}")
    properties = dict(schema.get("properties") or {})
    additional = schema.get("additionalProperties", True)
    for key, value in instance.items():
        child = f"{path}.{key}"
        if key in properties:
            _validate(value, properties[key], root, path=child)
            continue
        if additional is False:
            raise HealthSchemaError(f"{path}: additional field {key!r} is not allowed")
        if isinstance(additional, dict):
            _validate(value, additional, root, path=child)


def _validate_array(
    instance: list[Any],
    schema: Mapping[str, Any],
    root: Mapping[str, Any],
    *,
    path: str,
) -> None:
    if "minItems" in schema and len(instance) < int(schema["minItems"]):
        raise HealthSchemaError(f"{path}: fewer than minItems")
    if "maxItems" in schema and len(instance) > int(schema["maxItems"]):
        raise HealthSchemaError(f"{path}: more than maxItems")
    items = schema.get("items")
    if isinstance(items, dict):
        for index, value in enumerate(instance):
            _validate(value, items, root, path=f"{path}[{index}]")


def _type_matches(instance: object, expected: object) -> bool:
    if expected == "object":
        return isinstance(instance, dict)
    if expected == "array":
        return isinstance(instance, list)
    if expected == "string":
        return isinstance(instance, str)
    if expected == "integer":
        return isinstance(instance, int) and not isinstance(instance, bool)
    if expected == "number":
        return isinstance(instance, (int, float)) and not isinstance(instance, bool)
    if expected == "boolean":
        return isinstance(instance, bool)
    if expected == "null":
        return instance is None
    return False


def _resolve_ref(root: Mapping[str, Any], ref: str) -> Mapping[str, Any]:
    if not ref.startswith("#/"):
        raise HealthSchemaError(f"unsupported $ref {ref!r}")
    cursor: Any = root
    for part in ref[2:].split("/"):
        if not isinstance(cursor, dict) or part not in cursor:
            raise HealthSchemaError(f"unresolved $ref {ref!r}")
        cursor = cursor[part]
    if not isinstance(cursor, dict):
        raise HealthSchemaError(f"$ref {ref!r} did not resolve to an object")
    return cursor


_MISSING = object()


__all__ = [
    "HEALTH_SCHEMA_PATH",
    "HealthSchemaError",
    "load_health_schema",
    "validate_health_payload",
]
