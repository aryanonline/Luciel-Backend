"""add deferred_actions.resolved_lead_id for schedule_callback per-lead cap (P0-3)

Architecture §3.3.5 / Decision #51: schedule_callback is rate-limited per
(instance_id, resolved_lead_id) at scheduling time. To count pending callbacks per
lead we persist the resolved lead identity on the deferred_actions row.

Add-column-only, expand-only: the column is NULLABLE (existing rows and sessions
without a resolved identity carry NULL) so this needs no data rewrite and is fully
backward compatible. deferred_actions already carries admin_id + the three-layer
Layer-2 RLS (Arc 17 migration 0010); this migration does NOT touch tenant isolation.
An index on (admin_id, instance_id, resolved_lead_id) keeps the per-lead count cheap.

Revision ID: 0015_deferred_resolved_lead
Revises: 0014_sync_status_rename
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015_deferred_resolved_lead"
down_revision: str | None = "0014_sync_status_rename"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "deferred_actions",
        sa.Column("resolved_lead_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index(
        "ix_deferred_lead",
        "deferred_actions",
        ["admin_id", "instance_id", "resolved_lead_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_deferred_lead", table_name="deferred_actions")
    op.drop_column("deferred_actions", "resolved_lead_id")
