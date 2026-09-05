"""billing period truth — counter idempotency across periods + accrual period stamp

Two money-path corrections from the 2026-09-05 product-owner audit (§3.4.1b):

  * ``counted_sessions`` was unique per (session, billing period), so a conversation
    that straddled a period boundary was counted — and billed — once in each period
    (F029). The fire-once key is now (admin, instance, session); the period column stays
    as the record of which period the count landed in. Duplicates that the old key let
    in are collapsed to the earliest row before the new constraint is created.

  * ``billing_accounts.accrual_period_start`` records which period the usage watermarks
    (``accrued_blocks`` / ``last_reported_blocks``) belong to, so metering can roll them
    when a period ends without an ``invoice.paid`` (F030/F004) instead of under-billing
    the next period until its blocks exceeded the previous total.

Revision ID: 0055_billing_period_truth
Revises: 0054_email_delivery_incidents
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0055_billing_period_truth"
down_revision: str | None = "0054_email_delivery_incidents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    # Collapse cross-period duplicates to the earliest counted row: that is the count
    # the tenant was actually billed for first; the later row is the double count.
    op.execute(
        """
        DELETE FROM counted_sessions a
        USING counted_sessions b
        WHERE a.admin_id = b.admin_id
          AND a.instance_id = b.instance_id
          AND a.session_id = b.session_id
          AND (a.created_at > b.created_at OR (a.created_at = b.created_at AND a.id > b.id))
        """
    )
    # 0022 created the fire-once guard as a unique INDEX (not a table constraint), so
    # it is dropped as one; the replacement is an index too — ON CONFLICT resolves
    # index_elements against either.
    op.execute("DROP INDEX IF EXISTS uq_counted_session")
    op.execute(
        "CREATE UNIQUE INDEX uq_counted_session_once ON counted_sessions "
        "(admin_id, instance_id, session_id)"
    )
    op.add_column(
        "billing_accounts",
        sa.Column("accrual_period_start", sa.Date(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("billing_accounts", "accrual_period_start")
    op.execute("DROP INDEX IF EXISTS uq_counted_session_once")
    op.execute(
        "CREATE UNIQUE INDEX uq_counted_session ON counted_sessions "
        "(admin_id, instance_id, session_id, billing_period_start)"
    )
