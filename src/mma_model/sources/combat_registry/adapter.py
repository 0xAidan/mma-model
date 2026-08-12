"""Combat Registry public adapter: authoritative validation only (DWCS-105)."""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from mma_model.history.access import detect_access_kill
from mma_model.sources.combat_registry.client import BASE_HOST, CombatRegistryPublicClient
from mma_model.sources.combat_registry.mapper import map_results_to_observations
from mma_model.sources.combat_registry.parser import parse_results_page
from mma_model.sources.contracts import SourceObservationRecord
from mma_model.sources.http.block_signals import SourceBlockedError
from mma_model.sources.pit_proxy import PitProxyRule, load_pit_proxy_rule

if TYPE_CHECKING:
    from mma_model.ingest.raw_store import ContentAddressedRawStore


class CombatRegistryPublicAdapter:
    """Public/unauthenticated results only. Login walls kill the source."""

    def __init__(
        self,
        *,
        fixture_root: Path | None = None,
        client: CombatRegistryPublicClient | None = None,
        raw_store: ContentAddressedRawStore | None = None,
        proxy: PitProxyRule | None = None,
    ) -> None:
        self.fixture_root = fixture_root
        self.client = client
        self.raw_store = raw_store
        self.proxy = proxy if proxy is not None else load_pit_proxy_rule()
        self.killed_reason: str | None = None

    @classmethod
    def for_fixtures(
        cls,
        *,
        fixture_root: Path,
        raw_store: ContentAddressedRawStore | None = None,
    ) -> CombatRegistryPublicAdapter:
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
        html, digest = self._load_results_html(fighter_external_id)
        login = detect_access_kill(html)
        if login:
            self.killed_reason = login
            raise SourceBlockedError(login, host=BASE_HOST)
        if self.raw_store is not None:
            self.raw_store.put(html.encode("utf-8"))
        parsed = parse_results_page(html)
        if self.fixture_root is not None:
            parsed["observation_origin"] = "synthetic_fixture"
            parsed["parser_mode"] = parsed.get("parser_mode") or "synthetic_contract"
        else:
            parsed["observation_origin"] = parsed.get("observation_origin") or "live_public"
        parsed["fighter_external_id"] = fighter_external_id
        yield from map_results_to_observations(
            parsed=parsed,
            observed_at=observed_at,
            payload_hash=digest,
            proxy=self.proxy,
            identity_status=identity_status,
            fighter_canonical_id=fighter_canonical_id,
        )

    def _load_results_html(self, results_external_id: str) -> tuple[str, str]:
        if self.fixture_root is not None:
            path = self.fixture_root / "results" / f"{results_external_id}.html"
            if not path.is_file():
                path = self.fixture_root / f"results_{results_external_id}.html"
            if not path.is_file():
                raise FileNotFoundError(path)
            text = path.read_text(encoding="utf-8")
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            return text, digest
        if self.client is None:
            raise RuntimeError("live client required when fixture_root is unset")
        return self.client.fetch_results(results_external_id)
