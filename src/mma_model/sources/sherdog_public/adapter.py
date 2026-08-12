"""Sherdog public adapter: selective secondary reconciliation (DWCS-105)."""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Iterator
from urllib.parse import urlparse

from mma_model.history.access import detect_access_kill
from mma_model.history.constants import MAX_BOUTS_PER_FIGHTER, MAX_PAGES_PER_RUN
from mma_model.history.frontier import PageBudgetError, PaginationLoopError, RegionalFrontier
from mma_model.sources.contracts import SourceObservationRecord
from mma_model.sources.http.block_signals import SourceBlockedError
from mma_model.sources.pit_proxy import PitProxyRule, load_pit_proxy_rule
from mma_model.sources.sherdog_public.client import BASE_HOST, SherdogPublicClient
from mma_model.sources.sherdog_public.mapper import map_fighter_to_observations
from mma_model.sources.sherdog_public.parser import SOURCE_SHERDOG_PUBLIC, parse_fighter_page

if TYPE_CHECKING:
    from mma_model.ingest.raw_store import ContentAddressedRawStore


class SherdogPublicAdapter:
    """Orchestrate Sherdog fetch/parse/map without writing ORM tables."""

    def __init__(
        self,
        *,
        fixture_root: Path | None = None,
        client: SherdogPublicClient | None = None,
        raw_store: ContentAddressedRawStore | None = None,
        proxy: PitProxyRule | None = None,
        max_pages_per_run: int = MAX_PAGES_PER_RUN,
    ) -> None:
        self.fixture_root = fixture_root
        self.client = client
        self.raw_store = raw_store
        self.proxy = proxy if proxy is not None else load_pit_proxy_rule()
        self.max_pages_per_run = max_pages_per_run
        self.killed_reason: str | None = None

    @classmethod
    def for_fixtures(
        cls,
        *,
        fixture_root: Path,
        raw_store: ContentAddressedRawStore | None = None,
    ) -> SherdogPublicAdapter:
        return cls(fixture_root=fixture_root, client=None, raw_store=raw_store)

    def iter_fighter_observations(
        self,
        *,
        fighter_external_id: str,
        observed_at: datetime,
        identity_status: str = "unresolved",
        fighter_canonical_id: str | None = None,
    ) -> Iterator[SourceObservationRecord]:
        if observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware UTC")
        frontier = RegionalFrontier(
            source=SOURCE_SHERDOG_PUBLIC,
            max_pages_per_run=self.max_pages_per_run,
        )
        page_id = fighter_external_id
        bout_count = 0
        while page_id:
            html, digest = self._load_fighter_html(page_id)
            url = f"http://www.{BASE_HOST}/fighter/{page_id}"
            try:
                frontier.register_url(url)
            except PaginationLoopError as exc:
                raise SourceBlockedError("pagination_loop", host=BASE_HOST) from exc
            except PageBudgetError as exc:
                raise SourceBlockedError("page_budget_exhausted", host=BASE_HOST) from exc
            login = detect_access_kill(html)
            if login:
                self.killed_reason = login
                raise SourceBlockedError(login, host=BASE_HOST)
            if self.raw_store is not None:
                self.raw_store.put(html.encode("utf-8"))
            parsed = parse_fighter_page(html)
            rows = map_fighter_to_observations(
                parsed=parsed,
                observed_at=observed_at,
                payload_hash=digest,
                proxy=self.proxy,
                identity_status=identity_status,
                fighter_canonical_id=fighter_canonical_id,
            )
            for row in rows:
                if row.entity_kind == "regional_bout":
                    bout_count += 1
                    if bout_count > MAX_BOUTS_PER_FIGHTER:
                        return
                yield row
            next_url = parsed.get("next_url")
            if not next_url:
                break
            parsed_url = urlparse(str(next_url))
            page_id = parsed_url.path.strip("/").split("/")[-1]

    def _load_fighter_html(self, fighter_external_id: str) -> tuple[str, str]:
        if self.fixture_root is not None:
            path = self.fixture_root / "fighters" / f"{fighter_external_id}.html"
            if not path.is_file():
                path = self.fixture_root / f"fighter_{fighter_external_id}.html"
            if not path.is_file():
                raise FileNotFoundError(path)
            text = path.read_text(encoding="utf-8")
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            return text, digest
        if self.client is None:
            raise RuntimeError("live client required when fixture_root is unset")
        return self.client.fetch_fighter(fighter_external_id)
