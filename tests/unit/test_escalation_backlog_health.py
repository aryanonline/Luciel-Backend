"""Escalation backlog visibility (round 7 WP-4b, item 8).

``/healthz`` reported ``escalationBacklog`` as a number outside ``degraded`` and nothing
alarmed on it. Now a backlog that has outlived one deferred-sweep interval is a
``degraded`` entry (``escalation_backlog:<n>``) whose "since" is the persisted
``oldestQueuedAt`` (migration 0069) rather than a per-process clock, and the sweep
publishes ``EscalationBacklog`` for the ObservabilityStack alarm. The integration half
(the real SQL function, the real endpoint, the real sweep) is in
tests/integration/test_escalation_delivery_truth.py.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.notifications import backlog
from app.notifications.backlog import DEFERRED_SWEEP_INTERVAL, degraded_entry

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
_COMPUTE_STACK = Path(__file__).resolve().parents[2] / "infra" / "lib" / "compute-stack.ts"


def test_no_backlog_or_an_unknown_one_is_not_degraded() -> None:
    assert degraded_entry(None, now=NOW) is None
    assert degraded_entry({"queued": 0, "failed24h": 3, "oldestQueuedAt": None}, now=NOW) is None


def test_a_backlog_younger_than_one_sweep_interval_is_not_degraded_yet() -> None:
    """The sweep has not had its turn: a page queued a minute ago is normal, and so is
    one queued exactly one interval ago (the sweep fires at the boundary)."""
    young = {"queued": 2, "failed24h": 0, "oldestQueuedAt": NOW - timedelta(minutes=1)}
    assert degraded_entry(young, now=NOW) is None
    boundary = {"queued": 2, "failed24h": 0, "oldestQueuedAt": NOW - DEFERRED_SWEEP_INTERVAL}
    assert degraded_entry(boundary, now=NOW) is None


def test_a_backlog_older_than_one_sweep_interval_is_degraded_by_count() -> None:
    stale = {
        "queued": 3,
        "failed24h": 1,
        "oldestQueuedAt": NOW - DEFERRED_SWEEP_INTERVAL - timedelta(seconds=1),
    }
    assert degraded_entry(stale, now=NOW) == "escalation_backlog:3"


def test_a_naive_since_is_read_as_utc() -> None:
    since = (NOW - timedelta(hours=2)).replace(tzinfo=None)
    assert degraded_entry({"queued": 1, "oldestQueuedAt": since}, now=NOW) == "escalation_backlog:1"


def test_the_interval_is_the_deferred_schedule_in_cdk() -> None:
    """DEFERRED_SWEEP_INTERVAL is a copy of infra's cadence; this refuses a drift the
    way test_sweep_scheduling.py refuses an unscheduled sweep."""
    source = _COMPUTE_STACK.read_text(encoding="utf-8")
    match = re.search(r"name:\s*'deferred',\s*cron:\s*'cron\(([^)]*)\)'", source)
    assert match, f"deferred schedule not found in {_COMPUTE_STACK}"
    minute, hour, *_rest = match.group(1).split()
    assert minute.isdigit() and hour == "*", match.group(1)  # once an hour, fixed minute
    assert DEFERRED_SWEEP_INTERVAL == timedelta(hours=1)


async def test_the_sweep_metric_carries_the_queued_count(monkeypatch, capsys) -> None:
    async def _backlog() -> dict[str, object]:
        return {"queued": 4, "failed24h": 2, "oldestQueuedAt": NOW}

    monkeypatch.setattr(backlog, "escalation_backlog", _backlog)
    assert await backlog.emit_backlog_metric() == 4
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["EscalationBacklog"] == 4.0
    envelope = line["_aws"]["CloudWatchMetrics"][0]
    assert envelope["Namespace"] == "Luciel"
    assert envelope["Metrics"] == [{"Name": "EscalationBacklog", "Unit": "Count"}]
    # The [Environment] set the alarm reads, and only that (round 7 WP-8).
    assert envelope["Dimensions"] == [["Environment"]]


async def test_an_unknown_backlog_emits_nothing_rather_than_zero(monkeypatch, capsys) -> None:
    """A missing datum is not-breaching; a fabricated 0 would read as all clear."""

    async def _unknown() -> None:
        return None

    monkeypatch.setattr(backlog, "escalation_backlog", _unknown)
    assert await backlog.emit_backlog_metric() is None
    assert "EscalationBacklog" not in capsys.readouterr().out
