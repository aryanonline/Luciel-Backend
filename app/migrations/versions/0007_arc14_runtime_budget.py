"""arc14 runtime: conversation_budgets table (budget gate) + RLS

The conversation budget counter (Architecture §3.4.1b), keyed by
(admin_id, instance_id, billing_period_start). Postgres is the durable source of
truth; Redis is the hot cache (managed in app/runtime/budget.py). Tenant-scoped
with the three-layer Layer-2 RLS so isolation tests auto-cover it.

The other Arc 14 entities (sessions, transcripts, session_summaries,
escalation_events) already exist from migration 0001 — no new tables for them.

Revision ID: 0007_arc14_runtime_budget
Revises: 0006_arc13_channel_routing
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_arc14_runtime_budget"
down_revision: str | None = "0006_arc13_channel_routing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None

APP_ROLE = "luciel_app"


def upgrade() -> None:
    op.create_table(
        "conversation_budgets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("admin_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("instance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("billing_period_start", sa.Date(), nullable=False),
        sa.Column("counted_conversations", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "counted_session_ids",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_budget_admin_instance", "conversation_budgets", ["admin_id", "instance_id"])
    op.execute(
        "CREATE UNIQUE INDEX uq_budget_period ON conversation_budgets "
        "(admin_id, instance_id, billing_period_start)"
    )
    predicate = "admin_id = NULLIF(current_setting('app.admin_id', true), '')::uuid"
    op.execute("ALTER TABLE conversation_budgets ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE conversation_budgets FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON conversation_budgets "
        f"USING ({predicate}) WITH CHECK ({predicate})"
    )
    op.execute(f"REVOKE ALL ON conversation_budgets FROM {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON conversation_budgets TO {APP_ROLE}")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON conversation_budgets")
    op.drop_table("conversation_budgets")
