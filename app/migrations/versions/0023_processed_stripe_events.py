"""processed_stripe_events: close the Stripe webhook replay window (§3.4.1b)

Redelivery was gated on ``billing_accounts.last_stripe_event_id`` — a ONE-DEEP window on
a single nullable column, so an A → B → A redelivery reprocessed A. Only the audit
emissions had been made replay-safe; the state mutations had not, and each is destructive
on a second application: ``invoice.paid`` deletes the usage counter again (a PAYG tenant
who has run 180 conversations since the invoice loses all 180 and the next 130 are never
billed), ``payment_method.detached`` flips a re-carded account back to free_cap and
re-dormants its tools, ``invoice.payment_failed`` re-advances dunning and re-emails.

The event id as primary key makes INSERT ... ON CONFLICT DO NOTHING the entire gate.
Existing last_stripe_event_id values are backfilled so the event in flight at deploy time
is not re-applied.

Revision ID: 0023_processed_stripe_events
Revises: 0022_counted_sessions
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0023_processed_stripe_events"
down_revision: str | None = "0022_counted_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None

APP_ROLE = "luciel_app"


def upgrade() -> None:
    op.create_table(
        "processed_stripe_events",
        sa.Column("event_id", sa.String(255), primary_key=True),
        sa.Column("admin_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_processed_stripe_events_admin_id", "processed_stripe_events", ["admin_id"])

    # Backfill the one-deep window so the event in flight at deploy is not re-applied.
    op.execute(
        """
        INSERT INTO processed_stripe_events (event_id, admin_id, processed_at)
        SELECT b.last_stripe_event_id, b.admin_id, now()
          FROM billing_accounts b
         WHERE b.last_stripe_event_id IS NOT NULL
        ON CONFLICT DO NOTHING
        """
    )

    predicate = "admin_id = NULLIF(current_setting('app.admin_id', true), '')::uuid"
    op.execute("ALTER TABLE processed_stripe_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE processed_stripe_events FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON processed_stripe_events "
        f"USING ({predicate}) WITH CHECK ({predicate})"
    )
    op.execute(f"REVOKE ALL ON processed_stripe_events FROM {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON processed_stripe_events TO {APP_ROLE}")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON processed_stripe_events")
    op.drop_table("processed_stripe_events")
