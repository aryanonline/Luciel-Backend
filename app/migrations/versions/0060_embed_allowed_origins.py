"""embed allowed origins: the websites a Luciel's widget may load on

Round 6 WP-I (2026-09-07 audit F066): the embed key is public by design, and the
edge WAF never enforced the Origin/Referer allowlist its header claimed, so any page
that copied a customer's key could load — and bill — their chat. ``allowed_origins``
is the owner's list of website origins (``scheme://host[:port]``, at most ten); the
widget data plane refuses every other page origin with a 403. NULL (the default and
the state of every existing row) keeps today's behaviour: any page.

Revision ID: 0060_embed_allowed_origins
Revises: 0059_lead_stewardship
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0060_embed_allowed_origins"
down_revision: str | None = "0059_lead_stewardship"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "instances",
        sa.Column("allowed_origins", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("instances", "allowed_origins")
