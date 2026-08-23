"""leads.outcome — the admin-marked conversion dimension (Vision §7, C14)

Vision §7's "conversion by source / channel / service type" requires an
admin-marked lead outcome; ``Lead`` carried only the Arc-9 visibility state
(active/archived), so the analytics served an unconditional "conversion by
source isn't available yet" note to every account forever. This column is the
missing dimension: in_progress (default) / converted / lost, CHECK-constrained
to exactly the wire values.

Additive, expand-only (§5.9.1): server default backfills existing rows as
in_progress — the honest reading of a lead nobody has marked.

Revision ID: 0050_lead_outcome
Revises: 0049_connection_status_stash
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0050_lead_outcome"
down_revision: str | None = "0049_connection_status_stash"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "leads",
        sa.Column("outcome", sa.String(length=16), nullable=False, server_default="in_progress"),
    )
    op.create_check_constraint(
        "ck_leads_outcome",
        "leads",
        "outcome IN ('in_progress', 'converted', 'lost')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_leads_outcome", "leads")
    op.drop_column("leads", "outcome")
