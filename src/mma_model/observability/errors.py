"""Retry / failure classification wrappers over JobErrorClass (DWCS-403).

Do not invent a second error taxonomy — reuse ``JobErrorClass``,
``NON_RETRYABLE_ERRORS``, and ``DEFAULT_MAX_TRANSIENT_ATTEMPTS``.
"""

from __future__ import annotations

from dataclasses import dataclass

from mma_model.jobs.types import (
    DEFAULT_MAX_TRANSIENT_ATTEMPTS,
    NON_RETRYABLE_ERRORS,
    JobErrorClass,
)


def is_non_retryable(error_class: JobErrorClass | str | None) -> bool:
    """True for definitive auth/schema/entitlement/overlap/dependency failures."""
    if error_class is None:
        return False
    if isinstance(error_class, str):
        try:
            error_class = JobErrorClass(error_class)
        except ValueError:
            return False
    return error_class in NON_RETRYABLE_ERRORS


def is_retryable(error_class: JobErrorClass | str | None) -> bool:
    """Only transient failures are retryable under the shared taxonomy."""
    if error_class is None:
        return False
    if isinstance(error_class, str):
        try:
            error_class = JobErrorClass(error_class)
        except ValueError:
            return False
    if error_class in NON_RETRYABLE_ERRORS:
        return False
    return error_class == JobErrorClass.TRANSIENT


@dataclass(frozen=True)
class BoundedRetryPolicy:
    """Bounded retry for transient errors; auth/schema fail closed immediately."""

    max_attempts: int = DEFAULT_MAX_TRANSIENT_ATTEMPTS

    def should_retry(
        self,
        error_class: JobErrorClass | str | None,
        *,
        attempt: int,
    ) -> bool:
        if attempt >= self.max_attempts:
            return False
        return is_retryable(error_class)

    def classify_exception_message(self, message: str) -> JobErrorClass:
        """Map common definitive failure phrases; default to transient."""
        lower = str(message or "").lower()
        if any(tok in lower for tok in ("401", "403", "unauthorized", "authentication")):
            return JobErrorClass.AUTHENTICATION
        if any(tok in lower for tok in ("schema", "validation failed", "contract drift")):
            return JobErrorClass.SCHEMA
        if "entitlement" in lower or "402" in lower:
            return JobErrorClass.ENTITLEMENT
        return JobErrorClass.TRANSIENT


__all__ = [
    "BoundedRetryPolicy",
    "is_non_retryable",
    "is_retryable",
]
