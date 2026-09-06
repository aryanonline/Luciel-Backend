"""In-process mailbox polling on the data plane (2026-09-05 audit, cost).

The 5-minute ``mailbox_poll`` sweep was 288 Fargate task launches a day for a job
that, on a platform with a handful of BYO mailboxes, finishes in seconds. The data
plane is already running; it polls from a background task on the same cadence,
under the SAME per-tenant advisory lock the sweep takes, so an EventBridge backstop
(hourly, kept so the sweep-scheduling contract still holds) can never double-poll.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable

from app.config import Settings

logger = logging.getLogger(__name__)

DATA_PLANE = "data-plane"


def should_run_inprocess_mailbox_poll(settings: Settings) -> bool:
    """Only the data plane polls in-process, and only when the environment says so
    (the ECS task definition sets the interval; local and CI runs leave it at 0)."""
    return settings.inprocess_mailbox_poll_seconds > 0 and (
        os.environ.get("LUCIEL_PLANE") == DATA_PLANE
    )


async def mailbox_poll_loop(
    stop: asyncio.Event,
    *,
    interval_seconds: float,
    run: Callable[[], Awaitable[object]] | None = None,
) -> int:
    """Poll until ``stop`` is set. A failing run is logged and the loop continues —
    one bad tenant or one provider outage must not end polling for everyone.
    Returns the number of runs (the tests' acted signal)."""
    if run is None:
        from app.jobs.sweeps import run_mailbox_poll  # noqa: PLC0415

        run = run_mailbox_poll
    runs = 0
    while not stop.is_set():
        try:
            await run()
        except Exception:  # noqa: BLE001 — logged, never fatal to the loop
            logger.exception("inprocess_mailbox_poll_failed")
        runs += 1
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue
    return runs
