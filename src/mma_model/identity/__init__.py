"""Identity resolution package (DWCS-104)."""

from mma_model.identity.models import ResolveResult, ReviewCandidate, dump_evidence_json
from mma_model.identity.normalize import name_tokens, normalize_person_name
from mma_model.identity.resolver import resolve_fighter
from mma_model.identity.review import (
    apply_review_decision,
    enqueue_review,
    list_reviews,
    reverse_review_decision,
)

__all__ = [
    "ResolveResult",
    "ReviewCandidate",
    "apply_review_decision",
    "enqueue_review",
    "list_reviews",
    "name_tokens",
    "normalize_person_name",
    "dump_evidence_json",
    "resolve_fighter",
    "reverse_review_decision",
]
