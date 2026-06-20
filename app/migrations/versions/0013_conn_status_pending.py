"""widen instance_connections.status + add pending_carrier_registration (Axis6-F02/Axis10-F03)

Architecture §3.1.6 / Decision §46: SMS/Voice numbers are provisioned instantly but
cannot send until A2P 10DLC carrier registration is approved. The channel must show a
`pending_carrier_registration` status (not "connected") until the carrier approves, so
the admin never sees a live-looking channel that silently drops messages.

The existing `instance_connections.status` column is VARCHAR(16); the new value is 28
chars, so this migration widens the column to VARCHAR(32) (expand-only, backward
compatible — no data rewrite). The model enum + the wire enum (luciel.py
ConnectionStatusWire) + the frontend Zod connectionStatus enum are updated in lockstep.

Revision ID: 0013_conn_status_pending
Revises: 0012_internal_access_log
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_conn_status_pending"
down_revision: str | None = "0012_internal_access_log"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    # Expand the status column to fit `pending_carrier_registration` (28 chars).
    op.alter_column(
        "instance_connections",
        "status",
        type_=sa.String(32),
        existing_type=sa.String(16),
        existing_nullable=False,
        existing_server_default="unconfigured",
    )


def downgrade() -> None:
    # Contract back to VARCHAR(16). Any pending_carrier_registration rows must be
    # resolved first; this is a dev-only rollback path.
    op.alter_column(
        "instance_connections",
        "status",
        type_=sa.String(16),
        existing_type=sa.String(32),
        existing_nullable=False,
        existing_server_default="unconfigured",
    )
