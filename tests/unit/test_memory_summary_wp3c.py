"""Summary minimization + the bound summarizer (round 7 WP-3c item 4).

* §3.4.11 minimization: a persisted summary never carries an email address, a
  phone number, a card number or a long digit string — contact facts become
  presence flags ("email on file" / "phone on file"), numbers are redacted, and
  the intent line is a word-boundary truncation with identifiers removed.
* The live posture binds the fast model behind ``summarize_session`` for the
  §3.4.7 shape (intent / actions taken / open items / next step), grounded ONLY in
  the transcript; the deterministic structurer stays the mock path and the
  fallback for a provider error, a cap refusal, an off-shape or an ungrounded
  answer.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from app.models.conversation import Transcript
from app.runtime import memory, provider_factory
from app.runtime.llm_cap import LLMCapExceeded
from app.runtime.llm_router import LLMResponse, ModelClass, ProviderError

ADMIN = uuid.uuid4()


def _t(role: str, text: str) -> Transcript:
    return Transcript(
        id=uuid.uuid4(),
        admin_id=ADMIN,
        instance_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        role=role,
        text=text,
        at=datetime.now(UTC),
    )


HISTORY = [
    _t("lead", "Hi, I'm Jane — I'd like a deck quote, budget around $5,000, this week if possible"),
    _t("luciel", "Happy to help. What's the best email to send the estimate to?"),
    _t("lead", "jane@acme.com, or call me on 416-555-0143"),
    _t("luciel", "Thanks — I've passed this to the team; they'll follow up with an estimate."),
]


class _FakeRouter:
    """Records the one call the summarizer makes and answers with a script."""

    def __init__(self, text: str | None = None, *, raise_exc: Exception | None = None) -> None:
        self.text = text
        self.raise_exc = raise_exc
        self.calls: list[dict[str, object]] = []

    async def complete(self, system_prompt, messages, **kwargs):  # noqa: ANN001, ANN003
        self.calls.append({"system_prompt": system_prompt, "messages": messages, **kwargs})
        if self.raise_exc is not None:
            raise self.raise_exc
        return LLMResponse(
            text=self.text or "",
            provider="anthropic_fast",
            model_class=ModelClass.fast,
            confidence=0.9,
        )


# --- §3.4.11 minimization ------------------------------------------------------------ #
def test_minimize_turns_contact_facts_into_flags_and_redacts_long_numbers():
    text = (
        "card 4111 1111 1111 1111, email Jane@Acme.com, call 416-555-0143 or +1 (604) 555 0100, "
        "ref 12345678, budget $5,000 at 2 pm on 2026-09-28, 3 bedrooms"
    )
    out = memory.minimize(text)
    assert "4111" not in out and "[number redacted]" in out
    assert "Acme" not in out and "@" not in out and "email on file" in out
    assert "0143" not in out and "0100" not in out and out.count("phone on file") == 2
    assert "12345678" not in out
    # Short figures, times and ISO dates are facts the summary may keep.
    assert "$5,000" in out and "2 pm" in out and "2026-09-28" in out and "3 bedrooms" in out
    assert memory.minimize(out) == out  # idempotent


def test_the_deterministic_summary_carries_no_identifier_and_only_presence_flags():
    history = [
        _t(
            "lead",
            "I need a quote — my card is 4111 1111 1111 1111, email jane@acme.com, "
            "call 416-555-0143, budget $5,000, sometime this week",
        ),
        _t("luciel", "Sure — I'll send the estimate to jane@acme.com."),
    ]
    summary = memory.summarize(history)
    for leaked in ("4111", "jane@", "acme.com", "0143", "416"):
        assert leaked not in summary, summary
    assert summary.startswith(
        "Intent: I need a quote — my card is [number redacted], email email on file, "
        "call phone on file, budget $5,000, sometime this week"
    )
    assert "Last response: Sure — I'll send the estimate to email on file." in summary
    assert "| Facts: email on file; phone on file; budget $5,000; timing this week" in summary
    assert "| Turns: 2" in summary


def test_the_intent_line_is_cut_at_a_word_boundary():
    long = "I want " + " ".join(f"word{i}" for i in range(60)) + " and my email is jane@acme.com"
    summary = memory.summarize([_t("lead", long)])
    intent = summary.split(" | ")[0]
    assert intent.endswith("…") and len(intent) <= len("Intent: ") + memory.SUMMARY_LINE_LIMIT + 1
    assert not intent.rstrip("…").endswith("wor")  # never half a token
    assert "jane@" not in summary and "email on file" in summary


# --- the bound summarizer ------------------------------------------------------------ #
async def test_the_fast_model_is_asked_for_the_four_line_shape_grounded_in_the_transcript():
    router = _FakeRouter(
        "Intent: a deck quote, budget around $5,000, this week\n"
        "Actions taken: asked for an email; passed the request to the team\n"
        "Open items: the estimate itself (Jane gave jane@acme.com and 416-555-0143)\n"
        "Next step: the team follows up with an estimate"
    )
    summary = await memory.summarize_session(HISTORY, admin_id=ADMIN, router=router)
    assert summary == (
        "Intent: a deck quote, budget around $5,000, this week"
        " | Actions taken: asked for an email; passed the request to the team"
        " | Open items: the estimate itself (Jane gave email on file and phone on file)"
        " | Next step: the team follows up with an estimate"
    )
    (call,) = router.calls
    # The fast-tier hints (§3.4.3 / §7 #9) and the tenant cap travel with the call.
    assert call["requires_tools"] is False and call["complexity"] == 0.0
    assert call["admin_id"] == ADMIN
    assert call["system_prompt"] == memory.SUMMARY_SYSTEM_PROMPT
    # The model sees the transcript and NOTHING else — no prior summaries, no lead row.
    (message,) = call["messages"]
    assert message["role"] == "user"
    assert message["content"] == memory.transcript_for_summary(HISTORY)
    assert message["content"].startswith("Customer: Hi, I'm Jane")
    assert "Luciel: Happy to help." in message["content"]


async def test_a_person_who_joined_is_noted_on_the_model_path_too():
    history = [*HISTORY, _t("human_agent", "Ana here — I can come by Tuesday.")]
    router = _FakeRouter(
        "Intent: a deck quote\nActions taken: Ana offered a visit Tuesday\n"
        "Open items: none\nNext step: Ana comes by Tuesday"
    )
    summary = await memory.summarize_session(history, admin_id=ADMIN, router=router)
    assert summary.endswith(" | A person joined this conversation")


@pytest.mark.parametrize(
    ("text", "exc"),
    [
        # A figure the transcript never mentioned: the one hallucination a business
        # record cannot carry.
        (
            "Intent: a deck quote for $9,000\nActions taken: none\nOpen items: none\n"
            "Next step: not stated",
            None,
        ),
        # Off-shape: two lines, not four.
        ("Intent: a deck quote\nNext step: follow up", None),
        # Content that is not about this conversation at all.
        (
            "Intent: renewing a boat insurance policy in Halifax\nActions taken: quoted "
            "premium tiers\nOpen items: marina paperwork\nNext step: broker visits marina",
            None,
        ),
        ("", ProviderError("primary down")),
        ("", LLMCapExceeded(ADMIN)),
    ],
)
async def test_an_unusable_model_answer_falls_back_to_the_deterministic_summary(text, exc):
    router = _FakeRouter(text, raise_exc=exc)
    summary = await memory.summarize_session(HISTORY, admin_id=ADMIN, router=router)
    assert summary == memory.summarize(HISTORY)
    assert "jane@" not in summary and "0143" not in summary


# What memory reads: whether the LLM is real (live mode, or the local stack's
# llm_posture="live").
_MOCK_SETTINGS = SimpleNamespace(integration_mode="mock", real_ai=False)
_LIVE_SETTINGS = SimpleNamespace(integration_mode="live", real_ai=True)


async def test_mock_posture_never_builds_a_router_and_uses_the_structurer(monkeypatch):
    def _never(**_kw):  # noqa: ANN003
        raise AssertionError("no router may be built in mock posture")

    monkeypatch.setattr(provider_factory, "build_llm_router", _never)
    monkeypatch.setattr(memory, "get_settings", lambda: _MOCK_SETTINGS)
    assert await memory.summarize_session(HISTORY, admin_id=ADMIN) == memory.summarize(HISTORY)


async def test_live_posture_binds_the_tenant_capped_router(monkeypatch):
    router = _FakeRouter(
        "Intent: a deck quote\nActions taken: passed to the team\nOpen items: the estimate\n"
        "Next step: the team follows up"
    )
    built: list[uuid.UUID | None] = []

    def _build(settings=None, *, admin_id=None):  # noqa: ANN001
        built.append(admin_id)
        return router

    monkeypatch.setattr(provider_factory, "build_llm_router", _build)
    monkeypatch.setattr(memory, "get_settings", lambda: _LIVE_SETTINGS)
    summary = await memory.summarize_session(HISTORY, admin_id=ADMIN)
    assert built == [ADMIN]
    assert summary.startswith("Intent: a deck quote | Actions taken: passed to the team")


async def test_a_router_that_cannot_be_built_in_live_posture_falls_back(monkeypatch):
    def _broken(settings=None, *, admin_id=None):  # noqa: ANN001
        raise RuntimeError("integration_mode=live but platform secret is missing keys")

    monkeypatch.setattr(provider_factory, "build_llm_router", _broken)
    monkeypatch.setattr(memory, "get_settings", lambda: _LIVE_SETTINGS)
    assert await memory.summarize_session(HISTORY, admin_id=ADMIN) == memory.summarize(HISTORY)


async def test_an_empty_history_never_calls_a_model():
    router = _FakeRouter("Intent: x\nActions taken: y\nOpen items: z\nNext step: w")
    assert await memory.summarize_session([], admin_id=ADMIN, router=router) == (
        "No conversation content."
    )
    assert router.calls == []
