"""Escalation backlog visibility (round 7 WP-4b, item 8).

``/healthz`` has carried ``escalationBacklog`` since audit F079 — as a number OUTSIDE
``degraded``, so nothing gated on it and nothing alarmed: a queue of unheard pages was
a field an operator had to remember to read. Two seams here:

* ``degraded_entry`` — ``escalation_backlog:<n>`` for the ``degraded`` list once the
  queue has held escalation pages for longer than one deferred-sweep interval: the
  sweep has had its turn and the pages are still unheard. The "since" is
  ``oldestQueuedAt`` from ``platform_escalation_backlog()`` (migration 0069) — the
  persisted moment the current backlog began — never a timestamp kept in a process,
  which every task would keep differently and every deploy would reset.
* ``emit_backlog_metric`` — the ``EscalationBacklog`` datum the deferred sweep
  publishes after its run (the ObservabilityStack alarms on it, dimension set
  ``[Environment]`` like every other EMF metric). An unknown backlog emits nothing:
  a missing datum is "not breaching", a fabricated 0 would read as all clear.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from app.observability.metrics import emit_metric
from app.runtime.integration_status import escalation_backlog

#: The deferred sweep's cadence — ``sweepSchedules`` ``deferred`` in
#: infra/lib/compute-stack.ts is ``cron(10 * * * ? *)``, hourly. A copy, so
#: tests/unit/test_escalation_backlog_health.py refuses a drift between the two.
DEFERRED_SWEEP_INTERVAL = timedelta(hours=1)
BACKLOG_METRIC = "EscalationBacklog"
DEGRADED_PREFIX = "escalation_backlog:"


def _queued_count(backlog: Mapping[str, object]) -> int:
    value = backlog.get("queued")
    return value if isinstance(value, int) else 0


def degraded_entry(
    backlog: Mapping[str, object] | None, *, now: datetime | None = None
) -> str | None:
    """``escalation_backlog:<queued>`` when the backlog is non-zero and has been for
    longer than one sweep interval; None for no backlog, a young one (the sweep has not
    had its turn yet) or an unknown one (``/healthz`` already says ``null`` for that)."""
    if not backlog:
        return None
    queued = _queued_count(backlog)
    since = backlog.get("oldestQueuedAt")
    if queued <= 0 or not isinstance(since, datetime):
        return None
    if since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    age = (now or datetime.now(UTC)) - since
    if age <= DEFERRED_SWEEP_INTERVAL:
        return None
    return f"{DEGRADED_PREFIX}{queued}"


async def emit_backlog_metric() -> int | None:
    """Publish ``EscalationBacklog`` = the platform-wide queued count and return it;
    None (and no datum) when the count could not be taken."""
    backlog = await escalation_backlog()
    if backlog is None:
        return None
    queued = _queued_count(backlog)
    emit_metric(BACKLOG_METRIC, float(queued))
    return queued
