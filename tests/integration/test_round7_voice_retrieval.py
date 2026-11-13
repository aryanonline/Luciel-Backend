"""Voice runs the SAME single-pass retrieval bundle as every text channel (round 7
WP-2 remainder, item 5).

The voice pipeline used to bind ``retriever=`` + ``chunk_attributor=`` and no
``retriever_bundle``, so a caller's turn ran the two-seam path: no relevance floor
(an unrelated chunk could be spoken as the answer) and no ``EmbeddingError`` degraded
handling (a provider outage was an unhandled exception on a live call). Pinned at the
pipeline, through ``handle_turn``, with the production bundle over a patched vector
leg — hermetic, no embedding provider.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from app.db.session import tenant_session
from app.knowledge.embeddings import EmbeddingError
from app.models.conversation import Transcript
from app.models.instance import Instance, LucielState
from app.runtime import knowledge_retrieval
from app.runtime.knowledge_retrieval import is_retrieval_unavailable
from app.runtime.llm_router import LLMRouter
from app.runtime.orchestrator import KNOWLEDGE_UNAVAILABLE, PLATFORM_NOTE_KEY
from app.runtime.speech import EchoSpeechToText, EchoTextToSpeech
from app.runtime.testing import EchoModel
from app.runtime.voice_pipeline import handle_turn
from sqlalchemy import select

_ACTION_URL = "https://api.vantagemind.com/voice/turn"
NEAR_CHUNK = "Our refund policy allows a full refund within 30 days of purchase."
FAR_CHUNK = "The car park behind the building closes at 8pm on weekdays."
QUESTION = "what is your refund policy?"


class _RecordingEcho(EchoModel):
    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.turns: list[list[dict[str, Any]]] = []

    async def complete(self, system_prompt, messages, *, tools=None):  # type: ignore[override]
        self.turns.append([dict(m) for m in messages])
        return await super().complete(system_prompt, messages, tools=tools)


def _chunk(text: str) -> SimpleNamespace:
    return SimpleNamespace(source_id=uuid.uuid4(), text=text)


def _instance(admin: uuid.UUID, iid: uuid.UUID) -> Instance:
    return Instance(
        id=iid,
        admin_id=admin,
        name="L",
        state=LucielState.active,
        channels=["voice"],
        tools=[],
        escalation={},
        personality={},
    )


async def _turn(s, inst: Instance, admin: uuid.UUID, iid: uuid.UUID, model: EchoModel):
    return await handle_turn(
        s,
        instance=inst,
        admin_id=admin,
        instance_id=iid,
        from_number="+15557654321",
        speech_audio_ref=QUESTION,
        action_url=_ACTION_URL,
        stt=EchoSpeechToText(),
        tts=EchoTextToSpeech(),
        router=LLMRouter(primary=model, fallback=model),
        has_payment_method=False,
    )


async def _luciel_row(s, admin: uuid.UUID) -> Transcript:
    return (
        await s.execute(
            select(Transcript).where(Transcript.admin_id == admin, Transcript.role == "luciel")
        )
    ).scalar_one()


def _knowledge_block(messages: list[dict[str, Any]]) -> str:
    return "\n".join(
        str(m["content"])
        for m in messages
        if m.get("role") == "user" and str(m.get("content", "")).startswith("Knowledge:")
    )


def _notes(messages: list[dict[str, Any]]) -> list[str]:
    return [str(m["content"]) for m in messages if m.get(PLATFORM_NOTE_KEY)]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_voice_turn_drops_a_chunk_beyond_the_relevance_floor(monkeypatch):
    """The vector leg returns one relevant chunk and one far past the distance
    ceiling: the caller hears an answer built on the first alone, and the row's
    attribution names only the chunk that was actually used."""
    monkeypatch.setattr(knowledge_retrieval, "_relevance_floor_active", lambda: True)

    async def _retrieve(session, *, admin_id, instance_id, query, k=5):
        return [(_chunk(NEAR_CHUNK), 0.2), (_chunk(FAR_CHUNK), 0.95)]

    async def _sources(session, *, admin_id, instance_id, source_ids):
        return {}

    monkeypatch.setattr(knowledge_retrieval.service, "retrieve", _retrieve)
    monkeypatch.setattr(knowledge_retrieval.service, "sources_by_id", _sources)
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        inst = _instance(admin, iid)
        s.add(inst)
        await s.flush()
        model = _RecordingEcho(confidence=1.0, fixed_reply=NEAR_CHUNK)
        result = await _turn(s, inst, admin, iid, model)
        assert result.spoken_text and result.stt_failed is False
        (messages,) = model.turns
        block = _knowledge_block(messages)
        assert NEAR_CHUNK in block
        assert FAR_CHUNK not in block  # never injected, so never spoken
        row = await _luciel_row(s, admin)
        assert [c["text"] for c in row.source_chunks or []] == [NEAR_CHUNK]
        assert row.grounding_score is not None and row.grounding_score >= 0.5


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_voice_turn_survives_an_embedding_outage_with_the_degraded_marker(
    monkeypatch,
):
    """The embedding provider refuses the query mid-call: the turn is still served
    (no exception reaches the telephony webhook), the model is told the knowledge
    could not be searched, and the row carries the outage marker — never a false
    "searched and found nothing"."""

    async def _retrieve(session, *, admin_id, instance_id, query, k=5):
        raise EmbeddingError("openai_error:RateLimitError")

    monkeypatch.setattr(knowledge_retrieval.service, "retrieve", _retrieve)
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        inst = _instance(admin, iid)
        s.add(inst)
        await s.flush()
        model = _RecordingEcho(confidence=1.0)
        result = await _turn(s, inst, admin, iid, model)
        assert result.spoken_text and result.stt_failed is False
        assert "<Gather" in result.twiml  # the call stays up
        (messages,) = model.turns
        assert _notes(messages) == [KNOWLEDGE_UNAVAILABLE]
        assert _knowledge_block(messages) == ""
        row = await _luciel_row(s, admin)
        assert is_retrieval_unavailable(row.source_chunks)
