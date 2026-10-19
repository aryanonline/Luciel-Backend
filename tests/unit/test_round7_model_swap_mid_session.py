"""Round 7 WP-6 item 10: a model swap never changes the model under an in-flight turn.

The model_currency watchdog swaps a retired pin by rotating the platform secret and
force-redeploying the app services. ECS injects that secret at TASK START, the
process reads it once (``get_settings`` is cached), and every turn's router holds
providers built from that read. So the swap reaches only tasks started after it: the
rolling deployment drains the old tasks, and a turn already running — and any turn
that still lands on an old task — finishes on the OLD pin. Modelled here at the router
level with fake SDK clients that record the model each call names.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from app.config import get_settings
from app.runtime.llm_router import LLMRouter
from app.runtime.provider_factory import build_llm_router

from tests.unit.live_settings import REQUIRED_IN_LIVE_MODE, live_settings

OLD, NEW = "claude-sonnet-4-6", "claude-sonnet-4-8"
_KEYS = {"ANTHROPIC_API_KEY": "sk-ant-fake", "OPENAI_API_KEY": "sk-oai-fake"}


class _RecordingMessages:
    def __init__(self, seen: list[str]) -> None:
        self._seen = seen

    async def create(self, **kw: object) -> object:
        self._seen.append(str(kw["model"]))
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")], usage=None)


def _record_models(router: LLMRouter) -> list[str]:
    """Swap the live router's SDK clients for recorders; return what they saw."""
    seen: list[str] = []
    router._primary._client = SimpleNamespace(messages=_RecordingMessages(seen))  # type: ignore[attr-defined]
    return seen


async def test_an_in_flight_turn_finishes_on_the_pin_its_router_was_built_with():
    task_settings = live_settings(**_KEYS)  # what this task read at start
    assert task_settings.llm_anthropic_primary_model == OLD
    router = build_llm_router(task_settings)
    seen = _record_models(router)
    with router.turn():
        await router.complete("sys", [{"role": "user", "content": "first call"}])
        # The watchdog swaps mid-turn: the platform secret now names the successor —
        # what a task started from here on reads.
        rotated = live_settings(**_KEYS, LLM_ANTHROPIC_PRIMARY_MODEL=NEW)
        assert rotated.llm_anthropic_primary_model == NEW
        # A tool-using turn's next model calls stay on the pin the turn began with.
        await router.complete("sys", [{"role": "user", "content": "after the tool"}])
        await router.complete("sys", [{"role": "user", "content": "final answer"}])
    assert seen == [OLD, OLD, OLD]

    # Only a task started after the swap (its own settings read) runs the successor.
    fresh_task_router = build_llm_router(rotated)
    fresh_seen = _record_models(fresh_task_router)
    await fresh_task_router.complete("sys", [{"role": "user", "content": "next turn"}])
    assert fresh_seen == [NEW]


@pytest.fixture
def live_process(monkeypatch):
    """The process environment an ECS task gets: the platform secret as an env var."""

    def _boot(**blob: str) -> None:
        monkeypatch.setenv("LUCIEL_INTEGRATION_MODE", "live")
        monkeypatch.setenv(
            "LUCIEL_PLATFORM_SECRET", json.dumps({**REQUIRED_IN_LIVE_MODE, **_KEYS, **blob})
        )

    monkeypatch.delenv("LUCIEL_LLM_ANTHROPIC_PRIMARY_MODEL", raising=False)
    get_settings.cache_clear()  # type: ignore[attr-defined]
    yield _boot
    get_settings.cache_clear()  # type: ignore[attr-defined]


async def test_the_pin_is_read_once_per_task_so_later_turns_on_an_old_task_keep_it(
    live_process,
):
    live_process()  # the task boots with the old pin
    first_turn = build_llm_router()
    assert first_turn._primary._model == OLD  # type: ignore[attr-defined]

    # The secret rotates. Even if the process environment changed under it (ECS does
    # not even do that — the env is fixed at container start), the settings this task
    # read at boot are what every later turn's router is built from.
    live_process(LLM_ANTHROPIC_PRIMARY_MODEL=NEW)
    later_turn_same_task = build_llm_router()
    assert later_turn_same_task._primary._model == OLD  # type: ignore[attr-defined]

    # The forced redeploy: a new task reads the secret afresh.
    get_settings.cache_clear()  # type: ignore[attr-defined]
    new_task = build_llm_router()
    assert new_task._primary._model == NEW  # type: ignore[attr-defined]
