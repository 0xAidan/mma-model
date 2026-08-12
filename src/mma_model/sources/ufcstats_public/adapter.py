"""UFCStats public adapter: fixtures/live orchestration (DWCS-102)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator, Mapping

from mma_model.sources.contracts import SourceObservationRecord
from mma_model.sources.http.block_signals import SourceBlockedError
from mma_model.sources.pit_proxy import PitProxyRule, load_pit_proxy_rule
from mma_model.sources.ufcstats_public.client import UfcstatsPublicClient
from mma_model.sources.ufcstats_public.errors import (
    ParserSchemaDriftError,
    ParticipantError,
)
from mma_model.sources.ufcstats_public.mapper import map_fight_to_observations
from mma_model.sources.ufcstats_public.parser import (
    SOURCE_UFCSTATS_PUBLIC,
    parse_event_details,
    parse_fight_details,
)

if TYPE_CHECKING:
    from mma_model.ingest.raw_store import ContentAddressedRawStore

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_EVENTS_MANIFEST = REPO_ROOT / "data" / "manifests" / "dwcs_events_v1.jsonl"
DEFAULT_BOUTS_MANIFEST = REPO_ROOT / "data" / "manifests" / "dwcs_bouts_v1.jsonl"

COMPLETED_STATUSES = frozenset({"completed", "occurred"})
CANCELLED_STATUSES = frozenset({"cancelled", "canceled"})


@dataclass(frozen=True)
class AuditRow:
    kind: str  # event | bout
    entity_id: str
    status: str  # present | missing | blocked | unresolved | schema_drift
    detail: str | None = None


def classify_event_page(
    *,
    parsed: dict[str, Any],
    manifest_status: str | None,
) -> tuple[str, str]:
    """Classify a parsed event page against manifest expectations (fail closed)."""
    status = (manifest_status or "completed").strip().lower()
    event_name = str(parsed.get("event_name") or "").strip()
    date_text = str(parsed.get("date_text") or "").strip()
    fights = list(parsed.get("fights") or [])
    cancelled_evidence = bool(parsed.get("cancelled_evidence"))

    if not event_name or not date_text:
        return "schema_drift", "missing_event_card_structure"

    if status in CANCELLED_STATUSES:
        if len(fights) == 0 and cancelled_evidence:
            return "present", "cancelled_zero_bout"
        if len(fights) == 0 and not cancelled_evidence:
            return "schema_drift", "cancelled_without_parser_evidence"
        return "present", "cancelled_with_fights"

    if status in COMPLETED_STATUSES:
        if len(fights) == 0:
            return "schema_drift", "completed_event_empty_fights"
        for fight in fights:
            if not fight.get("external_fight_id"):
                return "schema_drift", "fight_row_missing_external_id"
            if not fight.get("fighter_a", {}).get("id") or not fight.get(
                "fighter_b", {}
            ).get("id"):
                return "schema_drift", "fight_row_missing_participant_ids"
        return "present", "completed_with_fights"

    if len(fights) == 0:
        return "unresolved", f"unknown_manifest_status:{status}"
    return "present", f"status:{status}"


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
        event_effective_at_by_id: Mapping[str, datetime] | None = None,
    ) -> Iterator[SourceObservationRecord]:
        if observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware UTC")
        manifest_effective = event_effective_at_by_id or {}
        for event_id in event_external_ids:
            event_html, _event_hash = self._load_event_html(event_id)
            if self.raw_store is not None:
                self.raw_store.put(event_html.encode("utf-8"))
            event = parse_event_details(event_html)
            effective_at = self._resolve_effective_at(
                event_id=event_id,
                event=event,
                manifest_effective=manifest_effective,
            )
            for fight in event.get("fights", []):
                fight_id = str(fight["external_fight_id"])
                fight_html, fight_hash = self._load_fight_html(fight_id)
                if self.raw_store is not None:
                    self.raw_store.put(fight_html.encode("utf-8"))
                parsed = parse_fight_details(fight_html)
                parsed_id = str(parsed.get("external_fight_id") or "")
                if parsed_id != fight_id:
                    raise ParserSchemaDriftError(
                        f"fight id mismatch listing={fight_id!r} details={parsed_id!r}"
                    )
                yield from map_fight_to_observations(
                    parsed=parsed,
                    observed_at=observed_at,
                    effective_at=effective_at,
                    source_published_at=None,
                    source_updated_at=None,
                    proxy=self.proxy,
                    payload_hash=fight_hash,
                )

    @staticmethod
    def _resolve_effective_at(
        *,
        event_id: str,
        event: dict[str, Any],
        manifest_effective: Mapping[str, datetime],
    ) -> datetime:
        """Derive effective_at from manifest override or parsed event date.

        Never invents a timestamp. Missing/unparseable dates are schema drift.
        """
        override = manifest_effective.get(event_id)
        if override is not None:
            if override.tzinfo is None:
                raise ValueError(
                    f"event_effective_at_by_id[{event_id!r}] must be timezone-aware UTC"
                )
            return override.astimezone(timezone.utc)
        event_date = event.get("event_date")
        if isinstance(event_date, datetime):
            if event_date.tzinfo is None:
                raise ParserSchemaDriftError(
                    f"event_date for {event_id!r} missing timezone"
                )
            return event_date.astimezone(timezone.utc)
        date_text = str(event.get("date_text") or "").strip()
        raise ParserSchemaDriftError(
            f"missing event_date/effective_at for event {event_id!r} "
            f"(date_text={date_text!r}); refuse fabricated timestamp"
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
                status, detail = classify_event_page(
                    parsed=parsed,
                    manifest_status=str(event.get("status") or "completed"),
                )
                event_rows.append(
                    AuditRow("event", event_id, status, f"{ufcstats_id}:{detail}")
                )
            except SourceBlockedError as exc:
                blocked = True
                block_reason = exc.reason
                event_rows.append(AuditRow("event", event_id, "blocked", exc.reason))
            except FileNotFoundError:
                event_rows.append(
                    AuditRow("event", event_id, "missing", str(ufcstats_id))
                )
            except (ParserSchemaDriftError, ParticipantError) as exc:
                event_rows.append(
                    AuditRow("event", event_id, "schema_drift", str(exc))
                )
            except Exception as exc:  # noqa: BLE001 - classify remainder as unresolved
                event_rows.append(
                    AuditRow(
                        "event",
                        event_id,
                        "unresolved",
                        f"{type(exc).__name__}:{exc}",
                    )
                )

        for bout in sorted(scoped_bouts, key=lambda r: str(r["bout_id"])):
            bout_id = str(bout["bout_id"])
            ufcstats_bout = bout.get("ufcstats_bout_id") or (
                (bout.get("source_ids") or {}).get("ufcstats_bout_id")
            )
            if blocked:
                bout_rows.append(AuditRow("bout", bout_id, "blocked", block_reason))
                continue
            if not ufcstats_bout:
                bout_rows.append(
                    AuditRow("bout", bout_id, "unresolved", "ufcstats_bout_id_unmapped")
                )
                continue
            try:
                html, _digest = self._load_fight_html(str(ufcstats_bout))
                parsed = parse_fight_details(html)
                parsed_id = str(parsed.get("external_fight_id") or "")
                if parsed_id != str(ufcstats_bout):
                    bout_rows.append(
                        AuditRow(
                            "bout",
                            bout_id,
                            "schema_drift",
                            f"id_mismatch expected={ufcstats_bout} got={parsed_id}",
                        )
                    )
                else:
                    bout_rows.append(
                        AuditRow("bout", bout_id, "present", str(ufcstats_bout))
                    )
            except SourceBlockedError as exc:
                blocked = True
                block_reason = exc.reason
                bout_rows.append(AuditRow("bout", bout_id, "blocked", exc.reason))
            except FileNotFoundError:
                bout_rows.append(
                    AuditRow("bout", bout_id, "missing", str(ufcstats_bout))
                )
            except (ParserSchemaDriftError, ParticipantError) as exc:
                bout_rows.append(AuditRow("bout", bout_id, "schema_drift", str(exc)))
            except Exception as exc:  # noqa: BLE001
                bout_rows.append(
                    AuditRow(
                        "bout",
                        bout_id,
                        "unresolved",
                        f"{type(exc).__name__}:{exc}",
                    )
                )

        def _counts(rows: list[AuditRow]) -> dict[str, int]:
            out = {
                "present": 0,
                "missing": 0,
                "blocked": 0,
                "unresolved": 0,
                "schema_drift": 0,
            }
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
        assert len(report["event_classifications"]) == len(scoped_events)
        assert len(report["bout_classifications"]) == len(scoped_bouts)
        return report

    def _load_event_html(self, event_external_id: str) -> tuple[str, str]:
        if self.fixture_root is not None:
            path = self.fixture_root / "events" / f"{event_external_id}.html"
            if not path.is_file():
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
