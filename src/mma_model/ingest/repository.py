"""Idempotent ingest repository with bounded-batch checkpoints (DWCS-101/102)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mma_model.db.tables.core import BoutResultVersion
from mma_model.db.tables.provenance import IngestRun, RawObservation, SourceCheckpoint
from mma_model.history.apply import HISTORY_ENTITY_KINDS, apply_history_observation
from mma_model.ingest.raw_store import ContentAddressedRawStore, PayloadCorruptionError
from mma_model.sources.contracts import (
    DETAIL_LEVEL_RANK,
    DetailLevel,
    SourceObservationRecord,
)
from mma_model.sources.policy import load_source_policy

SessionFactory = Callable[[], Session]


class ReservedAttributeKeyError(ValueError):
    """Raised when attributes contain reserved contract keys."""


class NestedBatchTransactionError(RuntimeError):
    """Raised when a batch write would open a nested independent commit."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical_attributes_json(attributes: object) -> str:
    return json.dumps(attributes, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class BatchCommitResult:
    inserted: int
    skipped_identical: int
    skipped_downgrade: int
    skipped_preserve_version: int


class IngestRepository:
    """Write path for provenance + safe application of source-neutral records.

    Adapters must not touch tables directly; they return contracts consumed here.
    Each ``commit_batch`` is one SQLite transaction (not one network-long run).
    """

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session] | SessionFactory,
        raw_store: ContentAddressedRawStore,
    ) -> None:
        self._session_factory = session_factory
        self._raw_store = raw_store
        self._reserved_attribute_keys = frozenset(
            load_source_policy().observation_metadata.reserved_attribute_keys
        )
        self._active_owned_session: Session | None = None

    def start_run(self, *, source: str, stream: str, scope: str) -> IngestRun:
        with self._session_factory() as session:
            run = IngestRun(
                source=source,
                stream=stream,
                scope=scope,
                status="running",
                started_at=_utc_now(),
                observation_count=0,
            )
            session.add(run)
            session.commit()
            session.refresh(run)
            return IngestRun(
                id=run.id,
                source=run.source,
                stream=run.stream,
                scope=run.scope,
                status=run.status,
                started_at=run.started_at,
                finished_at=run.finished_at,
                error_class=run.error_class,
                error_message=run.error_message,
                observation_count=run.observation_count,
                created_at=run.created_at,
            )

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        error_class: str | None = None,
        error_message: str | None = None,
    ) -> None:
        if status not in {"succeeded", "failed"}:
            raise ValueError(f"invalid terminal status: {status}")
        with self._session_factory() as session:
            run = session.get(IngestRun, run_id)
            if run is None:
                raise KeyError(f"unknown ingest run {run_id}")
            run.status = status
            run.finished_at = _utc_now()
            run.error_class = error_class
            run.error_message = error_message
            session.commit()

    def apply_batch(
        self,
        session: Session,
        *,
        run_id: str,
        observations: Sequence[SourceObservationRecord],
        checkpoint_token: str,
        checkpoint_version: str,
        on_after_raw_observation: Callable[[SourceObservationRecord], None] | None = None,
        on_before_result_version: Callable[[SourceObservationRecord], None] | None = None,
    ) -> BatchCommitResult:
        """Write observations/checkpoint into a caller-owned session (no commit).

        Used by DWCS-103 so canonical entity writes and provenance land in one
        SQL transaction. Does not call ``session.commit()`` or ``rollback()``.
        """
        if self._active_owned_session is not None and self._active_owned_session is not session:
            raise NestedBatchTransactionError(
                "nested independent batch session is prohibited"
            )

        inserted = 0
        skipped_identical = 0
        skipped_downgrade = 0
        skipped_preserve_version = 0

        run = session.get(IngestRun, run_id)
        if run is None:
            raise KeyError(f"unknown ingest run {run_id}")

        pending_apply: list[tuple[SourceObservationRecord, RawObservation]] = []
        for obs in observations:
            self._validate_observation(obs)
            raw_ref = self._resolve_raw_ref(obs)

            existing = session.scalars(
                select(RawObservation).where(
                    RawObservation.source == obs.source,
                    RawObservation.stream == obs.stream,
                    RawObservation.scope == run.scope,
                    RawObservation.checkpoint_version == checkpoint_version,
                    RawObservation.external_id == obs.external_id,
                    RawObservation.payload_hash == obs.payload_hash,
                )
            ).first()
            if existing is not None:
                skipped_identical += 1
                continue

            raw_row = RawObservation(
                ingest_run_id=run.id,
                source=obs.source,
                stream=obs.stream,
                scope=run.scope,
                checkpoint_version=checkpoint_version,
                external_id=obs.external_id,
                entity_kind=obs.entity_kind,
                observed_at=obs.observed_at,
                effective_at=obs.effective_at,
                source_published_at=obs.source_published_at,
                source_updated_at=obs.source_updated_at,
                proxy_published_at=obs.proxy_published_at,
                timestamp_quality=obs.timestamp_quality,
                timestamp_quality_source=obs.timestamp_quality_source,
                quality_tier=obs.quality_tier,
                attributes_json=_canonical_attributes_json(dict(obs.attributes)),
                payload_hash=obs.payload_hash,
                raw_ref=raw_ref,
                detail_level=str(obs.detail_level),
                version_kind=obs.version_kind,
                schema_version=obs.schema_version,
                subject_id=obs.subject_id,
            )
            session.add(raw_row)
            inserted += 1
            pending_apply.append((obs, raw_row))

        if on_after_raw_observation is not None and pending_apply:
            on_after_raw_observation(pending_apply[-1][0])

        if pending_apply:
            session.flush()

        for obs, raw_row in pending_apply:
            if on_before_result_version is not None and obs.entity_kind == "bout_result":
                on_before_result_version(obs)
            apply_result = self._apply_observation(
                session, obs, raw_observation_id=raw_row.id
            )
            if apply_result == "downgrade":
                skipped_downgrade += 1
            elif apply_result == "preserve_version":
                skipped_preserve_version += 1

        self._upsert_checkpoint(
            session,
            source=run.source,
            stream=run.stream,
            scope=run.scope,
            version=checkpoint_version,
            cursor_token=checkpoint_token,
            run_id=run.id,
        )
        run.observation_count = int(run.observation_count or 0) + inserted

        return BatchCommitResult(
            inserted=inserted,
            skipped_identical=skipped_identical,
            skipped_downgrade=skipped_downgrade,
            skipped_preserve_version=skipped_preserve_version,
        )

    def commit_batch(
        self,
        *,
        run_id: str,
        observations: Sequence[SourceObservationRecord],
        checkpoint_token: str,
        checkpoint_version: str,
        session: Session | None = None,
    ) -> BatchCommitResult:
        """Apply a batch and commit.

        When ``session`` is omitted, opens a repository-owned session (DWCS-101).
        When ``session`` is provided, applies into that session and commits it —
        callers that need a wider atomic unit should use ``apply_batch`` instead
        and commit once themselves.
        """
        if session is not None:
            if self._active_owned_session is not None:
                raise NestedBatchTransactionError(
                    "commit_batch cannot nest inside another owned batch transaction"
                )
            result = self.apply_batch(
                session,
                run_id=run_id,
                observations=observations,
                checkpoint_token=checkpoint_token,
                checkpoint_version=checkpoint_version,
            )
            session.commit()
            return result

        if self._active_owned_session is not None:
            raise NestedBatchTransactionError(
                "commit_batch cannot open a nested independent commit while a "
                "caller-owned batch session is active"
            )

        with self._session_factory() as owned:
            result = self.apply_batch(
                owned,
                run_id=run_id,
                observations=observations,
                checkpoint_token=checkpoint_token,
                checkpoint_version=checkpoint_version,
            )
            owned.commit()
            return result

    def begin_owned_batch(self, session: Session) -> None:
        """Mark ``session`` as the active caller-owned batch transaction."""
        if self._active_owned_session is not None:
            raise NestedBatchTransactionError(
                "caller-owned batch session already active"
            )
        self._active_owned_session = session

    def end_owned_batch(self, session: Session) -> None:
        if self._active_owned_session is session:
            self._active_owned_session = None

    def _validate_observation(self, obs: SourceObservationRecord) -> None:
        collisions = sorted(
            key for key in obs.attributes if key in self._reserved_attribute_keys
        )
        if collisions:
            raise ReservedAttributeKeyError(
                f"attributes contain reserved contract key(s): {collisions}"
            )
        if obs.detail_level != DetailLevel.VERIFIED:
            return
        missing: list[str] = []
        if not obs.timestamp_quality_source:
            missing.append("timestamp_quality_source")
        if obs.timestamp_quality == "unknown":
            missing.append("timestamp_quality")
        if missing:
            raise ValueError(
                "verified detail requires metadata fields: " + ", ".join(missing)
            )

    def _resolve_raw_ref(self, obs: SourceObservationRecord) -> str | None:
        """Enforce verified-blob invariant or explicit absence (no dangling raw_ref)."""
        if obs.raw_blob_absent:
            return None
        claimed = obs.raw_ref or obs.payload_hash
        if claimed != obs.payload_hash:
            raise PayloadCorruptionError(
                f"raw_ref {claimed} does not match payload_hash {obs.payload_hash}"
            )
        if not self._raw_store.exists(obs.payload_hash):
            raise PayloadCorruptionError(
                f"missing raw payload for hash {obs.payload_hash}"
            )
        self._raw_store.verify(obs.payload_hash)
        return obs.payload_hash

    def get_checkpoint(
        self, *, source: str, stream: str, scope: str, version: str
    ) -> SourceCheckpoint | None:
        with self._session_factory() as session:
            row = session.scalars(
                select(SourceCheckpoint).where(
                    SourceCheckpoint.source == source,
                    SourceCheckpoint.stream == stream,
                    SourceCheckpoint.scope == scope,
                    SourceCheckpoint.version == version,
                )
            ).first()
            if row is None:
                return None
            return SourceCheckpoint(
                id=row.id,
                source=row.source,
                stream=row.stream,
                scope=row.scope,
                version=row.version,
                cursor_token=row.cursor_token,
                last_ingest_run_id=row.last_ingest_run_id,
                updated_at=row.updated_at,
            )

    def _upsert_checkpoint(
        self,
        session: Session,
        *,
        source: str,
        stream: str,
        scope: str,
        version: str,
        cursor_token: str,
        run_id: str,
    ) -> None:
        row = session.scalars(
            select(SourceCheckpoint).where(
                SourceCheckpoint.source == source,
                SourceCheckpoint.stream == stream,
                SourceCheckpoint.scope == scope,
                SourceCheckpoint.version == version,
            )
        ).first()
        if row is None:
            session.add(
                SourceCheckpoint(
                    source=source,
                    stream=stream,
                    scope=scope,
                    version=version,
                    cursor_token=cursor_token,
                    last_ingest_run_id=run_id,
                    updated_at=_utc_now(),
                )
            )
            return
        row.cursor_token = cursor_token
        row.last_ingest_run_id = run_id
        row.updated_at = _utc_now()

    def _apply_observation(
        self,
        session: Session,
        obs: SourceObservationRecord,
        *,
        raw_observation_id: int | None = None,
    ) -> str | None:
        if obs.entity_kind in HISTORY_ENTITY_KINDS:
            return apply_history_observation(session, obs)
        if obs.entity_kind != "bout_result" or not obs.subject_id or not obs.version_kind:
            return None

        attrs = dict(obs.attributes)
        fighter_a_id = attrs.get("fighter_a_id")
        fighter_b_id = attrs.get("fighter_b_id")
        if not isinstance(fighter_a_id, str) or not isinstance(fighter_b_id, str):
            raise ValueError("bout_result observations require fighter_a_id and fighter_b_id")

        latest = session.scalars(
            select(BoutResultVersion)
            .where(
                BoutResultVersion.bout_id == obs.subject_id,
                BoutResultVersion.version_kind == obs.version_kind,
            )
            .order_by(BoutResultVersion.revision.desc())
        ).first()

        if latest is not None:
            prior_detail = self._latest_applied_detail(
                session,
                bout_id=obs.subject_id,
                version_kind=obs.version_kind,
                excluding_hash=obs.payload_hash,
            )
            existing_level = prior_detail or self._infer_detail(latest)
            if DETAIL_LEVEL_RANK[obs.detail_level] < DETAIL_LEVEL_RANK[existing_level]:
                return "downgrade"

        next_revision = 1
        if latest is not None:
            next_revision = int(latest.revision) + 1

        session.add(
            BoutResultVersion(
                bout_id=obs.subject_id,
                version_kind=obs.version_kind,
                revision=next_revision,
                fighter_a_id=fighter_a_id,
                fighter_b_id=fighter_b_id,
                winner_fighter_id=attrs.get("winner_fighter_id"),
                result_type=attrs.get("result_type"),
                method=attrs.get("method"),
                ending_round=attrs.get("ending_round"),
                time_str=attrs.get("time_str"),
                effective_at=obs.effective_at,
                observed_at=obs.observed_at,
                raw_observation_id=raw_observation_id,
                provenance_status="linked" if raw_observation_id is not None else "unknown",
            )
        )
        return None

    def _latest_applied_detail(
        self,
        session: Session,
        *,
        bout_id: str,
        version_kind: str,
        excluding_hash: str,
    ) -> DetailLevel | None:
        rows = session.scalars(
            select(RawObservation)
            .where(
                RawObservation.subject_id == bout_id,
                RawObservation.version_kind == version_kind,
                RawObservation.entity_kind == "bout_result",
                RawObservation.payload_hash != excluding_hash,
            )
            .order_by(RawObservation.id.desc())
        ).all()
        if not rows:
            return None
        best = DetailLevel.SUMMARY
        for row in rows:
            level = DetailLevel(row.detail_level)
            if DETAIL_LEVEL_RANK[level] > DETAIL_LEVEL_RANK[best]:
                best = level
        return best

    @staticmethod
    def _infer_detail(row: BoutResultVersion) -> DetailLevel:
        if row.method and row.ending_round is not None and row.time_str:
            return DetailLevel.VERIFIED
        if row.method or row.ending_round is not None or row.time_str:
            return DetailLevel.PARTIAL
        return DetailLevel.SUMMARY
