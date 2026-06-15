"""arc16 graph store: knowledge_entities + knowledge_edges + RLS

The domain-agnostic graph store (Architecture §3.2.1 — Path Locked: PostgreSQL
recursive CTEs ONLY, NO Neo4j/Memgraph/new vendor, Locked Decision §7 #4). Typed
nodes + directed typed edges extracted from admin-ingested knowledge at ingest time,
scoped by (admin_id, instance_id) with the three-layer Layer-2 RLS so the isolation
sweep auto-covers them. Both tables carry source_id so they cascade-delete with their
KnowledgeSource exactly like knowledge_chunks (§3.2.1 rule 5 / §3.2.2 cascade).

Traversal indexes back the recursive-CTE graph retrieval (§3.2.1 retrieval router):
  - (admin_id, instance_id) composite for tenant scoping (§3.2 indexes commitment),
  - source_id for the soft-delete cascade,
  - edges (admin_id, instance_id, src_entity_id) for forward relationship traversal,
  - entities (admin_id, instance_id, canonical) for membership / multi-attribute lookup.

Expand-only.

Revision ID: 0009_arc16_graph_store
Revises: 0008_arc14b_voice_call_records
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_arc16_graph_store"
down_revision: str | None = "0008_arc14b_voice_call_records"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None

APP_ROLE = "luciel_app"
_PREDICATE = "admin_id = NULLIF(current_setting('app.admin_id', true), '')::uuid"


def _apply_rls(table: str) -> None:
    """Install the three-layer Layer-2 RLS + standard grants (matches app/db/rls.py)."""
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
    )
    op.execute(f"REVOKE ALL ON {table} FROM {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")


def upgrade() -> None:
    op.create_table(
        "knowledge_entities",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("admin_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("instance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entity_type", sa.String(64), nullable=False),
        sa.Column("name", sa.String(512), nullable=False),
        sa.Column("canonical", sa.String(512), nullable=False),
        sa.Column(
            "attributes",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_entities_admin_instance", "knowledge_entities", ["admin_id", "instance_id"])
    op.create_index("ix_entities_source", "knowledge_entities", ["source_id"])
    # Membership / multi-attribute intersection lookups join on the normalized name.
    op.create_index(
        "ix_entities_canonical",
        "knowledge_entities",
        ["admin_id", "instance_id", "canonical"],
    )

    op.create_table(
        "knowledge_edges",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("admin_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("instance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("src_entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dst_entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("relationship_type", sa.String(64), nullable=False),
        sa.Column(
            "attributes",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_edges_admin_instance", "knowledge_edges", ["admin_id", "instance_id"])
    op.create_index("ix_edges_source", "knowledge_edges", ["source_id"])
    # Forward traversal (recursive CTE walks src→dst) keyed by the source entity.
    op.create_index("ix_edges_src", "knowledge_edges", ["admin_id", "instance_id", "src_entity_id"])

    _apply_rls("knowledge_entities")
    _apply_rls("knowledge_edges")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON knowledge_edges")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON knowledge_entities")
    op.drop_table("knowledge_edges")
    op.drop_table("knowledge_entities")
