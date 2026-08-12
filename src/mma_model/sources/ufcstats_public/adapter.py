"""UFCStats public adapter: fixtures/live orchestration (DWCS-102)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from mma_model.sources.contracts import SourceObservationRecord
from mma_model.sources.http.block_signals import SourceBlockedError
from mma_model.sources.pit_proxy import PitProxyRule, load_pit_proxy_rule
from mma_model.sources.ufcstats_public.client import UfcstatsPublicClient
from mma_model.sources.ufcstats_public.mapper import map_fight_to_observations
from mma_model.sources.ufcstats_public.parser import (
    SOURCE_UFCSTATS_PUBLIC,
    parse_event_details,
    parse_fight_details,
)

if TYPE_CHECKING:
    from mma_model.ingest.raw_store import ContentAddressedRawStore

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_EVENTS_MANIFEST = REPO_ROOT / "data" / "manifests" / "dwcs_events_v1.jsonl"
DEFAULT_BOUTS_MANIFEST = REPO_ROOT / "data" / "manifests" / "dwcs_bouts_v1.jsonl"


@dataclass(frozen=True)
class AuditRow:
    kind: str  # event | bout
    entity_id: str
    status: str  # present | missing | blocked | unresolved
    detail: str | None = None


class UfcstatsPublicAdapter:
    """Orchestrate UFCStats fetch/parse/map without writing ORM tables."""

    def __init__(
        self,
        *,
        fixture_root: Path | None = None,
        client: UfcstatsPublicClient | None = None,
        raw_store: ContentAddressedRawStore | None = None,
        proxy: PitProxyRule | None = None,
        events_manifest: Path | None = None,
        bouts_manifest: Path | None = None,
    ) -> None:
        self.fixture_root = fixture_root
        self.client = client
        self.raw_store = raw_store
        self.proxy = proxy if proxy is not None else load_pit_proxy_rule()
        self.events_manifest = events_manifest or DEFAULT_EVENTS_MANIFEST
        self.bouts_manifest = bouts_manifest or DEFAULT_BOUTS_MANIFEST
        self._block_reason: str | None = None

    @classmethod
    def for_fixtures(
        cls,
        *,
        fixture_root: Path,
        raw_store: ContentAddressedRawStore | None = None,
    ) -> UfcstatsPublicAdapter:
        return cls(fixture_root=fixture_root, client=None, raw_store=raw_store)

    def iter_observations(
        self,
        *,
        event_external_ids: list[str],
        observed_at: datetime,
    ) -> Iterator[SourceObservationRecord]:
        if observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware UTC")
        for event_id in event_external_ids:
            event_html, event_hash = self._load_event_html(event_id)
            if self.raw_store is not None:
                self.raw_store.put(event_html.encode("utf-8"))
            event = parse_event_details(event_html)
            for fight in event.get("fights", []):
                fight_id = str(fight["external_fight_id"])
                fight_html, fight_hash = self._load_fight_html(fight_id)
                if self.raw_store is not None:
                    self.raw_store.put(fight_html.encode("utf-8"))
                parsed = parse_fight_details(fight_html)
                # Prefer explicit fight id from listing when fixture omits it.
                if not parsed.get("external_fight_id") or parsed["external_fight_id"] == "unknown":
                    parsed["external_fight_id"] = fight_id
                effective_at = observed_at
                if effective_at.year >= 2020:
                    effective_at = datetime(2019, 1, 1, tzinfo=timezone.utc)
                yield from map_fight_to_observations(
                    parsed=parsed,
                    observed_at=observed_at,
                    effective_at=effective_at,
                    source_published_at=None,
                    source_updated_at=None,
                    proxy=self.proxy,
                    payload_hash=fight_hash,
                )

    def audit_manifest_scope(self, *, years: range) -> dict[str, object]:
        """Classify every DWCS event/bout in scope; no silent omissions."""
        events = self._load_jsonl(self.events_manifest)
        bouts = self._load_jsonl(self.bouts_manifest)
        year_set = set(years)
        scoped_events = [
            row
            for row in events
            if int(row.get("calendar_year", -1)) in year_set
        ]
        scoped_event_ids = {str(row["event_id"]) for row in scoped_events}
        scoped_bouts = [
            row for row in bouts if str(row.get("event_id")) in scoped_event_ids
        ]

        event_rows: list[AuditRow] = []
        bout_rows: list[AuditRow] = []
        blocked = False
        block_reason: str | None = None

        for event in sorted(scoped_events, key=lambda r: str(r["event_id"])):
            event_id = str(event["event_id"])
            ufcstats_id = event.get("ufcstats_event_id") or (
                (event.get("source_ids") or {}).get("ufcstats_event_id")
            )
            if not ufcstats_id:
                event_rows.append(
                    AuditRow("event", event_id, "unresolved", "ufcstats_event_id_unmapped")
                )
                continue
            try:
                html, _digest = self._load_event_html(str(ufcstats_id))
                parsed = parse_event_details(html)
                status = "present" if parsed.get("fights") is not None else "missing"
                event_rows.append(AuditRow("event", event_id, status, str(ufcstats_id)))
            except SourceBlockedError as exc:
                blocked = True
                block_reason = exc.reason
                event_rows.append(
                    AuditRow("event", event_id, "blocked", exc.reason)
                )
            except FileNotFoundError:
                event_rows.append(
                    AuditRow("event", event_id, "missing", str(ufcstats_id))
                )
            except Exception as exc:  # noqa: BLE001 - classify remainder as missing
                event_rows.append(
                    AuditRow("event", event_id, "missing", f"{type(exc).__name__}:{exc}")
                )

        for bout in sorted(scoped_bouts, key=lambda r: str(r["bout_id"])):
            bout_id = str(bout["bout_id"])
            ufcstats_bout = bout.get("ufcstats_bout_id") or (
                (bout.get("source_ids") or {}).get("ufcstats_bout_id")
            )
            if blocked:
                bout_rows.append(
                    AuditRow("bout", bout_id, "blocked", block_reason)
                )
                continue
            if not ufcstats_bout:
                bout_rows.append(
                    AuditRow("bout", bout_id, "unresolved", "ufcstats_bout_id_unmapped")
                )
                continue
            try:
                html, _digest = self._load_fight_html(str(ufcstats_bout))
                parsed = parse_fight_details(html)
                status = "present" if parsed.get("external_fight_id") else "missing"
                bout_rows.append(AuditRow("bout", bout_id, status, str(ufcstats_bout)))
            except SourceBlockedError as exc:
                blocked = True
                block_reason = exc.reason
                bout_rows.append(AuditRow("bout", bout_id, "blocked", exc.reason))
            except FileNotFoundError:
                bout_rows.append(
                    AuditRow("bout", bout_id, "missing", str(ufcstats_bout))
                )
            except Exception as exc:  # noqa: BLE001
                bout_rows.append(
                    AuditRow("bout", bout_id, "missing", f"{type(exc).__name__}:{exc}")
                )

        def _counts(rows: list[AuditRow]) -> dict[str, int]:
            out = {"present": 0, "missing": 0, "blocked": 0, "unresolved": 0}
            for row in rows:
                out[row.status] = out.get(row.status, 0) + 1
            return out

        report = {
            "source": SOURCE_UFCSTATS_PUBLIC,
            "years": {"start": years.start, "stop": years.stop - 1},
            "events_total": len(event_rows),
            "bouts_total": len(bout_rows),
            "events": _counts(event_rows),
            "bouts": _counts(bout_rows),
            "blocked": blocked,
            "block_reason": block_reason,
            "event_classifications": [
                {
                    "entity_id": r.entity_id,
                    "status": r.status,
                    "detail": r.detail,
                }
                for r in event_rows
            ],
            "bout_classifications": [
                {
                    "entity_id": r.entity_id,
                    "status": r.status,
                    "detail": r.detail,
                }
                for r in bout_rows
            ],
        }
        # Fail closed: every scoped entity must appear exactly once.
        assert len(report["event_classifications"]) == len(scoped_events)
        assert len(report["bout_classifications"]) == len(scoped_bouts)
        return report

    def _load_event_html(self, event_external_id: str) -> tuple[str, str]:
        if self.fixture_root is not None:
            path = self.fixture_root / "events" / f"{event_external_id}.html"
            if not path.is_file():
                # Flat layout fallback used by tests.
                path = self.fixture_root / f"event_{event_external_id}.html"
            if not path.is_file():
                raise FileNotFoundError(path)
            text = path.read_text(encoding="utf-8")
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            return text, digest
        if self.client is None:
            raise RuntimeError("live client required when fixture_root is unset")
        return self.client.fetch_event_details(event_external_id)

    def _load_fight_html(self, fight_external_id: str) -> tuple[str, str]:
        if self.fixture_root is not None:
            path = self.fixture_root / "fights" / f"{fight_external_id}.html"
            if not path.is_file():
                path = self.fixture_root / f"fight_{fight_external_id}.html"
            if not path.is_file():
                raise FileNotFoundError(path)
            text = path.read_text(encoding="utf-8")
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            return text, digest
        if self.client is None:
            raise RuntimeError("live client required when fixture_root is unset")
        return self.client.fetch_fight_details(fight_external_id)

    @staticmethod
    def _load_jsonl(path: Path) -> list[dict]:
        rows: list[dict] = []
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                rows.append(json.loads(line))
        return rows
