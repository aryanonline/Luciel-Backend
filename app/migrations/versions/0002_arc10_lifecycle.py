"""arc10 lifecycle: instance config columns + the remaining tenant tables + RLS

Adds the five-pillar config columns to instances, and creates the tenant tables
the hard-delete cascade (Architecture §3.6.5) and later arcs operate over:
knowledge_sources, knowledge_chunks, sessions, transcripts, session_summaries,
instance_connections, escalation_events. All get the three-layer Layer-2 RLS.

Expand-only: additive columns + new tables (no destructive change). Audit-log
untouched.

Revision ID: 0002_arc10_lifecycle
Revises: 0001_arc9_foundation
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_arc10_lifecycle"
down_revision: str | None = "0001_arc9_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None

APP_ROLE = "luciel_app"

NEW_TENANT_TABLES = [
    "knowledge_sources",
    "knowledge_chunks",
    "sessions",
    "transcripts",
    "session_summaries",
    "instance_connections",
    "escalation_events",
]


def _install_rls(table: str) -> None:
    predicate = "admin_id = NULLIF(current_setting('app.admin_id', true), '')::uuid"
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} USING ({predicate}) WITH CHECK ({predicate})"
    )
    op.execute(f"REVOKE ALL ON {table} FROM {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")


def upgrade() -> None:
    # --- instances: five-pillar config + soft-delete ---
    op.add_column(
        "instances", sa.Column("channels", postgresql.JSONB, nullable=False, server_default="[]")
    )
    op.add_column(
        "instances", sa.Column("tools", postgresql.JSONB, nullable=False, server_default="[]")
    )
    op.add_column(
        "instances", sa.Column("escalation", postgresql.JSONB, nullable=False, server_default="{}")
    )
    op.add_column(
        "instances", sa.Column("personality", postgresql.JSONB, nullable=False, server_default="{}")
    )
    op.add_column(
        "instances", sa.Column("soft_deleted_at", sa.DateTime(timezone=True), nullable=True)
    )

    uuid_col = lambda **kw: sa.Column(postgresql.UUID(as_uuid=True), **kw)  # noqa: E731

    # --- knowledge_sources ---
    op.create_table(
        "knowledge_sources",
        uuid_col(name="id", primary_key=True),
        uuid_col(name="admin_id", nullable=False),
        uuid_col(name="instance_id", nullable=False),
        sa.Column("name", sa.String(512), nullable=False),
        sa.Column("origin", sa.String(32), nullable=False),
        sa.Column("ingestion_status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("size_bytes", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("source_origin", sa.String(32), nullable=True),
        sa.Column("external_id", sa.String(1024), nullable=True),
        uuid_col(name="connection_id", nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sync_status", sa.String(32), nullable=True),
        sa.Column("soft_deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("original_object_key", sa.String(1024), nullable=True),
        sa.Column(
            "last_updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_ks_admin_instance", "knowledge_sources", ["admin_id", "instance_id"])

    # --- knowledge_chunks (pgvector embedding lands in Arc 11; column added there) ---
    op.create_table(
        "knowledge_chunks",
        uuid_col(name="id", primary_key=True),
        uuid_col(name="admin_id", nullable=False),
        uuid_col(name="instance_id", nullable=False),
        uuid_col(name="source_id", nullable=False),
        sa.Column("ordinal", sa.Integer, nullable=False, server_default="0"),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_kc_admin_instance", "knowledge_chunks", ["admin_id", "instance_id"])
    op.create_index("ix_kc_source", "knowledge_chunks", ["source_id"])

    # --- sessions ---
    op.create_table(
        "sessions",
        uuid_col(name="id", primary_key=True),
        uuid_col(name="admin_id", nullable=False),
        uuid_col(name="instance_id", nullable=False),
        uuid_col(name="resolved_lead_id", nullable=True),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False, server_default="ai"),
        sa.Column("counted", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "last_message_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("summary", sa.Text, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_sessions_admin_instance", "sessions", ["admin_id", "instance_id"])

    # --- transcripts ---
    op.create_table(
        "transcripts",
        uuid_col(name="id", primary_key=True),
        uuid_col(name="admin_id", nullable=False),
        uuid_col(name="instance_id", nullable=False),
        uuid_col(name="session_id", nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_transcripts_session", "transcripts", ["admin_id", "session_id"])

    # --- session_summaries ---
    op.create_table(
        "session_summaries",
        uuid_col(name="id", primary_key=True),
        uuid_col(name="admin_id", nullable=False),
        uuid_col(name="instance_id", nullable=False),
        uuid_col(name="resolved_lead_id", nullable=True),
        uuid_col(name="session_id", nullable=False),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_summaries_lead", "session_summaries", ["admin_id", "resolved_lead_id"])

    # --- instance_connections (Architecture §3.8.2) ---
    op.create_table(
        "instance_connections",
        uuid_col(name="connection_id", primary_key=True),
        uuid_col(name="admin_id", nullable=False),
        uuid_col(name="instance_id", nullable=False),
        sa.Column("connection_type", sa.String(32), nullable=False),
        sa.Column("auth_class", sa.String(32), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="unconfigured"),
        sa.Column("secret_ref", sa.String(512), nullable=True),
        sa.Column("non_secret_config", postgresql.JSONB, nullable=True),
        uuid_col(name="created_by_user_id", nullable=True),
        sa.Column("last_health_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status_detail", sa.Text, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_conn_admin_instance", "instance_connections", ["admin_id", "instance_id"])
    # single-active-per-type (Architecture §3.8.2): unique over non-revoked rows
    op.execute(
        "CREATE UNIQUE INDEX uq_conn_active_type ON instance_connections "
        "(admin_id, instance_id, connection_type) WHERE status <> 'revoked'"
    )

    # --- escalation_events (Architecture §3.5.2) ---
    op.create_table(
        "escalation_events",
        uuid_col(name="escalation_id", primary_key=True),
        uuid_col(name="admin_id", nullable=False),
        uuid_col(name="instance_id", nullable=False),
        uuid_col(name="session_id", nullable=True),
        uuid_col(name="resolved_lead_id", nullable=True),
        sa.Column("signal", sa.String(32), nullable=False),
        sa.Column("gate", sa.String(16), nullable=False),
        sa.Column("score_or_confidence", sa.Float, nullable=True),
        sa.Column(
            "fired_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_esc_admin_instance", "escalation_events", ["admin_id", "instance_id"])

    for t in NEW_TENANT_TABLES:
        _install_rls(t)


def downgrade() -> None:
    for t in NEW_TENANT_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {t}")
    op.execute("DROP INDEX IF EXISTS uq_conn_active_type")
    for t in reversed(NEW_TENANT_TABLES):
        op.drop_table(t)
    for col in ("soft_deleted_at", "personality", "escalation", "tools", "channels"):
        op.drop_column("instances", col)
