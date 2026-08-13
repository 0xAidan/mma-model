"""AsOfCutoff construction, proxy labeling, and shared-card enforcement."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from mma_model.features.as_of import (
    AsOfCutoff,
    CutoffKind,
    CutoffMismatchError,
    FeatureCutoffError,
    assert_identical_event_cutoffs,
    cutoff_for_event,
    observation_admitted,
)
from mma_model.features.builder import FeatureBuilder
from mma_model.features.snapshot import FeatureSnapshot, SnapshotEvent
from tests.features.helpers import add_bout, add_event, dt


def test_cutoff_is_sixty_minutes_before_scheduled_start() -> None:
    start = dt(2019, 6, 1, 2)
    event = SnapshotEvent("e1", start, start.date())
    cutoff = cutoff_for_event(event)
    assert cutoff.cutoff_kind is CutoffKind.SCHEDULED_MINUS_60M
    assert cutoff.cutoff == datetime(2019, 6, 1, 1, 0, tzinfo=UTC)
    assert cutoff.event_id == "e1"


def test_missing_start_fails_closed_without_proxy() -> None:
    event = SnapshotEvent("e1", None, None)
    with pytest.raises(FeatureCutoffError, match="refusing to invent"):
        cutoff_for_event(event)


def test_proxy_cutoff_is_labeled() -> None:
    event = SnapshotEvent("e1", None, dt(2019, 6, 1).date())
    cutoff = cutoff_for_event(event, allow_proxy=True)
    assert cutoff.cutoff_kind is CutoffKind.PROXY_SCHEDULED_START
    assert cutoff.cutoff == datetime(2019, 6, 1, 0, 0, tzinfo=UTC)


def test_shared_cutoff_mismatch_hard_fails() -> None:
    start = dt(2019, 6, 1, 2)
    a = AsOfCutoff("e1", start, CutoffKind.SCHEDULED_MINUS_60M)
    b = AsOfCutoff("e1", dt(2019, 6, 1, 3), CutoffKind.SCHEDULED_MINUS_60M)
    with pytest.raises(CutoffMismatchError, match="different cutoffs"):
        assert_identical_event_cutoffs([a, b])


def test_same_card_result_not_admitted_even_if_clocks_pass() -> None:
    start = dt(2019, 6, 1, 2)
    event = SnapshotEvent("e1", start, start.date())
    cutoff = cutoff_for_event(event)
    early = datetime(2019, 6, 1, 0, 30, tzinfo=UTC)
    assert observation_admitted(
        effective_at=early,
        observed_at=early,
        cutoff=cutoff,
        bout_event_id="other-event",
    )
    assert not observation_admitted(
        effective_at=early,
        observed_at=early,
        cutoff=cutoff,
        bout_event_id="e1",
    )


def test_builder_rejects_cutoff_mismatch_on_same_event() -> None:

    snapshot = FeatureSnapshot()
    start = dt(2019, 6, 1, 2)
    add_event(snapshot, "e1", start)
    add_bout(snapshot, "b1", "e1", "a", "b")
    add_bout(snapshot, "b2", "e1", "c", "d")
    builder = FeatureBuilder(snapshot)
    cutoff = cutoff_for_event(snapshot.events[0])
    builder.build("a", "b", cutoff, bout_id="b1")
    other = AsOfCutoff("e1", dt(2019, 6, 1, 0), CutoffKind.PROXY_SCHEDULED_START)
    with pytest.raises(CutoffMismatchError):
        builder.build("c", "d", other, bout_id="b2")
