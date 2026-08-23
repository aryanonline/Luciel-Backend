"""Provider tool-use mapping (audit round 3, C2) — neutral shape ↔ native SDKs.

Hermetic: fake SDK clients RECORD the kwargs each provider sends, so these tests
assert the exact native request shapes — including the two API rules that 400 in
production if broken:

  * Anthropic: an assistant ``tool_use`` turn must be immediately followed by ONE
    user message carrying ALL matching ``tool_result`` blocks (consecutive neutral
    tool_result turns merge; they are never sent as separate user messages).
  * OpenAI: ``function.arguments`` is a JSON STRING both ways — serialized on send,
    defensively parsed on receive (malformed arguments drop the call, never crash).

The neutral turn contract (llm_router.LLMProvider):
  {"role": "user"|"assistant", "content": str}
  {"role": "assistant", "content": str, "tool_calls": [{call_id, tool_id, inputs}]}
  {"role": "tool_result", "call_id": str, "tool_id": str, "content": str,
   "is_error": bool}
"""

from __future__ import annotations

import json

from app.runtime.llm_router import LLMRouter
from app.runtime.providers import AnthropicProvider, OpenAIProvider
from app.runtime.testing import EchoModel

TOOL_DEFS = [
    {
        "tool_id": "lookup_record",
        "description": "Query the record source.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    }
]

NEUTRAL_TOOL_TURNS = [
    {"role": "user", "content": "do you have a premium package?"},
    {
        "role": "assistant",
        "content": "Let me check.",
        "tool_calls": [
            {"call_id": "c1", "tool_id": "lookup_record", "inputs": {"query": "premium"}}
        ],
    },
    {
        "role": "tool_result",
        "call_id": "c1",
        "tool_id": "lookup_record",
        "content": '{"outcome": "ok", "payload": {"record": {"name": "Premium", "price": "$99"}}}',
        "is_error": False,
    },
    {
        "role": "tool_result",
        "call_id": "c2",
        "tool_id": "lookup_record",
        "content": '{"outcome": "failed", "reason": "x"}',
        "is_error": True,
    },
]


# --- recording fakes: Anthropic ---------------------------------------------
class _Block:
    def __init__(self, **kw: object) -> None:
        self.__dict__.update(kw)


class _RecordingAnthropicMessages:
    def __init__(self, response: object) -> None:
        self._response = response
        self.calls: list[dict[str, object]] = []

    async def create(self, **kw: object) -> object:
        self.calls.append(kw)
        return self._response

    @property
    def last(self) -> dict[str, object]:
        return self.calls[-1]


class _RecordingAnthropicClient:
    def __init__(self, response: object) -> None:
        self.messages = _RecordingAnthropicMessages(response)


# --- recording fakes: OpenAI -------------------------------------------------
class _RecordingCompletions:
    def __init__(self, response: object) -> None:
        self._response = response
        self.calls: list[dict[str, object]] = []

    async def create(self, **kw: object) -> object:
        self.calls.append(kw)
        return self._response

    @property
    def last(self) -> dict[str, object]:
        return self.calls[-1]


class _RecordingOpenAIClient:
    def __init__(self, response: object) -> None:
        self.chat = _Block(completions=_RecordingCompletions(response))


def _openai_response(content: str | None, tool_calls: list[object] | None = None) -> object:
    return _Block(choices=[_Block(message=_Block(content=content, tool_calls=tool_calls))])


# ---------------------------------------------------------------------------
# Anthropic mapping
# ---------------------------------------------------------------------------
async def test_anthropic_sends_tools_and_merges_tool_results() -> None:
    resp = _Block(content=[_Block(type="text", text="Premium is $99.")], stop_reason="end_turn")
    p = AnthropicProvider(api_key="k", model="m")
    p._client = _RecordingAnthropicClient(resp)

    await p.complete("sys", list(NEUTRAL_TOOL_TURNS), tools=TOOL_DEFS)
    sent = p._client.messages.last

    assert sent["tools"] == [
        {
            "name": "lookup_record",
            "description": "Query the record source.",
            "input_schema": TOOL_DEFS[0]["input_schema"],
        }
    ]
    msgs = sent["messages"]
    # user, assistant(tool_use), ONE merged user(tool_result x2)
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assistant = msgs[1]["content"]
    assert assistant[0] == {"type": "text", "text": "Let me check."}
    assert assistant[1]["type"] == "tool_use"
    assert assistant[1]["id"] == "c1"
    assert assistant[1]["name"] == "lookup_record"
    assert assistant[1]["input"] == {"query": "premium"}
    results = msgs[2]["content"]
    assert [b["type"] for b in results] == ["tool_result", "tool_result"]
    assert results[0]["tool_use_id"] == "c1"
    assert results[0]["is_error"] is False
    assert results[1]["tool_use_id"] == "c2"
    assert results[1]["is_error"] is True


async def test_anthropic_parses_tool_use_ids_and_raises_token_ceiling() -> None:
    resp = _Block(
        content=[
            _Block(type="text", text="checking"),
            _Block(type="tool_use", id="toolu_1", name="lookup_record", input={"query": "x"}),
        ],
        stop_reason="tool_use",
    )
    p = AnthropicProvider(api_key="k", model="m", max_output_tokens=1024)
    p._client = _RecordingAnthropicClient(resp)

    out = await p.complete("sys", [{"role": "user", "content": "hi"}], tools=TOOL_DEFS)
    assert out.tool_calls == [
        {"call_id": "toolu_1", "tool_id": "lookup_record", "inputs": {"query": "x"}}
    ]
    # Tool JSON counts against max_tokens — a tool-bearing call gets more headroom.
    assert p._client.messages.last["max_tokens"] >= 2048


async def test_anthropic_without_tools_is_byte_identical_to_legacy_shape() -> None:
    resp = _Block(content=[_Block(type="text", text="hello")])
    p = AnthropicProvider(api_key="k", model="m", max_output_tokens=1024)
    p._client = _RecordingAnthropicClient(resp)

    out = await p.complete("sys", [{"role": "user", "content": "hi"}])
    sent = p._client.messages.last
    assert "tools" not in sent
    assert sent["max_tokens"] == 1024
    assert sent["messages"] == [{"role": "user", "content": "hi"}]
    assert out.text == "hello"


# ---------------------------------------------------------------------------
# OpenAI mapping
# ---------------------------------------------------------------------------
async def test_openai_sends_function_defs_and_tool_role_messages() -> None:
    p = OpenAIProvider(api_key="k", model="m")
    p._client = _RecordingOpenAIClient(_openai_response("Premium is $99."))

    await p.complete("sys", list(NEUTRAL_TOOL_TURNS), tools=TOOL_DEFS)
    sent = p._client.chat.completions.last

    assert sent["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "lookup_record",
                "description": "Query the record source.",
                "parameters": TOOL_DEFS[0]["input_schema"],
            },
        }
    ]
    msgs = sent["messages"]
    # system, user, assistant(tool_calls), tool, tool
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "tool", "tool"]
    assistant = msgs[2]
    assert assistant["content"] == "Let me check."
    call = assistant["tool_calls"][0]
    assert call["id"] == "c1"
    assert call["type"] == "function"
    assert call["function"]["name"] == "lookup_record"
    assert json.loads(call["function"]["arguments"]) == {"query": "premium"}
    assert msgs[3] == {
        "role": "tool",
        "tool_call_id": "c1",
        "content": NEUTRAL_TOOL_TURNS[2]["content"],
    }
    assert msgs[4]["tool_call_id"] == "c2"


async def test_openai_parses_tool_calls_and_drops_malformed_arguments() -> None:
    good = _Block(
        id="call_1",
        function=_Block(name="lookup_record", arguments='{"query": "premium"}'),
    )
    malformed = _Block(id="call_2", function=_Block(name="lookup_record", arguments="{not json"))
    p = OpenAIProvider(api_key="k", model="m")
    p._client = _RecordingOpenAIClient(_openai_response(None, [good, malformed]))

    out = await p.complete("sys", [{"role": "user", "content": "hi"}], tools=TOOL_DEFS)
    assert out.tool_calls == [
        {"call_id": "call_1", "tool_id": "lookup_record", "inputs": {"query": "premium"}}
    ]
    assert out.text == ""


async def test_openai_without_tools_is_byte_identical_to_legacy_shape() -> None:
    p = OpenAIProvider(api_key="k", model="m")
    p._client = _RecordingOpenAIClient(_openai_response("answer"))

    out = await p.complete("sys", [{"role": "user", "content": "hi"}])
    sent = p._client.chat.completions.last
    assert "tools" not in sent
    assert sent["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ]
    assert out.text == "answer"


# ---------------------------------------------------------------------------
# EchoModel + router pass-through
# ---------------------------------------------------------------------------
async def test_echo_model_default_behavior_is_unchanged_and_accepts_tools() -> None:
    echo = EchoModel()
    out = await echo.complete("sys", [{"role": "user", "content": "hi"}], tools=TOOL_DEFS)
    assert out.text == "Echo: hi"
    assert out.tool_calls == []


async def test_router_passes_tools_through_to_the_provider() -> None:
    class _Recorder(EchoModel):
        def __init__(self) -> None:
            super().__init__()
            self.seen_tools: object = "unset"

        async def complete(self, system_prompt, messages, *, tools=None):  # type: ignore[override]
            self.seen_tools = tools
            return await super().complete(system_prompt, messages, tools=tools)

    primary = _Recorder()
    router = LLMRouter(primary=primary, fallback=EchoModel())
    await router.complete("sys", [{"role": "user", "content": "hi"}], tools=TOOL_DEFS)
    assert primary.seen_tools == TOOL_DEFS
