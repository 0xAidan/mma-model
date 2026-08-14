"""Structured JSON logs with secret redaction (DWCS-403)."""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Mapping, MutableMapping
from datetime import UTC, datetime
from typing import Any

from mma_model.jobs.types import JobErrorClass

_LOGGER = logging.getLogger("mma_model.observability")

_REDACTED = "[REDACTED]"

# Credentialed URLs: scheme://user:password@host
_URL_CREDS = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*)://([^:/@\s]+):([^@/\s]+)@")
# Authorization: Bearer <token> / Authorization: <token>
_AUTH_HEADER = re.compile(
    r"(?i)\b(authorization)\s*[:=]\s*(?:bearer\s+)?([^\s,;\"']+)"
)
_BEARER = re.compile(r"(?i)\bbearer\s+([a-z0-9\-._~+/]+=*)")
# api_key=... / THE_ODDS_API_KEY=... / password: ...
_KEY_VALUE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|secret|password|token|"
    r"the_odds_api_key|x-api-key)\b(\s*[:=]\s*)([^\s,;\"']+)"
)


def redact_secrets(text: str) -> str:
    """Remove API keys, bearer tokens, passwords, and credentialed URLs."""
    if not text:
        return text
    out = str(text)
    out = _URL_CREDS.sub(rf"\1://\2:{_REDACTED}@", out)
    out = _AUTH_HEADER.sub(rf"\1: {_REDACTED}", out)
    out = _BEARER.sub(f"Bearer {_REDACTED}", out)
    out = _KEY_VALUE.sub(rf"\1\2{_REDACTED}", out)
    return out


def redact_value(value: Any) -> Any:
    """Recursively redact secrets in strings nested inside mappings/sequences."""
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, Mapping):
        return {str(k): redact_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value(item) for item in value]
    if isinstance(value, BaseException):
        return redact_secrets(f"{type(value).__name__}: {value}")
    return value


def _utc_now_iso() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


def build_log_record(
    *,
    level: str,
    message: str,
    run_id: str | None = None,
    job_id: str | None = None,
    idempotency_key: str | None = None,
    event_id: str | None = None,
    bout_id: str | None = None,
    error_class: JobErrorClass | str | None = None,
    duration_ms: int | None = None,
    extra: Mapping[str, Any] | None = None,
    timestamp: str | None = None,
) -> dict[str, Any]:
    """Build one structured log object (secrets redacted)."""
    if error_class is None:
        err: str | None = None
    elif isinstance(error_class, JobErrorClass):
        err = error_class.value
    else:
        err = str(error_class)

    record: dict[str, Any] = {
        "bout_id": bout_id,
        "duration_ms": duration_ms,
        "error_class": err,
        "event_id": event_id,
        "idempotency_key": idempotency_key or job_id,
        "job_id": job_id or idempotency_key,
        "level": str(level).upper(),
        "message": redact_secrets(str(message)),
        "run_id": run_id,
        "timestamp": timestamp or _utc_now_iso(),
    }
    if extra:
        for key, value in extra.items():
            if key in record:
                continue
            record[str(key)] = redact_value(value)
    return {k: v for k, v in record.items() if v is not None}


def format_log_line(record: Mapping[str, Any]) -> str:
    """One JSON object per line with deterministic sorted keys."""
    safe = redact_value(dict(record))
    if not isinstance(safe, dict):
        safe = {"message": redact_secrets(str(safe))}
    return json.dumps(safe, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def log_event(
    *,
    level: str = "INFO",
    message: str,
    run_id: str | None = None,
    job_id: str | None = None,
    idempotency_key: str | None = None,
    event_id: str | None = None,
    bout_id: str | None = None,
    error_class: JobErrorClass | str | None = None,
    duration_ms: int | None = None,
    extra: Mapping[str, Any] | None = None,
    exc: BaseException | None = None,
    sink: MutableMapping[str, Any] | list[dict[str, Any]] | None = None,
    emit: bool = True,
) -> dict[str, Any]:
    """Create (and optionally emit) a redacted structured log record."""
    merged_extra: dict[str, Any] = dict(extra or {})
    if exc is not None:
        merged_extra["exception"] = redact_secrets(f"{type(exc).__name__}: {exc}")
    record = build_log_record(
        level=level,
        message=message,
        run_id=run_id,
        job_id=job_id,
        idempotency_key=idempotency_key,
        event_id=event_id,
        bout_id=bout_id,
        error_class=error_class,
        duration_ms=duration_ms,
        extra=merged_extra,
    )
    line = format_log_line(record)
    if sink is not None:
        if isinstance(sink, list):
            sink.append(dict(record))
        else:
            sink.update(record)
    if emit:
        log_level = getattr(logging, str(level).upper(), logging.INFO)
        _LOGGER.log(log_level, "%s", line)
    return record


def new_run_id() -> str:
    return str(uuid.uuid4())


__all__ = [
    "build_log_record",
    "format_log_line",
    "log_event",
    "new_run_id",
    "redact_secrets",
    "redact_value",
]
