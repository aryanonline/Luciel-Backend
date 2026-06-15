"""Hybrid vector + graph retrieval (DOCTRINE ANCHOR — Architecture §3.2 / §3.2.1).

Vector (pgvector HNSW) lands in Arc 11; graph + hybrid in Arc 16. The graph is
invoked only on structured-filter-intent queries; pure-semantic queries go to
vector only (Locked Decision §7 #6).
"""

from __future__ import annotations
