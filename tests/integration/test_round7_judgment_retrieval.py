"""Retrieval as an employee would use the filing cabinet (round 7 WP-2, items 3 + 4).

* Retrieval runs only when there is something to look up: a social or contact-details
  turn with no question in it performs no embedding call (item 3).
* The model is told what an absent Knowledge block means — searched and nothing
  matched, or the knowledge could not be searched — instead of guessing (item 4).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from app.db.session import tenant_session
from app.models.conversation import Session as SessionModel
from app.models.conversation import Transcript
from app.runtime.knowledge_retrieval import retrieval_unavailable_marker
from app.runtime.llm_router import LLMRouter
from app.runtime.orchestrator import (
    KNOWLEDGE_NONE_MATCHED,
    KNOWLEDGE_UNAVAILABLE,
    PLATFORM_NOTE_KEY,
    Orchestrator,
)
from app.runtime.testing import EchoModel
from sqlalchemy import select

pytestmark = [pytest.mark.asyncio]

REFUND_CHUNK = "Our refund policy allows a full refund within 30 days of purchase."
QUESTION = "what is your refund policy?"


class _RecordingBundle:
    """A ``retriever_bundle`` fake that records every query it was asked."""

    def __init__(self, texts=(), attributed=None) -> None:
        self._texts = list(texts)
        self._attributed = attributed
        self.calls: list[str] = []

    async def __call__(self, query: str):
        self.calls.append(query)
        attributed = list(self._attributed) if self._attributed is not None else []
        return list(self._texts), attributed


class _RecordingEcho(EchoModel):
    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.turns: list[tuple[str, list[dict[str, Any]], Any]] = []

    async def complete(self, system_prompt, messages, *, tools=None):  # type: ignore[override]
        self.turns.append((system_prompt, [dict(m) for m in messages], tools))
        return await super().complete(system_prompt, messages, tools=tools)


async def _seed_session(s, admin, iid) -> uuid.UUID:
    sid = uuid.uuid4()
    s.add(SessionModel(id=sid, admin_id=admin, instance_id=iid, channel="widget", mode="ai"))
    await s.flush()
    return sid


async def _never(_q: str) -> list[str]:
    raise AssertionError("the two-seam retriever must not be used when a bundle is bound")


def _orch(s, admin, iid, *, bundle, primary=None) -> Orchestrator:
    return Orchestrator(
        session=s,
        router=LLMRouter(
            primary=primary or EchoModel(confidence=1.0, fixed_reply=REFUND_CHUNK),
            fallback=EchoModel(),
        ),
        retriever=_never,
        retriever_bundle=bundle,
        admin_id=admin,
        instance_id=iid,
        enabled_channels={"widget"},
        has_payment_method=False,
    )


async def _luciel_rows(s, sid: uuid.UUID) -> list[Transcript]:
    return list(
        (
            await s.execute(
                select(Transcript)
                .where(Transcript.session_id == sid, Transcript.role == "luciel")
                .order_by(Transcript.at)
            )
        )
        .scalars()
        .all()
    )


# --- item 3 ------------------------------------------------------------------------ #
@pytest.mark.parametrize(
    ("text", "searched"),
    [
        ("thanks!", False),
        ("No its alright thankyou", False),
        ("you can reach me at jane@example.com", False),
        ("Thanks — and do you open Saturdays?", True),
        ("thanks, what time do you close", True),
        ("jane@example.com — when could you fit me in?", True),
        ("do you open Saturdays?", True),
    ],
)
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_turn_with_nothing_to_look_up_performs_no_search(text: str, searched: bool):
    """The retriever used to embed and search every turn — "thanks!" included —
    although the turn classifiers had already decided there was nothing to ground.
    A question in the same breath still searches."""
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        sid = await _seed_session(s, admin, iid)
        bundle = _RecordingBundle(texts=[REFUND_CHUNK])
        result = await _orch(s, admin, iid, bundle=bundle).run_turn(
            session_id=sid, resolved_lead_id=None, channel="widget", mode="ai", inbound_text=text
        )
        assert (bundle.calls != []) is searched, text
        assert result.reply_text  # the turn was still answered
        (row,) = await _luciel_rows(s, sid)
        if searched:
            assert row.source_chunks == []  # the fake attributes nothing; the search ran
        else:
            # No search ran: the row carries no attribution, never a false "nothing matched".
            assert row.source_chunks is None


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_pending_tool_gather_keeps_the_search_even_on_a_social_turn():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        sid = await _seed_session(s, admin, iid)
        sess = (await s.execute(select(SessionModel).where(SessionModel.id == sid))).scalar_one()
        sess.input_ask_attempts = 1  # the previous turn stalled on a required input
        await s.flush()
        bundle = _RecordingBundle(texts=[REFUND_CHUNK])
        await _orch(s, admin, iid, bundle=bundle).run_turn(
            session_id=sid,
            resolved_lead_id=None,
            channel="widget",
            mode="ai",
            inbound_text="ok thanks",
        )
        assert bundle.calls == ["ok thanks"]


# --- item 4 ------------------------------------------------------------------------ #
def _notes(messages: list[dict[str, Any]]) -> list[str]:
    return [str(m["content"]) for m in messages if m.get(PLATFORM_NOTE_KEY)]


def _has_block(messages: list[dict[str, Any]]) -> bool:
    return any(str(m.get("content", "")).startswith("Knowledge:") for m in messages)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_model_is_told_whether_nothing_matched_or_the_search_could_not_run():
    """The Knowledge block was appended only when chunks came back, so the model could
    not tell "searched, nothing matched" from "not searched"; the embedding-outage
    marker the bundle persists never reached the prompt either."""
    admin, iid = uuid.uuid4(), uuid.uuid4()

    async def _prompt_for(s, bundle, text: str) -> list[dict[str, Any]]:
        sid = await _seed_session(s, admin, iid)
        primary = _RecordingEcho(confidence=1.0, fixed_reply=REFUND_CHUNK)
        await _orch(s, admin, iid, bundle=bundle, primary=primary).run_turn(
            session_id=sid, resolved_lead_id=None, channel="widget", mode="ai", inbound_text=text
        )
        _, messages, _ = primary.turns[0]
        return messages

    async with tenant_session(admin) as s:
        # Searched, nothing matched: the note, no block.
        messages = await _prompt_for(s, _RecordingBundle(texts=[], attributed=[]), QUESTION)
        assert _notes(messages) == [KNOWLEDGE_NONE_MATCHED] and not _has_block(messages)
        # The search could not run (the bundle persisted its outage marker): the other note.
        degraded = _RecordingBundle(
            texts=[], attributed=[retrieval_unavailable_marker("EmbeddingError: quota")]
        )
        messages = await _prompt_for(s, degraded, QUESTION)
        assert _notes(messages) == [KNOWLEDGE_UNAVAILABLE] and not _has_block(messages)
        # Chunks found: the block, no note.
        messages = await _prompt_for(s, _RecordingBundle(texts=[REFUND_CHUNK]), QUESTION)
        assert _notes(messages) == [] and _has_block(messages)
        # Not searched (a social turn): neither — an absent block means "not consulted".
        messages = await _prompt_for(s, _RecordingBundle(texts=[REFUND_CHUNK]), "thanks!")
        assert _notes(messages) == [] and not _has_block(messages)
        # A note is a platform message the provider sees as a user turn, never a lead
        # transcript row or a knowledge claim.
        assert all(m.get("role") in ("user", "assistant") for m in messages)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_note_never_becomes_the_answer_or_the_customer_message():
    """A searched-nothing-matched turn still echoes the customer's words (the double
    ignores the note) and the note is never persisted as a transcript row."""
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        sid = await _seed_session(s, admin, iid)
        primary = _RecordingEcho(confidence=1.0)
        await _orch(
            s, admin, iid, bundle=_RecordingBundle(texts=[], attributed=[]), primary=primary
        ).run_turn(
            session_id=sid,
            resolved_lead_id=None,
            channel="widget",
            mode="ai",
            inbound_text=QUESTION,
        )
        _, messages, _ = primary.turns[0]
        assert _notes(messages) == [KNOWLEDGE_NONE_MATCHED]
        rows = (
            (await s.execute(select(Transcript).where(Transcript.session_id == sid)))
            .scalars()
            .all()
        )
        assert all(KNOWLEDGE_NONE_MATCHED not in r.text for r in rows)
