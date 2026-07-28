"""lead auto-prune rule: instances.lead_retention_days (NULL = off)

The opt-in lead auto-prune rule of Architecture §3.4.10a / Legal §B5. Captured Lead
records are the Admin's own business data, so the platform must NOT timer-delete them
the way it does transcripts and summaries; §3.4.10a therefore makes the auto-rule
"automatically delete leads inactive for N months" an explicit Admin opt-in that is
OFF BY DEFAULT. NULL encodes off, which is why the column is nullable with no server
default — every existing row reads as "the Admin never asked for this".

The window is measured against ``leads.last_activity_at`` (inactivity), not
``created_at``: §3.4.10a defines a stale lead as one with "no activity (no
conversation, no update) for a configurable window".

Expand-only: one nullable column, no backfill, no data touched.

Revision ID: 0020_lead_retention_rule
Revises: 0019_audit_truncate_guards
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_lead_retention_rule"
down_revision: str | None = "0019_audit_truncate_guards"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("instances", sa.Column("lead_retention_days", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("instances", "lead_retention_days")
