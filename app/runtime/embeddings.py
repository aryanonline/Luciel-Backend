"""Embedding interface and the deterministic mock embedder (Architecture §3.4.3 ethos).

Live mode embeds with OpenAI ``text-embedding-3-small`` (``OpenAIEmbedder``, model
pinned by ``settings.knowledge_embedding_model``). For local dev/test and
deterministic CI, a hashing-based embedder produces stable, L2-normalized vectors so
retrieval is exercisable end-to-end with no external API. The interface is
identical; ``app.knowledge.embeddings.get_embedder`` picks the embedder for the
integration mode.

Model selection is never admin-configurable (Locked Decision §7 #11).
"""

from __future__ import annotations

import hashlib
import math
from typing import Protocol

EMBED_DIM = 1536


class Embedder(Protocol):
    @property
    def model_name(self) -> str:
        """Which model produced the vectors (2026-09-05 audit F121); recorded per chunk
        so a pin change knows what to re-embed."""
        ...

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashingEmbedder:
    #: Hashed vectors belong to no real model; the name says so, and a pin change
    #: therefore never triggers a re-embed of mock-posture rows.
    model_name: str = "hashing-mock"
    """Deterministic, dependency-free embedder for dev/test.

    Maps token hashes into a fixed-dim vector (a bag-of-hashed-tokens), then
    L2-normalizes. Semantically: documents sharing tokens get higher cosine
    similarity — enough to exercise ANN retrieval deterministically.
    """

    def __init__(self, dim: int = EMBED_DIM) -> None:
        self.dim = dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        tokens = [tok for tok in text.lower().split() if tok]
        for tok in tokens:
            h = hashlib.sha256(tok.encode("utf-8")).digest()
            idx = int.from_bytes(h[:4], "big") % self.dim
            sign = 1.0 if h[4] & 1 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(x * x for x in vec))
        if norm == 0.0:
            # Avoid a zero vector (undefined cosine); nudge a stable component.
            vec[0] = 1.0
            norm = 1.0
        return [x / norm for x in vec]
