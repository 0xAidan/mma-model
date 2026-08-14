"""Generate JSON Schema and TypeScript types from Pydantic dashboard models."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from mma_model.publish.constants import (
    DASHBOARD_CONTRACT_ID,
    DASHBOARD_CONTRACT_VERSION,
    DASHBOARD_SCHEMA_VERSION,
    DASHBOARD_TICKET,
)
from mma_model.publish.schema import DOCUMENT_MODELS

REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_PATH = REPO_ROOT / "output" / "contracts" / "dashboard-v1.schema.json"
TS_DIR = REPO_ROOT / "web" / "src" / "generated"
TS_DASHBOARD_PATH = TS_DIR / "dashboard.ts"
TS_INDEX_PATH = TS_DIR / "index.ts"


def build_dashboard_json_schema() -> dict[str, Any]:
    """Compose a single versioned JSON Schema document for all publish files."""
    defs: dict[str, Any] = {}
    files: dict[str, Any] = {}
    for name, model in DOCUMENT_MODELS.items():
        schema = model.model_json_schema(mode="validation", ref_template="#/$defs/{model}")
        # Hoist nested $defs then keep the root under files.
        nested = dict(schema.pop("$defs", {}) or {})
        for def_name, def_schema in nested.items():
            defs[def_name] = def_schema
        # Also register the document model itself.
        model_name = model.__name__
        defs[model_name] = schema
        files[name] = {"$ref": f"#/$defs/{model_name}"}

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://mma-model.local/contracts/dashboard-v1.schema.json",
        "title": "DWCS-500 dashboard publish contract",
        "description": (
            "Versioned static dashboard JSON published by the Python worker. "
            "Python Pydantic models are the source of truth."
        ),
        "schema_version": DASHBOARD_SCHEMA_VERSION,
        "contract_id": DASHBOARD_CONTRACT_ID,
        "contract_version": DASHBOARD_CONTRACT_VERSION,
        "ticket": DASHBOARD_TICKET,
        "type": "object",
        "additionalProperties": False,
        "required": list(DOCUMENT_MODELS.keys()),
        "properties": files,
        "$defs": defs,
    }


def _ts_type_from_schema(
    schema: Mapping[str, Any],
    *,
    defs: Mapping[str, Any],
    force_nullable: bool = False,
) -> str:
    if "$ref" in schema:
        ref = str(schema["$ref"])
        name = ref.rsplit("/", 1)[-1]
        return f"{name} | null" if force_nullable else name
    if "anyOf" in schema:
        parts = [
            _ts_type_from_schema(part, defs=defs)
            for part in schema["anyOf"]
            if not (isinstance(part, Mapping) and part.get("type") == "null")
        ]
        has_null = any(
            isinstance(part, Mapping) and part.get("type") == "null"
            for part in schema["anyOf"]
        )
        joined = " | ".join(parts) if parts else "unknown"
        return f"{joined} | null" if has_null else joined
    if "enum" in schema:
        enums = schema["enum"]
        parts = []
        for item in enums:
            if isinstance(item, str):
                parts.append(json.dumps(item))
            elif item is None:
                parts.append("null")
            elif isinstance(item, bool):
                parts.append("true" if item else "false")
            else:
                parts.append(str(item))
        return " | ".join(parts)
    if "const" in schema:
        const = schema["const"]
        if isinstance(const, str):
            return json.dumps(const)
        if const is None:
            return "null"
        if isinstance(const, bool):
            return "true" if const else "false"
        return str(const)

    types = schema.get("type")
    if isinstance(types, list):
        mapped = []
        for item in types:
            if item == "null":
                mapped.append("null")
            else:
                mapped.append(_ts_type_from_schema({**schema, "type": item}, defs=defs))
        return " | ".join(mapped)

    if types == "object":
        if "properties" in schema:
            return _ts_object_inline(schema, defs=defs)
        additional = schema.get("additionalProperties")
        if isinstance(additional, Mapping):
            value_t = _ts_type_from_schema(additional, defs=defs)
            return f"Record<string, {value_t}>"
        if additional is False:
            return "Record<string, never>"
        return "Record<string, unknown>"
    if types == "array":
        items = schema.get("items")
        if isinstance(items, Mapping):
            return f"ReadonlyArray<{_ts_type_from_schema(items, defs=defs)}>"
        return "ReadonlyArray<unknown>"
    if types == "string":
        return "string"
    if types == "integer":
        return "number"
    if types == "number":
        return "number"
    if types == "boolean":
        return "boolean"
    if types == "null":
        return "null"
    return "unknown"


def _ts_object_inline(schema: Mapping[str, Any], *, defs: Mapping[str, Any]) -> str:
    props = dict(schema.get("properties") or {})
    required = set(schema.get("required") or [])
    lines: list[str] = ["{"]
    for key, prop_schema in props.items():
        optional = "?" if key not in required else ""
        ts = _ts_type_from_schema(prop_schema, defs=defs)
        lines.append(f"  readonly {key}{optional}: {ts};")
    lines.append("}")
    return "\n".join(lines)


def render_typescript(schema_doc: Mapping[str, Any]) -> str:
    defs = dict(schema_doc.get("$defs") or {})
    chunks: list[str] = [
        "/**",
        " * Generated from Python Pydantic dashboard contracts (DWCS-500).",
        " * Do not edit by hand — regenerate via `python -m mma_model.publish.codegen`.",
        " */",
        "",
        "export const DASHBOARD_SCHEMA_VERSION = 1 as const;",
        'export const DASHBOARD_CONTRACT_ID = "dwcs_dashboard" as const;',
        f'export const DASHBOARD_CONTRACT_VERSION = "{DASHBOARD_CONTRACT_VERSION}" as const;',
        'export const DASHBOARD_TICKET = "DWCS-500" as const;',
        "",
    ]
    # Emit defs in stable order; document models last if needed.
    for name in sorted(defs.keys()):
        def_schema = defs[name]
        if not isinstance(def_schema, Mapping):
            continue
        if def_schema.get("type") == "object" or "properties" in def_schema:
            body = _ts_object_inline(def_schema, defs=defs)
            chunks.append(f"export type {name} = {body};")
            chunks.append("")
        elif "enum" in def_schema or "const" in def_schema or "type" in def_schema:
            chunks.append(
                f"export type {name} = {_ts_type_from_schema(def_schema, defs=defs)};"
            )
            chunks.append("")

    chunks.append("export type DashboardDocumentName =")
    for name in DOCUMENT_MODELS:
        chunks.append(f"  | {json.dumps(name)}")
    chunks.append(";")
    chunks.append("")
    chunks.append("export interface DashboardReleaseFiles {")
    for name, model in DOCUMENT_MODELS.items():
        chunks.append(f"  readonly {json.dumps(name)}: {model.__name__};")
    chunks.append("}")
    chunks.append("")
    return "\n".join(chunks).rstrip() + "\n"


def render_index_ts() -> str:
    return (
        "/** Re-export generated dashboard contract types (DWCS-500). */\n"
        "export * from './dashboard';\n"
    )


def write_generated_artifacts(
    *,
    schema_path: Path = SCHEMA_PATH,
    ts_dashboard_path: Path = TS_DASHBOARD_PATH,
    ts_index_path: Path = TS_INDEX_PATH,
) -> dict[str, Path]:
    schema_doc = build_dashboard_json_schema()
    schema_path.parent.mkdir(parents=True, exist_ok=True)
    schema_path.write_text(
        json.dumps(schema_doc, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    ts_dashboard_path.parent.mkdir(parents=True, exist_ok=True)
    ts_dashboard_path.write_text(render_typescript(schema_doc), encoding="utf-8")
    ts_index_path.write_text(render_index_ts(), encoding="utf-8")
    return {
        "schema": schema_path,
        "typescript": ts_dashboard_path,
        "index": ts_index_path,
    }


def generated_artifacts_are_current() -> tuple[bool, list[str]]:
    """Return whether committed generated files match a fresh regeneration."""
    schema_doc = build_dashboard_json_schema()
    expected_schema = json.dumps(schema_doc, indent=2, sort_keys=True) + "\n"
    expected_ts = render_typescript(schema_doc)
    expected_index = render_index_ts()
    problems: list[str] = []
    if not SCHEMA_PATH.is_file():
        problems.append(f"missing {SCHEMA_PATH}")
    elif SCHEMA_PATH.read_text(encoding="utf-8") != expected_schema:
        problems.append(f"stale {SCHEMA_PATH}")
    if not TS_DASHBOARD_PATH.is_file():
        problems.append(f"missing {TS_DASHBOARD_PATH}")
    elif TS_DASHBOARD_PATH.read_text(encoding="utf-8") != expected_ts:
        problems.append(f"stale {TS_DASHBOARD_PATH}")
    if not TS_INDEX_PATH.is_file():
        problems.append(f"missing {TS_INDEX_PATH}")
    elif TS_INDEX_PATH.read_text(encoding="utf-8") != expected_index:
        problems.append(f"stale {TS_INDEX_PATH}")
    return (not problems, problems)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Regenerate DWCS-500 dashboard contracts")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if generated artifacts are stale (do not write)",
    )
    args = parser.parse_args(argv)
    if args.check:
        ok, problems = generated_artifacts_are_current()
        if not ok:
            for problem in problems:
                print(f"codegen check failed: {problem}")
            return 1
        print("codegen check ok")
        return 0
    written = write_generated_artifacts()
    for label, path in written.items():
        print(f"wrote {label}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
