"""usage watermark: stop losing whole billable blocks (§3.4.1b, P1-12 / P1-13)

Reporting fired exactly once, on the boundary conversation, and swallowed every
exception. A Stripe blip at conversation 151 meant block 2 was never billed and nothing
ever went back for it — there was no record that a report was owed.

``last_reported_blocks`` is that record: reporting becomes "post whenever the accrued
total exceeds what Stripe has been told", so a failed post is simply retried on the next
turn or by the reconcile sweep.

``accrued_blocks`` is the period high-water mark, and it is what closes the free-usage
path: ``build_budget`` reports zero blocks once the card is gone, so a tenant who ran 250
conversations and then detached had two real blocks silently voided. The accrual is
recorded while the card is live and never walks backwards within a period.

The reconcile sweep is the backstop for a tenant who crosses a block and then stops
conversing; it needs the cross-tenant candidate list, so ``platform_sweep_candidates``
gains the kind.

Revision ID: 0025_usage_watermark
Revises: 0024_stripe_customer_lookup
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025_usage_watermark"
down_revision: str | None = "0024_stripe_customer_lookup"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "billing_accounts",
        sa.Column("accrued_blocks", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "billing_accounts",
        sa.Column("last_reported_blocks", sa.Integer(), nullable=False, server_default="0"),
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION platform_sweep_candidates(p_kind text)
        RETURNS TABLE(admin_id uuid)
        LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
            SELECT i.admin_id FROM instances i
             WHERE p_kind = 'retention' AND i.state IN ('active', 'paused')
            UNION
            SELECT i.admin_id FROM instances i
             WHERE p_kind = 'grace_expiry' AND i.state = 'luciel_grace_window'
            UNION
            SELECT a.admin_id FROM accounts a
             WHERE p_kind = 'close_purge' AND a.state = 'closed'
            UNION
            SELECT b.admin_id FROM billing_accounts b
             WHERE p_kind = 'dunning' AND b.dunning_state = 'retrying'
            UNION
            SELECT b.admin_id FROM billing_accounts b
             WHERE p_kind = 'usage_reconcile'
               AND b.accrued_blocks > b.last_reported_blocks
        $$;
        """
    )
    op.execute("ALTER FUNCTION platform_sweep_candidates(text) OWNER TO luciel_auth")
    op.execute("GRANT EXECUTE ON FUNCTION platform_sweep_candidates(text) TO luciel_app")


def downgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION platform_sweep_candidates(p_kind text)
        RETURNS TABLE(admin_id uuid)
        LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
            SELECT i.admin_id FROM instances i
             WHERE p_kind = 'retention' AND i.state IN ('active', 'paused')
            UNION
            SELECT i.admin_id FROM instances i
             WHERE p_kind = 'grace_expiry' AND i.state = 'luciel_grace_window'
            UNION
            SELECT a.admin_id FROM accounts a
             WHERE p_kind = 'close_purge' AND a.state = 'closed'
            UNION
            SELECT b.admin_id FROM billing_accounts b
             WHERE p_kind = 'dunning' AND b.dunning_state = 'retrying'
        $$;
        """
    )
    op.execute("ALTER FUNCTION platform_sweep_candidates(text) OWNER TO luciel_auth")
    op.drop_column("billing_accounts", "last_reported_blocks")
    op.drop_column("billing_accounts", "accrued_blocks")
