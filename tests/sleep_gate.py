"""Hold one module's waits on the test instead of on the clock.

Some tests must land an action inside a wait of the code under test: the sender's commit
between two lookups of the delivery-receipt route, a provider's recovery before a retry
ladder's rung. They used to shorten that wait and sleep a little themselves, betting that
their action would come first. On a slow runner it need not: a correct build then fails,
and a build that never waits can pass. ``SleepGate.install`` gives ONE module a stand-in
for its ``asyncio`` name that forwards everything to the real module except ``sleep``: a
positive wait there is recorded and ends only once the test calls ``release``. A zero wait
is a turn of the loop, not a wait (a drain lets finished tasks' callbacks run that way),
and goes straight to the real ``asyncio.sleep``. ``asyncio.sleep`` itself is never
patched, so every other module, the client, the database driver and the test keep their
real timers.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

#: Bound on every wait a gate holds, and on the test's own wait for one. These waits span
#: real database work (a request's way up to its first wait, the test's commit while the
#: code is held) on a shared test database that is sometimes starved, so the bound is sized
#: for that rather than for a handoff between two coroutines. It only matters once
#: something is broken: the test then fails within it instead of hanging until the run's
#: watchdog (600 s, ``tests/conftest.py``).
GATE_SECONDS = 30


class _GatedAsyncio:
    """What the gated module reads as ``asyncio``: the real module, ``sleep`` aside."""

    def __init__(self, gate: SleepGate) -> None:
        self.sleep = gate.sleep

    def __getattr__(self, name: str) -> Any:
        return getattr(asyncio, name)


class SleepGate:
    """Holds every positive ``asyncio.sleep`` of the module it is installed on until
    ``release``, and records the delay each one asked for."""

    def __init__(self) -> None:
        #: The delay each held wait asked for, in order.
        self.delays: list[float] = []
        self._reached = asyncio.Event()
        self._released = asyncio.Event()

    def install(self, monkeypatch: pytest.MonkeyPatch, module: object) -> None:
        """Gate ``module``'s waits for the rest of the test."""
        monkeypatch.setattr(module, "asyncio", _GatedAsyncio(self))

    async def sleep(self, delay: float, result: Any = None) -> Any:
        if delay <= 0:
            return await asyncio.sleep(delay, result)
        self.delays.append(delay)
        self._reached.set()
        await asyncio.wait_for(self._released.wait(), timeout=GATE_SECONDS)
        return result

    async def reached(self, *, unless: asyncio.Future[Any] | None = None) -> None:
        """Return once the module is held in a wait. If ``unless`` (the request or job
        under test) finishes first, fail at once with what it returned or raised, rather
        than at the bound with a bare timeout."""
        reached = asyncio.ensure_future(self._reached.wait())
        racers: set[asyncio.Future[Any]] = {reached} if unless is None else {reached, unless}
        try:
            await asyncio.wait(racers, timeout=GATE_SECONDS, return_when=asyncio.FIRST_COMPLETED)
        finally:
            reached.cancel()
            await asyncio.gather(reached, return_exceptions=True)
        if self._reached.is_set():
            return
        if unless is not None and unless.done():
            error = unless.exception()
            if error is not None:
                raise error
            pytest.fail(f"it finished without waiting: {unless.result()!r}")
        raise TimeoutError(f"nothing was held in a wait within {GATE_SECONDS} s")

    def release(self) -> None:
        """End the held wait, and let every later one pass at once."""
        self._released.set()
