"""tool_trace_events + retrieval_trace_events — the durable trace store (§5.4, C6)

§3.3.4 rule 3 (every outcome is traced) was satisfied only by an in-memory list
that died with the request, and §3.9's top-knowledge-sources metric plus the
§3.2.2 delete-confirmation tooltip had no store to read at all. Two PII-light
fact tables close all three. RLS ENABLE+FORCE + tenant policy like every tenant
table (0041 pattern); both join the delete cascade and the 1-year retention
sweep.

Revision ID: 0047_trace_store
Revises: 0046_session_ask_attempts
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0047_trace_store"
down_revision: str | None = "0046_session_ask_attempts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None

APP_ROLE = "luciel_app"
_PREDICATE = "admin_id = NULLIF(current_setting('app.admin_id', true), '')::uuid"


def _lock_down(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
    )
    op.execute(f"REVOKE ALL ON {table} FROM {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")


def upgrade() -> None:
    op.create_table(
        "tool_trace_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("admin_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("instance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("tool_id", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("connection_type", sa.String(length=32), nullable=True),
        sa.Column("missing_fields", postgresql.JSONB(), nullable=True),
        sa.Column("reason", sa.String(length=256), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_tool_trace_events_admin_at", "tool_trace_events", ["admin_id", "at"])
    _lock_down("tool_trace_events")

    op.create_table(
        "retrieval_trace_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("admin_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("instance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("transcript_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_retrieval_trace_events_admin_source_at",
        "retrieval_trace_events",
        ["admin_id", "source_id", "at"],
    )
    _lock_down("retrieval_trace_events")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON tool_trace_events")
    op.drop_table("tool_trace_events")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON retrieval_trace_events")
    op.drop_table("retrieval_trace_events")
