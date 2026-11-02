"""The sweep process lets the owner notifications it scheduled finish (round 7
phase 2 review, seams 2).

A sweep that writes an escalation row only queues its notification: the task is
created by the after-commit hook and nothing in the sweep awaits it. The entry point
used to be a bare ``asyncio.run(sweep())``, which closes the loop the moment the sweep
returns and cancels every task still pending — so the session-close "a booking
stalled" page was cancelled before it opened its database session, on the last tenant
of every hourly run.

These tests drive the real entry point (``python -m app.jobs <sweep>`` is
``main([sweep])``) on a worker thread, so ``asyncio.run`` owns a loop of its own and
its exit behaves as the process's does. The notification is registered exactly the
way ``escalation_dispatch._start_notifications_after_commit`` registers it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Coroutine
from typing import Any

import pytest
from app.jobs import __main__ as jobs_main
from app.jobs.sweeps import SESSION_FINALIZE, SWEEPS
from app.notifications import escalation_dispatch

pytestmark = [pytest.mark.asyncio]


def _schedule(
    coro: Coroutine[Any, Any, None], registry: set[asyncio.Task[None]] | None = None
) -> asyncio.Task[None]:
    registry = escalation_dispatch._BACKGROUND_NOTIFICATIONS if registry is None else registry
    task = asyncio.get_running_loop().create_task(coro)
    registry.add(task)
    task.add_done_callback(registry.discard)
    return task


async def _run_entry_point() -> int:
    return await asyncio.wait_for(asyncio.to_thread(jobs_main.main, [SESSION_FINALIZE]), timeout=30)


async def test_a_notification_the_sweep_scheduled_completes_before_the_process_exits(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    delivered: list[str] = []

    async def _notify() -> None:
        # The page's own work: a pool checkout, its queries, the provider send.
        await asyncio.sleep(0.05)
        delivered.append("owner paged")

    async def _sweep() -> list[str]:
        _schedule(_notify())  # what the tenant's commit does
        return ["tenant-1"]  # ... and the sweep returns without awaiting it

    monkeypatch.setitem(SWEEPS, SESSION_FINALIZE, _sweep)

    assert await _run_entry_point() == 0

    assert delivered == ["owner paged"]
    assert not escalation_dispatch._BACKGROUND_NOTIFICATIONS
    line = capsys.readouterr().out.strip().splitlines()[-1]
    assert json.loads(line) == {"sweep": SESSION_FINALIZE, "result": ["tenant-1"]}


async def test_a_notification_that_never_finishes_is_abandoned_after_the_bound(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    seen: dict[str, Any] = {}

    async def _hung_provider_call() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            seen["cancelled"] = True
            raise

    async def _sweep() -> list[str]:
        seen["task"] = _schedule(_hung_provider_call())
        return []

    monkeypatch.setitem(SWEEPS, SESSION_FINALIZE, _sweep)
    monkeypatch.setattr(jobs_main, "NOTIFICATION_DRAIN_SECONDS", 0.2)

    started = time.monotonic()
    with caplog.at_level(logging.ERROR, logger="app.jobs"):
        # Not a failed run: a scheduler retry could not re-page a sealed session.
        assert await _run_entry_point() == 0
    assert time.monotonic() - started < 10

    assert seen["cancelled"] is True and seen["task"].cancelled()
    assert not escalation_dispatch._BACKGROUND_NOTIFICATIONS
    errors = [
        r
        for r in caplog.records
        if r.levelno == logging.ERROR and "sweep_notification_drain_timeout" in r.getMessage()
    ]
    # ERROR is the level the sweep log group's SweepErrors metric filter alarms on,
    # and the line names the sweep so the operator knows which run to look at.
    assert len(errors) == 1
    assert f"sweep={SESSION_FINALIZE}" in errors[0].getMessage()


async def test_pages_already_scheduled_still_go_out_when_the_sweep_then_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    delivered: list[str] = []

    async def _notify() -> None:
        await asyncio.sleep(0.05)
        delivered.append("owner paged")

    async def _sweep() -> list[str]:
        _schedule(_notify())  # tenant one committed its escalation
        raise RuntimeError("candidate lookup failed for the next step")

    monkeypatch.setitem(SWEEPS, SESSION_FINALIZE, _sweep)

    # The failure still surfaces: a non-zero exit, and an ERROR line naming the sweep
    # (the level the SweepErrors metric filter counts) with the traceback under it ...
    with caplog.at_level(logging.ERROR, logger="app.jobs"):
        assert await _run_entry_point() == 1
    failed = [r for r in caplog.records if "sweep_failed" in r.getMessage()]
    assert len(failed) == 1 and failed[0].levelno == logging.ERROR
    assert f"sweep={SESSION_FINALIZE}" in failed[0].getMessage()
    assert failed[0].exc_info and "candidate lookup failed" in str(failed[0].exc_info[1])
    # ... and the page for the tenant that had already committed was not lost with it.
    assert delivered == ["owner paged"]


async def test_the_process_does_not_wait_for_the_in_process_retry_ladder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ladder sleeps 30 s and then 120 s between attempts; the refused send has
    already enqueued its durable deferred action, so the deferred sweep owns that
    retry and the Fargate task is not held open for it."""
    seen: dict[str, Any] = {}

    async def _ladder() -> None:
        await asyncio.sleep(3600)

    async def _sweep() -> list[str]:
        seen["ladder"] = _schedule(_ladder(), escalation_dispatch._BACKGROUND_RETRIES)
        return []

    monkeypatch.setitem(SWEEPS, SESSION_FINALIZE, _sweep)

    started = time.monotonic()
    assert await _run_entry_point() == 0

    assert time.monotonic() - started < 10  # nowhere near the 60 s notification bound
    assert seen["ladder"].cancelled()
    assert not escalation_dispatch._BACKGROUND_RETRIES
