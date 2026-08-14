"""Event look-ahead windows for weekly ticks. Kept import-light on purpose."""

from __future__ import annotations

from datetime import timedelta

# Official T-60 / due-job look-ahead from a production tick.
TICK_EVENT_HORIZON = timedelta(days=30)
# Preview publish may show a card that is still further out.
PREVIEW_EVENT_HORIZON = timedelta(days=120)

__all__ = ["PREVIEW_EVENT_HORIZON", "TICK_EVENT_HORIZON"]
