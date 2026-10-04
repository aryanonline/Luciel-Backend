"""A drain is bounded by the tasks the running loop can actually await (round 7).

Escalation notifications and their retry ladders run as background tasks that the
process drains at shutdown and the suite drains after every test. A task belongs to
the loop that created it: one left behind by a loop that has stopped can never
finish, and ``asyncio.gather`` on it from another loop waits forever. A full-suite CI
run sat in exactly that wait for 34 minutes on 2026-10-04, reporting nothing.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from app.notifications import escalation_dispatch

pytestmark = [pytest.mark.asyncio]

_LOGGER = "app.notifications.escalation_dispatch"


async def _never() -> None:
    await asyncio.Event().wait()


def _task_on_a_stopped_loop() -> tuple[asyncio.AbstractEventLoop, asyncio.Task[None]]:
    """A pending task whose loop is not running — built on a worker thread, because a
    second loop cannot be driven from inside the test's own running loop."""
    loop = asyncio.new_event_loop()
    return loop, loop.create_task(_never())


def _dispose(loop: asyncio.AbstractEventLoop, task: asyncio.Task[None]) -> None:
    task.cancel()
    loop.run_until_complete(asyncio.gather(task, return_exceptions=True))
    loop.close()


@pytest.mark.parametrize(
    ("registry_name", "drain_name"),
    [
        ("_BACKGROUND_NOTIFICATIONS", "drain_background_notifications"),
        ("_BACKGROUND_RETRIES", "drain_background_retries"),
    ],
)
async def test_a_task_left_on_another_loop_is_dropped_not_awaited_forever(
    registry_name: str, drain_name: str, caplog: pytest.LogCaptureFixture
) -> None:
    registry = getattr(escalation_dispatch, registry_name)
    drain = getattr(escalation_dispatch, drain_name)
    loop, foreign = await asyncio.to_thread(_task_on_a_stopped_loop)
    registry.add(foreign)
    try:
        with caplog.at_level(logging.ERROR, logger=_LOGGER):
            # Unguarded, this waits on a task whose loop will never run again.
            await asyncio.wait_for(drain(), timeout=2)
        assert foreign not in registry
        assert any("abandoned background escalation task" in r.getMessage() for r in caplog.records)
    finally:
        registry.discard(foreign)
        await asyncio.to_thread(_dispose, loop, foreign)


async def test_a_task_on_this_loop_is_still_awaited_to_completion() -> None:
    ran: list[str] = []

    async def _work() -> None:
        await asyncio.sleep(0.01)
        ran.append("done")

    task = asyncio.get_running_loop().create_task(_work())
    escalation_dispatch._BACKGROUND_NOTIFICATIONS.add(task)
    task.add_done_callback(escalation_dispatch._BACKGROUND_NOTIFICATIONS.discard)
    await escalation_dispatch.drain_background_notifications()
    assert ran == ["done"]
    assert task not in escalation_dispatch._BACKGROUND_NOTIFICATIONS


async def test_the_whole_shutdown_drain_survives_an_abandoned_task() -> None:
    loop, foreign = await asyncio.to_thread(_task_on_a_stopped_loop)
    escalation_dispatch._BACKGROUND_NOTIFICATIONS.add(foreign)
    try:
        await asyncio.wait_for(escalation_dispatch.drain_background_work(), timeout=2)
        assert not escalation_dispatch._BACKGROUND_NOTIFICATIONS
    finally:
        escalation_dispatch._BACKGROUND_NOTIFICATIONS.discard(foreign)
        await asyncio.to_thread(_dispose, loop, foreign)
