"""rename knowledge_sources.sync_status needs_reconnect -> paused_reconnect_needed (Axis4-F02)

Architecture §3.2.3 specifies the sync_status vocabulary as
``synced | syncing | paused_reconnect_needed | error``. The code previously wrote
``needs_reconnect``; this data migration renames any existing rows so the stored
values match the canonical enum (the wire enum + frontend Zod enum are updated in
lockstep). The column is already VARCHAR(32) (no type change needed); ``error`` is
an additive value with no stored rows to migrate.

Revision ID: 0014_sync_status_rename
Revises: 0013_conn_status_pending
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0014_sync_status_rename"
down_revision: str | None = "0013_conn_status_pending"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        "UPDATE knowledge_sources "
        "SET sync_status = 'paused_reconnect_needed' "
        "WHERE sync_status = 'needs_reconnect'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE knowledge_sources "
        "SET sync_status = 'needs_reconnect' "
        "WHERE sync_status = 'paused_reconnect_needed'"
    )
