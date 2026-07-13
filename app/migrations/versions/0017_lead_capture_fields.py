"""add leads.lead_score + leads.opted_out (§3.4.4/§3.4.5, §3.3.5 opt-out)

Lead-capture cognition (§3.4.4) now writes/maintains a real Lead record. Two
add-column-only, backward-compatible fields support it:
  - lead_score (float, nullable): the normalized high-value-lead score at capture
    (§3.4.5); NULL when captured without a scored high-value signal.
  - opted_out (bool, not null, default false): channel opt-out (STOP/HELP, §3.1.6);
    an opted-out lead is never sent a scheduled callback regardless of caps (§3.3.5).

Expand-contract safe: both columns are additive; opted_out has a server default so
existing rows backfill to false without a rewrite.

Revision ID: 0017_lead_capture_fields
Revises: 0016_session_config_snapshot
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_lead_capture_fields"
down_revision: str | None = "0016_session_config_snapshot"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("leads", sa.Column("lead_score", sa.Float(), nullable=True))
    op.add_column(
        "leads",
        sa.Column(
            "opted_out", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )


def downgrade() -> None:
    op.drop_column("leads", "opted_out")
    op.drop_column("leads", "lead_score")
