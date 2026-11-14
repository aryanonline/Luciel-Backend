"""SeedTest event-loop hygiene (round 7 WP-12): every async client is closed inside the
loop that opened it, so a run ends with no traceback in its log.

The 2026-10-19 dev run printed "Task exception was never retrieved ... AsyncClient.aclose()
... RuntimeError: Event loop is closed" three times (after T5 and T6). Each in-process block
runs under its own ``asyncio.run()``, which closes its loop on the way out; a live router's
Anthropic/OpenAI SDK clients were left open there. The SDK finalizer — run whenever the
garbage collector reaches the client, typically during the NEXT ``asyncio.run()`` — schedules
``aclose()`` on whatever loop is running, and closing the dead loop's transports raises.

``FakeSdkClient`` models exactly that: it is bound to the loop that opened it, a close from
any other loop raises "Event loop is closed", and ``finalize()`` is the SDK's ``__del__``.
The first test reproduces the live noise with it; the rest prove the harness closes every
client in its own loop, last opened first, each once, and that one failing close never
leaves the others open.
"""

from __future__ import annotations

import asyncio
import inspect
import uuid

import pytest

from tests.unit import seedtest_harness

H = seedtest_harness.load()


class FakeSdkClient:
    def __init__(self, name: str, log: list[tuple[str, bool]]) -> None:
        self.name = name
        self.log = log
        self.loop = asyncio.get_running_loop()
        self.closed = False

    def is_closed(self) -> bool:
        return self.closed

    async def close(self) -> None:
        running = asyncio.get_running_loop()
        if running is not self.loop or self.loop.is_closed():
            raise RuntimeError("Event loop is closed")
        self.closed = True
        # (name, closed inside the very loop that opened it while that loop still ran)
        self.log.append((self.name, running is self.loop and not self.loop.is_closed()))

    def finalize(self) -> None:
        """What the SDK's ``__del__`` does when the GC reaches an unclosed client."""
        if self.closed:
            return
        try:
            asyncio.get_running_loop().create_task(self.close())
        except Exception:  # noqa: BLE001 — the SDK swallows "no running loop" too
            pass


class FakeProvider:
    """An Anthropic/OpenAI provider: the SDK client is built lazily on first use."""

    def __init__(self, name: str, log: list[tuple[str, bool]]) -> None:
        self.name = name
        self.log = log
        self._client: FakeSdkClient | None = None

    def use(self) -> FakeSdkClient:
        if self._client is None:
            self._client = FakeSdkClient(self.name, self.log)
        return self._client


def _later_loop_finalizes(client: FakeSdkClient) -> list[str]:
    """Run a NEW loop in which the GC finalizes ``client``; return what that loop's
    exception handler saw (the 'Task exception was never retrieved' channel)."""
    seen: list[str] = []

    async def later() -> None:
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(
            lambda _loop, ctx: seen.append(f"{ctx.get('message')}: {ctx.get('exception')!r}")
        )
        client.finalize()
        for _ in range(3):
            await asyncio.sleep(0)

    asyncio.run(later())
    return seen


def test_an_unclosed_client_reproduces_the_live_noise() -> None:
    log: list[tuple[str, bool]] = []
    holder: dict[str, FakeSdkClient] = {}

    async def open_only() -> None:
        holder["client"] = FakeSdkClient("openai", log)

    asyncio.run(open_only())
    seen = _later_loop_finalizes(holder["client"])
    assert any("never retrieved" in s and "Event loop is closed" in s for s in seen), seen


def test_run_async_closes_inside_the_opening_loop_last_opened_first() -> None:
    log: list[tuple[str, bool]] = []
    providers = [FakeProvider(name, log) for name in ("primary", "fallback", "fast")]

    async def body(clients: object) -> str:
        for provider in providers:
            clients.track(provider)  # type: ignore[attr-defined]
            provider.use()
        return "done"

    assert H.run_async(body, app_cleanup=False) == "done"
    assert log == [("fast", True), ("fallback", True), ("primary", True)]


def test_a_client_closed_in_its_loop_leaves_nothing_for_the_next_loop() -> None:
    log: list[tuple[str, bool]] = []
    provider = FakeProvider("anthropic", log)

    async def body(clients: object) -> None:
        clients.track(provider)  # type: ignore[attr-defined]
        provider.use()

    H.run_async(body, app_cleanup=False)
    assert provider._client is not None
    assert _later_loop_finalizes(provider._client) == []


def test_clients_are_closed_even_when_the_body_raises() -> None:
    log: list[tuple[str, bool]] = []
    provider = FakeProvider("anthropic", log)

    async def body(clients: object) -> None:
        clients.track(provider)  # type: ignore[attr-defined]
        provider.use()
        raise ValueError("the case failed")

    with pytest.raises(ValueError, match="the case failed"):
        H.run_async(body, app_cleanup=False)
    assert log == [("anthropic", True)]


def test_router_legs_are_found_and_a_shared_provider_is_closed_once() -> None:
    from app.runtime.llm_router import LLMRouter

    log: list[tuple[str, bool]] = []
    shared = FakeProvider("anthropic", log)  # primary AND fast, like a degenerate wiring
    fallback = FakeProvider("openai", log)
    router = LLMRouter(primary=shared, fallback=fallback, admin_id=uuid.uuid4(), fast=shared)

    async def go() -> None:
        shared.use()
        fallback.use()
        await H.close_llm_clients(router)

    asyncio.run(go())
    assert sorted(name for name, _ in log) == ["anthropic", "openai"]
    assert all(in_loop for _, in_loop in log)


def test_an_unused_provider_has_nothing_to_close() -> None:
    log: list[tuple[str, bool]] = []
    idle = FakeProvider("openai", log)

    async def go() -> None:
        await H.close_llm_clients(idle)

    asyncio.run(go())
    assert log == []


def test_one_failing_close_never_leaves_the_others_open(capsys: pytest.CaptureFixture[str]) -> None:
    log: list[tuple[str, bool]] = []

    class Broken(FakeSdkClient):
        async def close(self) -> None:
            raise OSError("socket already gone")

    async def go() -> None:
        first = FakeSdkClient("first", log)
        broken = Broken("broken", log)
        last = FakeSdkClient("last", log)
        await H.close_llm_clients(first, broken, last)

    asyncio.run(go())
    assert [name for name, _ in log] == ["last", "first"]
    assert "::warning::seedtest could not close Broken: OSError: socket already gone" in (
        capsys.readouterr().out
    )


@pytest.mark.parametrize("provider_name", ["AnthropicProvider", "OpenAIProvider"])
def test_the_real_sdk_clients_close_in_their_loop(provider_name: str) -> None:
    """The real providers' lazily-built SDK clients (no network: nothing is sent)."""
    from app.runtime import providers

    provider = getattr(providers, provider_name)(api_key="sk-test-not-a-key", model="m")

    async def go() -> object:
        client = provider._get_client()
        assert client.is_closed() is False
        await H.close_llm_clients(provider)
        return client

    client = asyncio.run(go())
    assert client.is_closed() is True  # type: ignore[attr-defined]


def test_every_in_process_block_that_builds_a_live_router_closes_it() -> None:
    """T3b and T5 build routers inside their own asyncio.run(); T4/T6 run through
    _run_inprocess_turn. Each must close the router inside that loop."""
    for fn in (H.test_llm_cap_live, H.test_fallback_failover, H._run_inprocess_turn):
        assert "await close_llm_clients(router)" in inspect.getsource(fn), fn.__name__


def test_the_teardown_disposes_its_engine_in_its_own_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.db.engine

    disposed: list[bool] = []

    async def fake_dispose() -> None:
        disposed.append(asyncio.get_running_loop().is_running())

    monkeypatch.setattr(app.db.engine, "dispose_engine", fake_dispose)
    asyncio.run(H._teardown([]))
    assert disposed == [True]


def test_off_turn_work_is_settled_inside_the_loop_and_bounded(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from app.notifications import escalation_dispatch

    state: dict[str, bool] = {}

    async def slow_drain() -> None:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            state["cancelled_in_loop"] = asyncio.get_running_loop().is_running()
            raise

    monkeypatch.setattr(escalation_dispatch, "drain_background_work", slow_drain)
    asyncio.run(H._settle_background_work(timeout=0.05))
    assert state == {"cancelled_in_loop": True}
    assert "off-turn escalation work still running" in capsys.readouterr().out


def test_the_harness_process_leaves_retries_to_the_durable_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.notifications import escalation_dispatch

    monkeypatch.setattr(escalation_dispatch, "ESCALATION_RETRY_DELAYS", (30.0, 120.0))
    H._harness_process_posture()
    assert escalation_dispatch.ESCALATION_RETRY_DELAYS == ()


@pytest.mark.parametrize(
    ("short_name", "assistant_name", "business", "assistant"),
    [
        ("Acme Store", "Aurora", "Acme Store", "Aurora"),
        (None, None, "this business", "SeedTest main"),
    ],
)
def test_the_in_process_turn_uses_the_names_production_passes(
    monkeypatch: pytest.MonkeyPatch,
    short_name: str | None,
    assistant_name: str | None,
    business: str,
    assistant: str,
) -> None:
    """Fixer round 1, finding 4: T6 built its Orchestrator with no names, so rule 1 told
    the model "I'm the assistant, an AI assistant for this business." - a sentence no
    production entry point sends - and the strict gate never live-checked the real
    wording. The harness now resolves the names as runtime_entry does."""
    import contextlib
    from types import SimpleNamespace

    import app.db.engine
    import app.db.session
    import app.runtime.orchestrator
    import app.runtime.sessions

    inst = SimpleNamespace(
        id=uuid.uuid4(),
        name="SeedTest main",
        business_short_name=short_name,
        personality={"assistantName": assistant_name} if assistant_name else {},
    )
    built: dict[str, object] = {}

    class FakeSession:
        async def execute(self, _stmt: object) -> object:
            return SimpleNamespace(scalar_one=lambda: inst)

    @contextlib.asynccontextmanager
    async def fake_tenant_session(_admin_id: uuid.UUID):  # type: ignore[no-untyped-def]
        yield FakeSession()

    async def fake_resolve(*_args: object, **_kwargs: object) -> tuple[object, bool]:
        return SimpleNamespace(id=uuid.uuid4(), mode="ai"), True

    class CaptureOrchestrator:
        def __init__(self, **kwargs: object) -> None:
            built.update(kwargs)

        async def run_turn(self, **_kwargs: object) -> str:
            return "turn"

    async def nothing(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(app.db.session, "tenant_session", fake_tenant_session)
    monkeypatch.setattr(app.runtime.sessions, "resolve_or_open_session", fake_resolve)
    monkeypatch.setattr(app.runtime.orchestrator, "Orchestrator", CaptureOrchestrator)
    monkeypatch.setattr(app.db.engine, "dispose_engine", nothing)
    monkeypatch.setattr(H, "_settle_background_work", nothing)
    monkeypatch.setattr(H, "close_llm_clients", nothing)

    result = asyncio.run(
        H._run_inprocess_turn(
            uuid.uuid4(),
            router=None,
            channel="email",
            enabled_channels={"widget", "email"},
            inbound="How many days do I have to return an item?",
            retriever=None,
        )
    )
    assert result == "turn"
    assert (built.get("business_name"), built.get("assistant_name")) == (business, assistant)
    assert built["instance_id"] == inst.id
