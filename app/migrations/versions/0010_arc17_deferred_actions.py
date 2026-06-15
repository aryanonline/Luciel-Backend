"""arc17 connections fail-safe: deferred_actions retry queue + RLS

The bounded deferred-action retry queue (Architecture §3.8.7 rule D). Deferred /
outbound actions (schedule_callback delivery §3.3.5, accepted send_email/send_sms
§3.3.6/§4.5) that hit an unhealthy delivery connection are queued here rather than
dropped, retried with backoff when the connection recovers, and abandoned-with-
owner-notification once past their 24h max-age TTL. TenantMixin + the three-layer
Layer-2 RLS so the isolation sweep auto-covers it.

The instance_connections table itself already exists (Arc 12 / earlier); this arc
adds ONLY the fail-safe queue table — no schema change to instance_connections is
required for Arc 17.

Indexes:
  - (admin_id, instance_id) for tenant-scoped lookups (§3.8 indexes commitment),
  - (status, next_attempt_at) so the worker sweep selects due queued items cheaply.

Expand-only.

Revision ID: 0010_arc17_deferred_actions
Revises: 0009_arc16_graph_store
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010_arc17_deferred_actions"
down_revision: str | None = "0009_arc16_graph_store"
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
        "deferred_actions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("admin_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("instance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action_type", sa.String(64), nullable=False),
        sa.Column("connection_type", sa.String(32), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_deferred_admin_instance", "deferred_actions", ["admin_id", "instance_id"])
    # The worker sweep selects due, still-queued items.
    op.create_index("ix_deferred_due", "deferred_actions", ["status", "next_attempt_at"])

    _apply_rls("deferred_actions")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON deferred_actions")
    op.drop_table("deferred_actions")
