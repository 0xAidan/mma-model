"""Job-style publish passes as_of into dashboard JSON (DWCS-500)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from mma_model.domain.markets import RecommendationState
from mma_model.jobs.handlers import handle_publish
from mma_model.jobs.types import DueJob, EventContext, JobStatus, JobType
from mma_model.observability.publish_guard import FilesystemPublishPointer
from mma_model.publish.constants import CURRENT_EVENT_JSON
from tests.publish.helpers import open_publish_session, seed_publication

FIXED_AS_OF = datetime(2026, 8, 11, 18, 30, 0, tzinfo=UTC)


def test_handle_publish_writes_as_of_into_current_event(tmp_path: Path) -> None:
    session, engine = open_publish_session(tmp_path)
    try:
        seed_publication(
            session,
            bout_id="bout-asof",
            state=RecommendationState.PRICE_TARGET,
        )
        root = tmp_path / "publish"
        job = DueJob(
            job_type=JobType.PUBLISH,
            idempotency_key="publish:evt-1:t60",
            dependencies=(),
            event_id="evt-1",
            window_slot="t60",
        )
        events = (
            EventContext(
                event_id="evt-1",
                event_start=FIXED_AS_OF,
                bout_ids=("bout-asof",),
                series="dwcs",
            ),
        )
        result = handle_publish(
            session,
            job=job,
            as_of=FIXED_AS_OF,
            events=events,
            context={"publish_root": str(root)},
        )
        assert result.status is JobStatus.SUCCESS
        pointer = FilesystemPublishPointer(root)
        release_id = pointer.current_release_id
        assert release_id is not None
        payload = json.loads(
            (root / "releases" / release_id / CURRENT_EVENT_JSON).read_text(
                encoding="utf-8"
            )
        )
        assert payload["as_of"] == "2026-08-11T18:30:00Z"
        assert payload["last_successful_update_at"]["value"] == "2026-08-11T18:30:00Z"
    finally:
        session.close()
        engine.dispose()
