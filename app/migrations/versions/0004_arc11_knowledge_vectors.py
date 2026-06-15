"""arc11 knowledge: pgvector embedding column + HNSW index

Adds the embedding vector to knowledge_chunks and an HNSW ANN index (Locked
Decision §7 #3, Architecture §3.2). Dimension 1536 (standard embedding size; the
provider is platform-controlled and pluggable behind the embedder interface).

Expand-only. The composite (admin_id, instance_id) index already exists from Arc 10.

Revision ID: 0004_arc11_knowledge_vectors
Revises: 0003_arc10_audit_seq
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004_arc11_knowledge_vectors"
down_revision: str | None = "0003_arc10_audit_seq"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None

EMBED_DIM = 1536


def upgrade() -> None:
    op.execute(f"ALTER TABLE knowledge_chunks ADD COLUMN embedding vector({EMBED_DIM})")
    # HNSW index for cosine ANN over the embedding column (Locked Decision §7 #3).
    op.execute(
        "CREATE INDEX ix_kc_embedding_hnsw ON knowledge_chunks "
        "USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_kc_embedding_hnsw")
    op.execute("ALTER TABLE knowledge_chunks DROP COLUMN IF EXISTS embedding")
