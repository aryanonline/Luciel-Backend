"""knowledge_sync candidates + contact-form delete grant (§3.2.3 / data growth)

Two enablers for the data-currency/data-growth sweeps:

  * ``platform_sweep_candidates`` gains a ``knowledge_sync`` branch: tenants
    with a CONNECTED knowledge_source connection. The §3.2.3 "kept in sync"
    promise finally gets its periodic half — before this the sync engine was
    reachable only from the manual dashboard routes.
  * ``contact_submission`` becomes deletable by the app role, so the retention
    enforcer has a path to prune the anonymous-PII table that previously had
    no deletion path at all.

(The internal_access_log 3-year-ceiling prune is the lock script's to own —
see scripts/lock_audit_log.sql `internal_access_prune`.)

Follows the 0018/0034/0038 pattern: replace the candidates function wholesale,
restate owner + grant.

Revision ID: 0042_knowledge_sync_prunes
Revises: 0041_booking_events
"""

from __future__ import annotations

from alembic import op

revision: str = "0042_knowledge_sync_prunes"
down_revision: str | None = "0041_booking_events"
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
        UNION
        SELECT c.admin_id FROM instance_connections c
         WHERE p_kind = 'mailbox_poll'
           AND c.connection_type = 'email_sender'
           AND c.provider = 'outlook'
           AND c.status = 'connected'
"""

_KNOWLEDGE_SYNC = """
        UNION
        SELECT c.admin_id FROM instance_connections c
         WHERE p_kind = 'knowledge_sync'
           AND c.connection_type = 'knowledge_source'
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
    _replace(_COMMON + _KNOWLEDGE_SYNC)
    # NOTE: the internal_access_log 3-year-CEILING prune lives in
    # scripts/lock_audit_log.sql (the superuser post-migrate step), NOT here —
    # the lock script owns that table (luciel_audit_owner), its append-only
    # guard, and the only authorized DELETE exemptions; a migration running as
    # the app role can neither replace that guard nor grant past it. See
    # `internal_access_prune` there, mirroring `audit_retention_prune`.
    #
    # contact_submission was INSERT/SELECT-only for the app role (0027) — sound
    # against accidental mutation, but it also made the anonymous-PII table
    # undeletable by ANY code path: no retention, no cascade, no sweep. The
    # retention enforcer (app/lifecycle/retention.expire_platform_data) is that
    # deletion path now; UPDATE stays revoked (submissions are immutable facts;
    # the notify flag rides its own SECURITY DEFINER path if ever needed).
    op.execute("GRANT DELETE ON contact_submission TO luciel_app;")


def downgrade() -> None:
    op.execute("REVOKE DELETE ON contact_submission FROM luciel_app;")
    _replace(_COMMON)
