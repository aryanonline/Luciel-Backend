"""The per-call output ceiling and the truncation flag (round 7 walk run 8).

The graph extractor's JSON stopped at the providers' 1024-token default on every long
page (a probe against the live model: /legal/terms, 1943 input tokens, stopped at
output_tokens=1024 mid-object) and read as "unparseable". ``LLMRouter.complete`` now
takes ``max_output_tokens``; ``None`` (every existing caller) changes nothing.
Hermetic: fake SDK clients record the kwargs each provider sends.
"""

from __future__ import annotations

from typing import Any

import pytest
from app.runtime.llm_router import LLMResponse, LLMRouter, ModelClass, ProviderError
from app.runtime.providers import TOOL_CALL_MAX_OUTPUT_TOKENS, AnthropicProvider, OpenAIProvider
from app.runtime.testing import EchoModel

TOOLS = [
    {
        "tool_id": "lookup_record",
        "description": "Query the record source.",
        "input_schema": {"type": "object", "properties": {}},
    }
]
TURN = [{"role": "user", "content": "hi"}]


class _Obj:
    def __init__(self, **kw: object) -> None:
        self.__dict__.update(kw)


class _Recorder:
    def __init__(self, response: object) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kw: Any) -> object:
        self.calls.append(kw)
        return self.response


def _anthropic(stop_reason: str = "end_turn") -> tuple[AnthropicProvider, _Recorder]:
    rec = _Recorder(_Obj(content=[_Obj(type="text", text="ok")], stop_reason=stop_reason))
    p = AnthropicProvider(api_key="k", model="m")
    p._client = _Obj(messages=rec)
    return p, rec


def _openai(finish_reason: str = "stop") -> tuple[OpenAIProvider, _Recorder]:
    choice = _Obj(message=_Obj(content="ok", tool_calls=None), finish_reason=finish_reason)
    rec = _Recorder(_Obj(choices=[choice]))
    p = OpenAIProvider(api_key="k", model="m")
    p._client = _Obj(chat=_Obj(completions=rec))
    return p, rec


@pytest.mark.parametrize(
    ("override", "tools", "expected"),
    [
        pytest.param(None, None, 1024, id="default-unchanged"),
        pytest.param(None, TOOLS, TOOL_CALL_MAX_OUTPUT_TOKENS, id="tools-default-unchanged"),
        pytest.param(4096, None, 4096, id="override"),
        pytest.param(4096, TOOLS, 4096, id="override-above-the-tool-floor"),
        pytest.param(512, TOOLS, TOOL_CALL_MAX_OUTPUT_TOKENS, id="tool-floor-holds"),
        pytest.param(512, None, 512, id="override-below-default"),
    ],
)
async def test_the_override_reaches_both_providers_kwargs(
    override: int | None, tools: list[dict[str, Any]] | None, expected: int
) -> None:
    ap, arec = _anthropic()
    op, orec = _openai()
    kw: dict[str, Any] = {} if override is None else {"max_output_tokens": override}
    await ap.complete("sys", list(TURN), tools=tools, **kw)
    await op.complete("sys", list(TURN), tools=tools, **kw)
    assert arec.calls[-1]["max_tokens"] == expected
    assert orec.calls[-1]["max_completion_tokens"] == expected
    assert "max_tokens" not in orec.calls[-1]  # GPT-5.x rejects the old name


@pytest.mark.parametrize(("stop", "truncated"), [("max_tokens", True), ("end_turn", False)])
async def test_anthropic_truncated_is_read_from_stop_reason(stop: str, truncated: bool) -> None:
    p, _ = _anthropic(stop)
    assert (await p.complete("sys", list(TURN))).truncated is truncated


@pytest.mark.parametrize(("finish", "truncated"), [("length", True), ("stop", False)])
async def test_openai_truncated_is_read_from_finish_reason(finish: str, truncated: bool) -> None:
    p, _ = _openai(finish)
    assert (await p.complete("sys", list(TURN))).truncated is truncated


async def test_echo_model_and_a_bare_response_are_never_truncated() -> None:
    out = await EchoModel().complete("sys", list(TURN), max_output_tokens=10)
    assert out.truncated is False
    bare = LLMResponse(text="", provider="x", model_class=ModelClass.fast, confidence=1.0)
    assert bare.truncated is False


class _Seen(EchoModel):
    def __init__(self, *, fail: bool = False) -> None:
        super().__init__()
        self.fail = fail
        self.kwargs: list[dict[str, Any]] = []

    async def complete(self, system_prompt, messages, **kw):  # type: ignore[override]
        self.kwargs.append(kw)
        if self.fail:
            raise ProviderError("down")
        return await super().complete(system_prompt, messages, **kw)


class _OldShape(EchoModel):
    """A provider (or fake) that predates the override: it takes ``tools`` only."""

    async def complete(self, system_prompt, messages, *, tools=None):  # type: ignore[override]
        return await super().complete(system_prompt, messages, tools=tools)


async def test_the_router_forwards_the_override_to_primary_and_fallback() -> None:
    primary, fallback = _Seen(fail=True), _Seen()
    router = LLMRouter(primary=primary, fallback=fallback)
    out = await router.complete("sys", list(TURN), max_output_tokens=4096)
    assert out.used_fallback is True
    assert primary.kwargs[-1]["max_output_tokens"] == 4096
    assert fallback.kwargs[-1]["max_output_tokens"] == 4096


async def test_the_router_forwards_the_override_to_the_fast_provider() -> None:
    primary, fast = _Seen(), _Seen()
    router = LLMRouter(primary=primary, fallback=EchoModel(), fast=fast)
    await router.complete(
        "sys",
        list(TURN),
        requires_tools=False,
        context_tokens=0,
        complexity=0.0,
        max_output_tokens=4096,
    )
    assert fast.kwargs[-1]["max_output_tokens"] == 4096
    assert primary.kwargs == []


async def test_without_an_override_the_call_shape_is_unchanged() -> None:
    primary = _Seen()
    await LLMRouter(primary=primary, fallback=EchoModel()).complete("sys", list(TURN))
    assert primary.kwargs[-1] == {"tools": None}
    # A provider that predates the keyword still works when nobody passes it.
    out = await LLMRouter(primary=_OldShape(), fallback=EchoModel()).complete("sys", list(TURN))
    assert out.text == "Echo: hi"
