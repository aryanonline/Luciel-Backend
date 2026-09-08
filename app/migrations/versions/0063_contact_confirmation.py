"""contact confirmation: which identifiers the customer confirmed, and a parked answer

Round 6 WP-H (2026-09-08 audit F060). Nothing is sent to an address the customer has
not confirmed. ``lead_identifiers.confirmed_at`` / ``source`` (transport | volunteered)
record that confirmation — an identifier that arrived with the channel itself is
confirmed at capture; one typed in conversation is confirmed when the customer says
so. ``sessions.pending_outbound`` parks an answer the customer asked to receive on
another channel until they confirm or supply where it goes.

Revision ID: 0063_contact_confirmation
Revises: 0062_daily_brief_candidates
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0063_contact_confirmation"
down_revision: str | None = "0062_daily_brief_candidates"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "lead_identifiers",
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("lead_identifiers", sa.Column("source", sa.String(length=16), nullable=True))
    op.add_column(
        "sessions",
        sa.Column("pending_outbound", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("sessions", "pending_outbound")
    op.drop_column("lead_identifiers", "source")
    op.drop_column("lead_identifiers", "confirmed_at")
