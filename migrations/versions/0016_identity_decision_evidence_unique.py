"""Unique terminal decision evidence per identity review.

Revision ID: 0016_identity_decision_evidence_unique
Revises: 0015_quote_eligibility_scope
Create Date: 2026-08-13

Defense in depth for concurrent approve/reject races: at most one
``approved``/``rejected`` evidence row per ``review_id``. Correctness still
depends on pending/version CAS before side effects; this index prevents
silent duplicate persistence if a race slips through.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016_identity_decision_evidence_unique"
down_revision: Union[str, Sequence[str], None] = "0015_quote_eligibility_scope"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEX_NAME = "uq_identity_evidence_review_decision"


def upgrade() -> None:
    op.execute(
        sa.text(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {_INDEX_NAME} "
            "ON identity_match_evidence(review_id) "
            "WHERE action IN ('approved', 'rejected') AND review_id IS NOT NULL"
        )
    )


def downgrade() -> None:
    op.execute(sa.text(f"DROP INDEX IF EXISTS {_INDEX_NAME}"))
