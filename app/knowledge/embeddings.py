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

import asyncio
import logging
import random
from typing import Any

from app.config import Settings, get_settings, parse_platform_secret
from app.runtime.embeddings import EMBED_DIM, Embedder, HashingEmbedder

__all__ = [
    "EMBED_DIM",
    "Embedder",
    "HashingEmbedder",
    "OpenAIEmbedder",
    "embeddings_degraded",
    "get_embedder",
]

logger = logging.getLogger(__name__)

# The embeddings endpoint accepts a batch of inputs per call. Ingesting a large file
# produces hundreds of chunks, so batch rather than issuing one request per chunk.
_BATCH = 128

# Round 7 WP-6 item 7: ONE retry, after a short jittered wait, for a refusal that may
# clear within a second — 429 (rate limit / quota burst) and 5xx (provider trouble).
# Never for a 4xx that will not change (bad request, auth) and never for a timeout or
# connection error (the visitor is already waiting on this call). The wait stays
# inside a quarter of the per-call deadline. Tests set the range to (0, 0).
EMBED_RETRY_DELAY_RANGE: tuple[float, float] = (0.25, 0.75)
# The process-level health signal: this many CONSECUTIVE failed ``embed()`` calls and
# ``/healthz`` names ``embeddings:degraded`` (live mode, via integration_status); one
# success clears it. A single blip is not degraded — the retry and the turn's own
# KnowledgeRetrievalDegraded marker cover that.
EMBEDDINGS_DEGRADED_AFTER = 3
_consecutive_failures = 0


def embeddings_degraded() -> bool:
    """True while the last ``EMBEDDINGS_DEGRADED_AFTER`` embed calls all failed."""
    return _consecutive_failures >= EMBEDDINGS_DEGRADED_AFTER


def reset_embeddings_health() -> None:
    """Forget the failure streak (tests; a fresh process starts healthy)."""
    global _consecutive_failures
    _consecutive_failures = 0


def _note_embed_outcome(*, ok: bool) -> None:
    global _consecutive_failures
    if ok:
        if _consecutive_failures >= EMBEDDINGS_DEGRADED_AFTER:
            logger.warning("embeddings_recovered after=%d failed calls", _consecutive_failures)
        _consecutive_failures = 0
        return
    _consecutive_failures += 1
    if _consecutive_failures == EMBEDDINGS_DEGRADED_AFTER:
        logger.error(
            "embeddings_degraded consecutive_failures=%d — knowledge retrieval is down "
            "until the embedding provider answers",
            _consecutive_failures,
        )


def _retryable_status(exc: BaseException) -> bool:
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and (status == 429 or status >= 500)


def _retry_delay() -> float:
    low, high = EMBED_RETRY_DELAY_RANGE
    delay = random.uniform(low, high)  # noqa: S311  # nosec B311 — jitter, not security
    ceiling = float(get_settings().llm_timeout_seconds) / 4
    return max(0.0, min(delay, ceiling))


#: What the owner reads when the embedding provider is down: one sentence, served by the
#: EmbeddingError handler (app.api.errors) and by the sync job (app.knowledge.sync_job).
EMBEDDING_DOWN = (
    "Your knowledge couldn't be indexed right now — the embedding provider "
    "isn't answering. Nothing was saved; please try again in a few minutes."
)
EMBEDDING_RETRY_AFTER_SECONDS = 300


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

            # Same budget as the chat providers (2026-09-05 audit F101): a visitor
            # is waiting on this call, and the SDK's own retries only multiply an
            # outage's latency — the 2026-09-06 E2E walk watched a 429 at quota get
            # retried twice for nothing behind the typing bubble.
            self._client = AsyncOpenAI(
                api_key=self._api_key,
                max_retries=0,
                timeout=float(get_settings().llm_timeout_seconds),
            )
        return self._client

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            vectors = await self._embed(texts)
        except EmbeddingError:
            _note_embed_outcome(ok=False)
            raise
        _note_embed_outcome(ok=True)
        return vectors

    async def _embed(self, texts: list[str]) -> list[list[float]]:
        from openai import OpenAIError  # noqa: PLC0415 — lazy/guarded import

        client = self._get_client()
        vectors: list[list[float]] = []
        for start in range(0, len(texts), _BATCH):
            batch = texts[start : start + _BATCH]
            resp: Any = None
            for attempt in (1, 2):
                try:
                    resp = await client.embeddings.create(  # type: ignore[attr-defined]
                        model=self._model,
                        input=batch,
                        # text-embedding-3-* support native dimension reduction. Asking
                        # for the column's width keeps `vector(1536)` valid across model
                        # swaps.
                        dimensions=self.dim,
                    )
                    break
                except OpenAIError as exc:
                    if attempt == 1 and _retryable_status(exc):
                        # ONE jittered retry for 429/5xx (round 7 WP-6 item 7); the
                        # SDK's own retries stay off (F101).
                        await asyncio.sleep(_retry_delay())
                        continue
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
