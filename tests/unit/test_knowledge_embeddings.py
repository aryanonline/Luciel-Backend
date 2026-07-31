"""Live knowledge embeddings are real embeddings, not the hashing stand-in (§3.2.2).

``get_embedder`` returned ``HashingEmbedder`` unconditionally — a bag-of-hashed-tokens
vector. It is a legitimate vector and pgvector ranks it correctly, but it encodes no
semantics: only exact token overlap retrieves anything, so a paraphrased customer
question matched nothing in live. Selection is now mode-aware, mirroring
``app.runtime.provider_factory``: mock keeps the deterministic fake (no key, no
network), live builds the OpenAI-backed embedder and fails loud without the key rather
than writing semantically-empty vectors into the live index.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from app.config import Settings
from app.knowledge.embeddings import (
    EMBED_DIM,
    EmbeddingError,
    HashingEmbedder,
    OpenAIEmbedder,
    get_embedder,
)

from tests.unit.live_settings import REQUIRED_IN_LIVE_MODE


def _settings(**blob: str) -> Settings:
    return Settings(
        integration_mode="live",
        platform_secret=json.dumps({**REQUIRED_IN_LIVE_MODE, **blob}),
    )


class _FakeEmbeddings:
    """Stands in for ``AsyncOpenAI().embeddings`` — records calls, returns unit vectors."""

    def __init__(self, dim: int = EMBED_DIM) -> None:
        self.dim = dim
        self.calls: list[list[str]] = []

    async def create(self, *, model: str, input: list[str], dimensions: int) -> Any:  # noqa: A002
        self.calls.append(list(input))
        self.model = model
        self.requested_dimensions = dimensions
        # Width comes from the fake, not the request, so a provider returning the wrong
        # width can be exercised. Returned out of order on purpose: the caller zips
        # vectors onto chunk rows, so trusting response order would mislabel knowledge.
        data = [
            type("Item", (), {"index": i, "embedding": [float(i)] * self.dim})()
            for i in range(len(input))
        ]
        return type("Resp", (), {"data": list(reversed(data))})()


def _wire(embedder: OpenAIEmbedder, fake: _FakeEmbeddings) -> None:
    embedder._client = type("Client", (), {"embeddings": fake})()


def test_mock_mode_uses_the_deterministic_fake():
    assert isinstance(get_embedder(Settings()), HashingEmbedder)


def test_live_mode_builds_the_real_provider():
    embedder = get_embedder(_settings(OPENAI_API_KEY="sk-test"))
    assert isinstance(embedder, OpenAIEmbedder)
    assert embedder._model == Settings().knowledge_embedding_model


def test_live_mode_without_the_key_fails_loud():
    """Never silently degrade to the fake in live mode: hashing vectors and real vectors
    are different spaces, so mixing them corrupts every similarity score in the index."""
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        get_embedder(_settings())


@pytest.mark.asyncio
async def test_vectors_come_back_in_input_order():
    embedder = OpenAIEmbedder(api_key="sk-test", model="text-embedding-3-small", dim=4)
    _wire(embedder, _FakeEmbeddings(dim=4))
    vectors = await embedder.embed(["a", "b", "c"])
    assert vectors == [[0.0] * 4, [1.0] * 4, [2.0] * 4]


@pytest.mark.asyncio
async def test_large_inputs_are_batched():
    embedder = OpenAIEmbedder(api_key="sk-test", model="text-embedding-3-small", dim=2)
    fake = _FakeEmbeddings(dim=2)
    _wire(embedder, fake)
    vectors = await embedder.embed([f"chunk {i}" for i in range(300)])
    assert len(vectors) == 300
    assert [len(c) for c in fake.calls] == [128, 128, 44]


@pytest.mark.asyncio
async def test_no_call_is_made_for_an_empty_input():
    embedder = OpenAIEmbedder(api_key="sk-test", model="text-embedding-3-small")
    fake = _FakeEmbeddings()
    _wire(embedder, fake)
    assert await embedder.embed([]) == []
    assert fake.calls == []


@pytest.mark.asyncio
async def test_the_hashing_fake_carries_no_semantics():
    """Why the swap matters, stated as a property of the fake rather than a claim about
    it: with no token overlap the hashing embedder puts a paraphrase of a document and an
    unrelated document at the SAME cosine distance, so retrieval has no signal to rank
    on. This is the ceiling live retrieval was running against."""
    hashing = HashingEmbedder()
    query, relevant, irrelevant = (
        "what time do you close",
        "we are open 9-5 on weekdays",
        "anything returned in its packaging gets a full refund",
    )
    qv, rv, iv = await hashing.embed([query, relevant, irrelevant])

    def cosine_distance(a: list[float], b: list[float]) -> float:
        return 1.0 - sum(x * y for x, y in zip(a, b, strict=True))

    assert cosine_distance(qv, rv) > 0.95
    assert abs(cosine_distance(qv, rv) - cosine_distance(qv, iv)) < 0.05


@pytest.mark.asyncio
async def test_a_wrong_width_vector_is_rejected():
    """The chunk column is vector(1536); a mismatch must fail at the provider boundary
    rather than as an opaque insert error halfway through an ingestion."""
    embedder = OpenAIEmbedder(api_key="sk-test", model="text-embedding-3-small", dim=EMBED_DIM)
    _wire(embedder, _FakeEmbeddings(dim=8))
    with pytest.raises(EmbeddingError, match="embedding_dim_mismatch"):
        await embedder.embed(["a"])
