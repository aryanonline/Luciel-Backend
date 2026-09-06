"""Embedding provider selection for the knowledge pipeline (Architecture §3.2.2).

Retrieval was semantically broken: every environment, live included, embedded with
``HashingEmbedder`` — a bag-of-hashed-tokens vector. It is a valid vector and pgvector
ranks it correctly, but it carries no semantics, so only *exact token overlap*
retrieves. A customer asking "what are your opening times?" against a document that
says "we are open 9-5 on weekdays" retrieved nothing.

This module keeps the ``Embedder`` Protocol and the deterministic hashing embedder as
the mock/CI implementation, and adds the real one: OpenAI's embeddings endpoint, the
same platform-owned ``OPENAI_API_KEY`` the LLM fallback and Whisper STT already use
(§3.8.3 — platform-owned key class, never hardcoded). Selection is mode-aware and
mirrors ``app.runtime.provider_factory``: ``mock`` (the default) constructs no client
and opens no network path; only ``live`` reads the platform secret, and it fails loud
at construction rather than silently degrading to the semantically-empty fake.

Model selection is never admin-configurable (Locked Decision §7 #11) — the model ID
comes from config.
"""

from __future__ import annotations

from app.config import Settings, get_settings, parse_platform_secret
from app.runtime.embeddings import EMBED_DIM, Embedder, HashingEmbedder

__all__ = ["EMBED_DIM", "Embedder", "HashingEmbedder", "OpenAIEmbedder", "get_embedder"]

# The embeddings endpoint accepts a batch of inputs per call. Ingesting a large file
# produces hundreds of chunks, so batch rather than issuing one request per chunk.
_BATCH = 128


class EmbeddingError(RuntimeError):
    """Embedding backend failure — surfaces as a failed ingestion/retrieval, never as
    a silent fallback to a different vector space (mixing spaces would corrupt every
    similarity score in the index)."""


class OpenAIEmbedder:
    """Real embedding provider — OpenAI embeddings behind the ``Embedder`` Protocol."""

    @property
    def model_name(self) -> str:
        """The pin behind every vector this embedder writes (F121)."""
        return self._model

    def __init__(self, *, api_key: str, model: str, dim: int = EMBED_DIM) -> None:
        self.dim = dim
        self._api_key = api_key
        self._model = model
        self._client: object | None = None

    def _get_client(self) -> object:
        if self._client is None:
            from openai import AsyncOpenAI  # noqa: PLC0415 — lazy/guarded import

            self._client = AsyncOpenAI(api_key=self._api_key)
        return self._client

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        from openai import OpenAIError  # noqa: PLC0415 — lazy/guarded import

        client = self._get_client()
        vectors: list[list[float]] = []
        for start in range(0, len(texts), _BATCH):
            batch = texts[start : start + _BATCH]
            try:
                resp = await client.embeddings.create(  # type: ignore[attr-defined]
                    model=self._model,
                    input=batch,
                    # text-embedding-3-* support native dimension reduction. Asking for
                    # the column's width keeps `vector(1536)` valid across model swaps.
                    dimensions=self.dim,
                )
            except OpenAIError as exc:
                raise EmbeddingError(f"openai_error:{type(exc).__name__}") from exc
            # The API documents order-preserving output, but the chunk<->vector pairing
            # is zipped straight into rows, so a reorder would mislabel knowledge.
            vectors.extend(item.embedding for item in sorted(resp.data, key=lambda d: d.index))
        if any(len(v) != self.dim for v in vectors):
            raise EmbeddingError(f"embedding_dim_mismatch:expected_{self.dim}")
        return vectors


def get_embedder(settings: Settings | None = None) -> Embedder:
    """Build the embedder for the active integration mode.

    - ``mock`` (default): ``HashingEmbedder`` — deterministic, no key, no network.
    - ``live``: ``OpenAIEmbedder`` with the platform ``OPENAI_API_KEY``.
    """
    settings = settings if settings is not None else get_settings()
    if settings.integration_mode != "live":
        return HashingEmbedder()

    api_key = parse_platform_secret(settings.platform_secret, live=True).get("OPENAI_API_KEY", "")
    if not api_key:
        # Fail loud, exactly as _build_live_router does: a live deploy without the key
        # is a misconfiguration. Falling back to HashingEmbedder here would write
        # semantically-empty vectors into the live index — worse than not ingesting.
        raise RuntimeError(
            "integration_mode=live but platform secret is missing keys: OPENAI_API_KEY "
            "(required for knowledge embeddings)"
        )
    return OpenAIEmbedder(api_key=api_key, model=settings.knowledge_embedding_model)
