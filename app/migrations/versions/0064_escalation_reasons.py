"""escalation reasons: why a signal fired, in the owner's words

Round 6 WP-G (2026-09-08 audit F124). ``escalation_events.reasons`` carries the short
deterministic phrases behind a fired signal ("named a budget", "wants it soon", "answer
only 30% backed by your knowledge") so the dashboard explains an escalation instead of
showing a bare score. NULL on rows from before the column existed.

Revision ID: 0064_escalation_reasons
Revises: 0063_contact_confirmation
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0064_escalation_reasons"
down_revision: str | None = "0063_contact_confirmation"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "escalation_events",
        sa.Column("reasons", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("escalation_events", "reasons")
