"""booking_events — the durable store behind the Appointments-booked metric (§3.9)

The analytics card read a trace-store table that never existed and served an
honest gap forever. One row per successful ``book_appointment`` dispatch,
written by the tool broker, gives the card a real number fed by the same event
that told the customer "booked". RLS ENABLE+FORCE + tenant policy like every
tenant table; joined to the delete cascade.

Revision ID: 0041_booking_events
Revises: 0040_crm_links_and_record_source
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0041_booking_events"
down_revision: str | None = "0040_crm_links_and_record_source"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None

APP_ROLE = "luciel_app"
_PREDICATE = "admin_id = NULLIF(current_setting('app.admin_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "booking_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("admin_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("instance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("booking_ref", sa.String(length=128), nullable=False, server_default=""),
        sa.Column(
            "booked_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_booking_events_admin_id", "booking_events", ["admin_id"])
    op.execute("ALTER TABLE booking_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE booking_events FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON booking_events "
        f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
    )
    op.execute(f"REVOKE ALL ON booking_events FROM {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON booking_events TO {APP_ROLE}")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON booking_events")
    op.drop_table("booking_events")
