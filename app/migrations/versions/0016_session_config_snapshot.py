"""add sessions.config_snapshot jsonb (§3.7.1/§3.8.7 rule E, Decision #39)

A conversation snapshots its EFFECTIVE additive/neutral config (enabled tools,
personality, enabled channels) when it BEGINS. Additive pillar changes made while a
conversation is in flight apply to NEW conversations only — they must not shift an
in-flight conversation mid-sentence. Removative/safety changes (disable/revoke,
connection-health failure) stay LIVE via the broker gates. This add-column-only,
nullable migration stores that snapshot; NULL rows fall back to live config
(backward compatible with sessions opened before this column existed).

Numbering note: other unmerged branches already used 0015 twice; this revision is
0016 with down_revision pinned to the current main head (0014) to avoid collision.

Revision ID: 0016_session_config_snapshot
Revises: 0014_sync_status_rename
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016_session_config_snapshot"
down_revision: str | None = "0015_conn_status_email_routing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column("config_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("sessions", "config_snapshot")
