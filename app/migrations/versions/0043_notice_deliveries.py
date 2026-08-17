"""notice_deliveries + billing_notices candidates (Legal §A5 / §A10 email half)

Two enablers for the daily billing-notice email sweep:

  * ``notice_deliveries`` — the once-only ledger. Sweeps deliver at-least-once,
    so "this tenant already got this notice" must be a durable fact with a
    unique constraint doing the idempotency, not an in-memory check.
  * ``platform_sweep_candidates`` gains a ``billing_notices`` branch: every
    VERIFIED account. Notices are account-level (§A5 owes them to every
    existing account), so the candidate set is simply the accounts that can
    receive mail; the per-tenant worker decides cheaply whether anything is
    actually due.

Follows the 0018/0034/0038/0042 pattern: replace the candidates function
wholesale, restate owner + grant.

Revision ID: 0043_notice_deliveries
Revises: 0042_knowledge_sync_prunes
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0043_notice_deliveries"
down_revision: str | None = "0042_knowledge_sync_prunes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None

APP_ROLE = "luciel_app"
_PREDICATE = "admin_id = NULLIF(current_setting('app.admin_id', true), '')::uuid"

_COMMON = """
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
        UNION
        SELECT a.admin_id FROM accounts a
         WHERE p_kind = 'unverified_reap' AND a.state = 'unverified'
        UNION
        SELECT d.admin_id FROM deferred_actions d
         WHERE p_kind = 'deferred' AND d.status = 'queued'
        UNION
        SELECT c.admin_id FROM instance_connections c
         WHERE p_kind = 'health' AND c.status <> 'revoked'
        UNION
        SELECT a.admin_id FROM accounts a
         WHERE p_kind = 'audit_retention'
           AND a.created_at < now() - interval '365 days'
        UNION
        SELECT i.admin_id FROM instances i
         WHERE p_kind = 'session_finalize' AND i.state IN ('active', 'paused')
        UNION
        SELECT i.admin_id FROM instances i
         WHERE p_kind = 'transcript_archive' AND i.state IN ('active', 'paused')
        UNION
        SELECT c.admin_id FROM instance_connections c
         WHERE p_kind = 'mailbox_poll'
           AND c.connection_type = 'email_sender'
           AND c.provider = 'outlook'
           AND c.status = 'connected'
        UNION
        SELECT c.admin_id FROM instance_connections c
         WHERE p_kind = 'knowledge_sync'
           AND c.connection_type = 'knowledge_source'
           AND c.status = 'connected'
"""

_BILLING_NOTICES = """
        UNION
        SELECT a.admin_id FROM accounts a
         WHERE p_kind = 'billing_notices' AND a.state = 'verified'
"""

_HEADER = """
    CREATE OR REPLACE FUNCTION platform_sweep_candidates(p_kind text)
    RETURNS TABLE(admin_id uuid)
    LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
"""

_FOOTER = "    $$;"


def _replace(body: str) -> None:
    op.execute(_HEADER + body + _FOOTER)
    op.execute("ALTER FUNCTION platform_sweep_candidates(text) OWNER TO luciel_auth")
    op.execute("GRANT EXECUTE ON FUNCTION platform_sweep_candidates(text) TO luciel_app")


def upgrade() -> None:
    op.create_table(
        "notice_deliveries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("admin_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("notice_id", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("delivery_status", sa.String(length=16), nullable=False),
        sa.Column(
            "sent_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("admin_id", "notice_id", name="uq_notice_delivery"),
    )
    op.create_index("ix_notice_deliveries_admin_id", "notice_deliveries", ["admin_id"])
    op.execute("ALTER TABLE notice_deliveries ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE notice_deliveries FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON notice_deliveries "
        f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
    )
    op.execute(f"REVOKE ALL ON notice_deliveries FROM {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON notice_deliveries TO {APP_ROLE}")

    _replace(_COMMON + _BILLING_NOTICES)


def downgrade() -> None:
    _replace(_COMMON)
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON notice_deliveries")
    op.drop_table("notice_deliveries")
