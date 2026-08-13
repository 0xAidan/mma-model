"""Unique terminal decision evidence per identity review.

Revision ID: 0016_identity_decision_evidence_unique
Revises: 0015_quote_eligibility_scope
Create Date: 2026-08-13

Defense in depth for concurrent approve/reject races: at most one
``approved``/``rejected`` evidence row per ``review_id``. Correctness still
depends on pending/version CAS before side effects; this index prevents
silent duplicate persistence if a race slips through.

Upgrade fails closed when duplicate terminal evidence already exists; it does
not delete or rewrite immutable audit rows.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from mma_model.db.tables.identity import (
    IDENTITY_DECISION_EVIDENCE_INDEX_NAME,
    IDENTITY_DECISION_EVIDENCE_WHERE,
)

revision: str = "0016_identity_decision_evidence_unique"
down_revision: Union[str, Sequence[str], None] = "0015_quote_eligibility_scope"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEX_NAME = IDENTITY_DECISION_EVIDENCE_INDEX_NAME
_WHERE = IDENTITY_DECISION_EVIDENCE_WHERE
_MAX_LISTED_REVIEW_IDS = 20


class IdentityDecisionEvidenceDuplicateError(RuntimeError):
    """Raised when duplicate approved/rejected evidence blocks index creation."""


def _assert_no_duplicate_decision_evidence() -> None:
    """Fail closed with an actionable report; never mutate audit evidence."""
    bind = op.get_bind()
    rows = list(
        bind.execute(
            sa.text(
                "SELECT review_id, COUNT(*) AS n "
                "FROM identity_match_evidence "
                f"WHERE {_WHERE} "
                "GROUP BY review_id "
                "HAVING COUNT(*) > 1 "
                "ORDER BY n DESC, review_id ASC"
            )
        ).fetchall()
    )
    if not rows:
        return

    listed = rows[:_MAX_LISTED_REVIEW_IDS]
    examples = ", ".join(f"{rid} (count={int(n)})" for rid, n in listed)
    overflow = len(rows) - len(listed)
    if overflow > 0:
        examples = f"{examples}, (+{overflow} more)"
    raise IdentityDecisionEvidenceDuplicateError(
        f"Cannot create {_INDEX_NAME}: {len(rows)} review_id group(s) have "
        "duplicate approved/rejected identity_match_evidence rows. "
        "Immutable audit evidence was not deleted or rewritten. "
        "Resolve duplicates manually, then re-run the migration. "
        f"Duplicate groups: {examples}"
    )


def upgrade() -> None:
    _assert_no_duplicate_decision_evidence()
    op.execute(
        sa.text(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {_INDEX_NAME} "
            f"ON identity_match_evidence(review_id) "
            f"WHERE {_WHERE}"
        )
    )


def downgrade() -> None:
    op.execute(sa.text(f"DROP INDEX IF EXISTS {_INDEX_NAME}"))
