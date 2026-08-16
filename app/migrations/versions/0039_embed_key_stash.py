"""instances gain embed_key_stashed — restore revives the SAME embed key (§3.6.4)

Delete's quiesce revokes the embed key by nulling ``embed_key_public_id`` (widget
resolution keys on that column, so a stashed key accepts no inbound). Before this
column the value was simply discarded and restore minted a fresh key — which
permanently killed the snippet already deployed on the customer's website, quietly
contradicting the "restore returns it exactly as it was" commitment. The stash
holds the value through the grace window; restore moves it back and clears the
stash; the day-30 cascade deletes the instance row, stash included.

Expand-only: nullable column, no backfill (rows already in grace before this
migration keep the fresh-key fallback in ``state_machine.restore``).

Revision ID: 0039_embed_key_stash
Revises: 0038_mailbox_poll_candidates
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0039_embed_key_stash"
down_revision: str | None = "0038_mailbox_poll_candidates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("instances", sa.Column("embed_key_stashed", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("instances", "embed_key_stashed")
