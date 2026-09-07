"""team availability: when the HUMAN team is reachable (never Luciel's hours)

Round 6 WP-E (2026-09-07 audit F054). Luciel is on duty around the clock on every
channel and nothing here ever makes it stop answering. ``team_availability`` is an
optional description of when the owner's people can pick up a hand-off, used only to
(a) word follow-up promises honestly ("someone will follow up tomorrow at 9:00 am")
and (b) route an after-hours escalation to a different contact. NULL (the default and
every existing row) keeps today's generic wording and routing.

``timezone`` is the owner's IANA zone — the team-availability windows are read in it,
and the daily brief (WP-F) is sent at morning local time. ``business_short_name`` is
the name Luciel calls the business in greetings ("Acme" rather than "Acme Patios &
Decks Ltd."); ``daily_brief_enabled`` is the owner's opt-out of the morning brief
(WP-F), on by default.

Revision ID: 0061_team_availability
Revises: 0060_embed_allowed_origins
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0061_team_availability"
down_revision: str | None = "0060_embed_allowed_origins"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("instances", sa.Column("timezone", sa.String(length=64), nullable=True))
    op.add_column(
        "instances",
        sa.Column("team_availability", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "instances", sa.Column("business_short_name", sa.String(length=80), nullable=True)
    )
    op.add_column(
        "instances",
        sa.Column("daily_brief_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def downgrade() -> None:
    op.drop_column("instances", "daily_brief_enabled")
    op.drop_column("instances", "business_short_name")
    op.drop_column("instances", "team_availability")
    op.drop_column("instances", "timezone")
