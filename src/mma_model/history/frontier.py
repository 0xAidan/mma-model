"""Deterministic crawl frontier with bounded depth/pages (DWCS-105)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from mma_model.db.tables.history import HistoryFrontier
from mma_model.history.constants import MAX_DEPTH, MAX_PAGES_PER_RUN


class PaginationLoopError(RuntimeError):
    """Raised when a source pagination cursor repeats a seen URL."""

    def __init__(self, url: str) -> None:
        self.url = url
        super().__init__(f"pagination_loop url={url!r}")


class PageBudgetError(RuntimeError):
    """Raised when the per-run page budget is exhausted."""

    def __init__(self, count: int, budget: int) -> None:
        self.count = count
        self.budget = budget
        super().__init__(f"page_budget_exhausted count={count} budget={budget}")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def frontier_token(*, source: str, entity_kind: str, entity_id: str) -> str:
    return f"{source}:{entity_kind}:{entity_id}"


@dataclass
class RegionalFrontier:
    """Pending fighter/event URLs with depth and page budget.

    Checkpoints are deterministic by fighter/event/source ID. Restart is
    idempotent: completed entities are skipped; pending resume from cursor.
    """

    source: str
    max_pages_per_run: int = MAX_PAGES_PER_RUN
    max_depth: int = MAX_DEPTH
    seen_urls: set[str] = field(default_factory=set)
    pages_this_run: int = 0

    def register_url(self, url: str) -> None:
        if url in self.seen_urls:
            raise PaginationLoopError(url)
        if self.pages_this_run >= self.max_pages_per_run:
            raise PageBudgetError(self.pages_this_run, self.max_pages_per_run)
        self.seen_urls.add(url)
        self.pages_this_run += 1

    def depth_allowed(self, depth: int) -> bool:
        return 0 <= depth <= self.max_depth

    def load_or_create(
        self,
        session: Session,
        *,
        entity_kind: str,
        entity_id: str,
        depth: int = 0,
    ) -> HistoryFrontier:
        row = session.scalars(
            select(HistoryFrontier).where(
                HistoryFrontier.source == self.source,
                HistoryFrontier.entity_kind == entity_kind,
                HistoryFrontier.entity_id == entity_id,
            )
        ).first()
        if row is not None:
            return row
        row = HistoryFrontier(
            source=self.source,
            entity_kind=entity_kind,
            entity_id=entity_id,
            depth=depth,
            status="pending",
            cursor_json="{}",
            page_count=0,
            updated_at=_utc_now(),
        )
        session.add(row)
        session.flush()
        return row

    def mark(
        self,
        session: Session,
        *,
        entity_kind: str,
        entity_id: str,
        status: str,
        cursor: dict[str, Any] | None = None,
        page_count: int | None = None,
        depth: int | None = None,
    ) -> HistoryFrontier:
        row = self.load_or_create(
            session, entity_kind=entity_kind, entity_id=entity_id
        )
        row.status = status
        if cursor is not None:
            row.cursor_json = json.dumps(cursor, sort_keys=True, separators=(",", ":"))
        if page_count is not None:
            row.page_count = page_count
        if depth is not None:
            row.depth = depth
        row.updated_at = _utc_now()
        return row

    def pending_ids(
        self, session: Session, *, entity_kind: str
    ) -> list[str]:
        rows = session.scalars(
            select(HistoryFrontier)
            .where(
                HistoryFrontier.source == self.source,
                HistoryFrontier.entity_kind == entity_kind,
                HistoryFrontier.status == "pending",
            )
            .order_by(HistoryFrontier.entity_id.asc())
        ).all()
        return [row.entity_id for row in rows]

    def seed(
        self,
        session: Session,
        *,
        entity_kind: str,
        entity_ids: Iterable[str],
        depth: int = 0,
    ) -> int:
        created = 0
        for entity_id in sorted({eid.strip() for eid in entity_ids if eid and eid.strip()}):
            existing = session.scalars(
                select(HistoryFrontier).where(
                    HistoryFrontier.source == self.source,
                    HistoryFrontier.entity_kind == entity_kind,
                    HistoryFrontier.entity_id == entity_id,
                )
            ).first()
            if existing is not None:
                continue
            session.add(
                HistoryFrontier(
                    source=self.source,
                    entity_kind=entity_kind,
                    entity_id=entity_id,
                    depth=depth,
                    status="pending",
                    cursor_json="{}",
                    page_count=0,
                    updated_at=_utc_now(),
                )
            )
            created += 1
        return created
