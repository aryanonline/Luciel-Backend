"""counted_sessions: make the fire-once conversation counter atomic (§3.4.1b)

The counter was a read-modify-write over a JSONB list with no locking, at the engine's
READ COMMITTED default. Two concurrent turns on one session both saw the session id
absent and both incremented (one conversation billed twice, and the clobbered JSONB
write dropped ids so the session could be counted again later); two different sessions
racing both read N and wrote N+1 (silent under-billing); and two first-conversations of
a period both INSERTed the budget row, one hitting uq_budget_period and surfacing as a
500 on a real customer's message.

This table moves the fire-once decision into a unique constraint. The caller inserts
(admin_id, instance_id, session_id, billing_period_start) with ON CONFLICT DO NOTHING —
the rowcount is the answer — then increments with a single UPDATE ... SET n = n + 1.
The budget-row create becomes ON CONFLICT DO NOTHING for the same reason.

Existing counted_session_ids are backfilled so a session already counted under the old
guard is not counted a second time by the new one.

Revision ID: 0022_counted_sessions
Revises: 0021_meta_channel_destinations
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022_counted_sessions"
down_revision: str | None = "0021_meta_channel_destinations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None

APP_ROLE = "luciel_app"


def upgrade() -> None:
    op.create_table(
        "counted_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("admin_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("instance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("billing_period_start", sa.Date(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_counted_sessions_admin_id", "counted_sessions", ["admin_id"])
    # The fire-once guard itself — the increment is idempotent because this index is.
    op.execute(
        "CREATE UNIQUE INDEX uq_counted_session ON counted_sessions "
        "(admin_id, instance_id, session_id, billing_period_start)"
    )

    # Backfill from the JSONB list so a session counted under the old guard stays counted.
    op.execute(
        """
        INSERT INTO counted_sessions
            (id, admin_id, instance_id, session_id, billing_period_start)
        SELECT gen_random_uuid(), b.admin_id, b.instance_id, sid::uuid, b.billing_period_start
          FROM conversation_budgets b,
               LATERAL jsonb_array_elements_text(b.counted_session_ids) AS sid
        ON CONFLICT DO NOTHING
        """
    )

    predicate = "admin_id = NULLIF(current_setting('app.admin_id', true), '')::uuid"
    op.execute("ALTER TABLE counted_sessions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE counted_sessions FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON counted_sessions "
        f"USING ({predicate}) WITH CHECK ({predicate})"
    )
    op.execute(f"REVOKE ALL ON counted_sessions FROM {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON counted_sessions TO {APP_ROLE}")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON counted_sessions")
    op.drop_table("counted_sessions")
