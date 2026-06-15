"""arc18 metering: billing_accounts (Stripe binding + dunning + period + pm display)

The per-tenant billing account (Architecture §3.4.1b / Vision §7). Holds the
Stripe pointers (customer/subscription ids — never the card), billingState
(free_cap | payg_enabled), dunningState (none | retrying | reduced), the
Stripe-billing-date-anchored period bounds, and the DISPLAY-ONLY payment-method
tuple (brand/last4/expMonth/expYear). HARD CONSTRAINT #2: no column can carry a
PAN/CVV — Stripe holds the card, we hold pointers + the display tuple.

TenantMixin + the three-layer Layer-2 RLS so the isolation sweep auto-covers it.
Unique on admin_id (one billing account per tenant).

Expand-only.

Revision ID: 0011_arc18_billing_accounts
Revises: 0010_arc17_deferred_actions
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011_arc18_billing_accounts"
down_revision: str | None = "0010_arc17_deferred_actions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None

APP_ROLE = "luciel_app"
_PREDICATE = "admin_id = NULLIF(current_setting('app.admin_id', true), '')::uuid"


def _apply_rls(table: str) -> None:
    """Install the three-layer Layer-2 RLS + standard grants (matches app/db/rls.py)."""
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
    )
    op.execute(f"REVOKE ALL ON {table} FROM {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")


def upgrade() -> None:
    op.create_table(
        "billing_accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("admin_id", postgresql.UUID(as_uuid=True), nullable=False),
        # Stripe pointers — never the card (§3.4.1b card hygiene).
        sa.Column("stripe_customer_id", sa.String(64), nullable=True),
        sa.Column("stripe_subscription_id", sa.String(64), nullable=True),
        sa.Column("billing_state", sa.String(16), nullable=False, server_default="free_cap"),
        sa.Column("dunning_state", sa.String(16), nullable=False, server_default="none"),
        sa.Column("dunning_started_at", sa.DateTime(timezone=True), nullable=True),
        # Stripe-billing-date-anchored period bounds (NOT calendar month).
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("period_resets_at", sa.DateTime(timezone=True), nullable=True),
        # Display-only payment-method tuple — NO PAN (§3.4.1b).
        sa.Column("pm_brand", sa.String(32), nullable=True),
        sa.Column("pm_last4", sa.String(4), nullable=True),
        sa.Column("pm_exp_month", sa.Integer(), nullable=True),
        sa.Column("pm_exp_year", sa.Integer(), nullable=True),
        # Webhook redelivery idempotency guard (§3.4.1b).
        sa.Column("last_stripe_event_id", sa.String(64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("admin_id", name="uq_billing_account_admin"),
    )
    op.create_index("ix_billing_accounts_admin_id", "billing_accounts", ["admin_id"])

    _apply_rls("billing_accounts")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON billing_accounts")
    op.drop_table("billing_accounts")
