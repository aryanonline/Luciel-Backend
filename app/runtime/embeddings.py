"""Embedding provider (platform-controlled, pluggable — Architecture §3.4.3 ethos).

Production uses the platform LLM provider's embedding endpoint (Anthropic/OpenAI
class). For local dev/test and deterministic CI, a hashing-based embedder produces
stable, L2-normalized vectors so retrieval is exercisable end-to-end with no
external API. The interface is identical; the provider is swapped via config.

Model selection is never admin-configurable (Locked Decision §7 #11).
"""

from __future__ import annotations

import hashlib
import math
from typing import Protocol

EMBED_DIM = 1536


class Embedder(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashingEmbedder:
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


_default: Embedder | None = None


def get_embedder() -> Embedder:
    global _default
    if _default is None:
        # TODO(Arc 14+): swap to the platform LLM-provider embedder via config.
        _default = HashingEmbedder()
    return _default
