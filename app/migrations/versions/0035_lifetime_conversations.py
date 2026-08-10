"""billing_accounts.lifetime_conversations — an all-time count that survives resets

``conversations_total`` on the analytics overview claimed "all-time billed
conversations" but was computed as ``COUNT(*) FROM counted_sessions`` — and the
``invoice.paid`` webhook DELETES every counted_sessions row for the tenant when a
period closes (the correct fire-once ledger reset, §3.4.1b). "All-time" therefore
silently reset to zero every billing period.

The fix is a monotonic lifetime counter on the billing account, incremented inside
``count_session_once`` in the same transaction as the ledger claim (so it inherits
the fire-once idempotency: a session that loses the on-conflict claim adds nothing).
The ledger keeps its reset semantics; the lifetime number survives them.

Backfill is best-effort honest: current ``counted_sessions`` rows are the only
billed-conversation record that still exists (prior periods' rows were already
deleted by their resets), so the backfilled number is a FLOOR for accounts with
history, exact for accounts inside their first period. The migration cannot invent
the deleted history and does not pretend to.

Tenants with no billing_accounts row (never opened billing) accrue from the row's
creation; the analytics read falls back to the counted_sessions count when no
billing row exists, so their total remains correct too.

Revision ID: 0035_lifetime_conversations
Revises: 0034_new_sweep_candidates
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035_lifetime_conversations"
down_revision: str | None = "0034_new_sweep_candidates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "billing_accounts",
        sa.Column("lifetime_conversations", sa.Integer(), nullable=False, server_default="0"),
    )
    op.execute(
        """
        UPDATE billing_accounts b
           SET lifetime_conversations = (
               SELECT count(*) FROM counted_sessions c WHERE c.admin_id = b.admin_id
           )
        """
    )


def downgrade() -> None:
    op.drop_column("billing_accounts", "lifetime_conversations")
