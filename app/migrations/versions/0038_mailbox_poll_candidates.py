"""Add the mailbox_poll branch to platform_sweep_candidates (§3.1.6a).

The BYO Outlook mailbox has no webhook — inbound mail arrives via a 5-minute delta
poll sweep. Its candidate discovery is deliberately narrow: only tenants with a
CONNECTED outlook email_sender row. Expired/revoked/ses rows are excluded so the
sweep never wakes a Fargate task for a tenant it cannot (or must not) poll, and a
platform with no mailbox tenants pays one index scan per run.

Follows the 0018/0034 pattern exactly: replace the SECURITY DEFINER function
wholesale, restate owner + grant (CREATE OR REPLACE preserves the owner, but
restating keeps the invariant visible at every revision).

Revision ID: 0038_mailbox_poll_candidates
Revises: 0037_email_destination_backfill
"""

from __future__ import annotations

from alembic import op

revision: str = "0038_mailbox_poll_candidates"
down_revision: str | None = "0037_email_destination_backfill"
branch_labels = None
depends_on = None

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
"""

_MAILBOX_POLL = """
        UNION
        SELECT c.admin_id FROM instance_connections c
         WHERE p_kind = 'mailbox_poll'
           AND c.connection_type = 'email_sender'
           AND c.provider = 'outlook'
           AND c.status = 'connected'
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
    _replace(_COMMON + _MAILBOX_POLL)


def downgrade() -> None:
    _replace(_COMMON)
