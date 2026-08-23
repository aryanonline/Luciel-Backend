"""instance_connections status stash — restore returns pre-delete truth (§3.6.4, C8)

delete_luciel overwrote every connection's status with 'revoked' and stashed
nothing, so restore could only guess — and its guess was a blanket
'unconfigured', telling every restored owner to reconnect everything even
though the secrets were never destroyed during grace. §3.6.4 says a restored
Luciel comes back "exactly as it was": these two columns carry each
connection's pre-delete status/detail through the grace window so restore can
put the honest chip back (foreign-claimed destinations still stripped, c21).

Additive, expand-only (§5.9.1): nullable columns, no backfill — legacy rows
already in grace have no stash and keep the conservative 'unconfigured' path.

Revision ID: 0049_connection_status_stash
Revises: 0048_widget_pause_grace
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0049_connection_status_stash"
down_revision: str | None = "0048_widget_pause_grace"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "instance_connections", sa.Column("status_stashed", sa.String(length=32), nullable=True)
    )
    op.add_column(
        "instance_connections", sa.Column("status_detail_stashed", sa.Text(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("instance_connections", "status_detail_stashed")
    op.drop_column("instance_connections", "status_stashed")
