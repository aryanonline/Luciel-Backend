"""daily_brief sweep candidates: every active/paused Luciel whose owner wants the brief

Round 6 WP-F (2026-09-07): the hourly ``daily_brief`` sweep visits each tenant with
an active or paused Luciel and ``instances.daily_brief_enabled`` on (migration 0061),
and sends the morning brief once per LOCAL day — the ``notice_deliveries`` ledger,
not the schedule, makes "once" durable. Function replacement is wholesale with
OWNER + GRANT restated (0018/0034/0043/0048/0056 pattern); the body is 0056's with
one extra UNION branch.

Revision ID: 0062_daily_brief_candidates
Revises: 0061_team_availability
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0062_daily_brief_candidates"
down_revision: str | None = "0061_team_availability"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None

_CANDIDATES_HEADER = """
    CREATE OR REPLACE FUNCTION platform_sweep_candidates(p_kind text)
    RETURNS TABLE(admin_id uuid)
    LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
"""

_CANDIDATES_FOOTER = "    $$;"

# 0056's body verbatim (audit_retention reaches purged tenants).
_BODY_0056 = """
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
        SELECT DISTINCT l.admin_id FROM admin_audit_log l
         WHERE p_kind = 'audit_retention'
           AND l.created_at < now() - interval '365 days'
        UNION
        SELECT i.admin_id FROM instances i
         WHERE p_kind = 'session_finalize'
           AND i.state IN ('active', 'paused', 'luciel_grace_window')
        UNION
        SELECT i.admin_id FROM instances i
         WHERE p_kind = 'transcript_archive'
           AND i.state IN ('active', 'paused', 'luciel_grace_window')
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
        UNION
        SELECT a.admin_id FROM accounts a
         WHERE p_kind = 'billing_notices' AND a.state = 'verified'
"""

_DAILY_BRIEF_BRANCH = """
        UNION
        SELECT i.admin_id FROM instances i
         WHERE p_kind = 'daily_brief'
           AND i.state IN ('active', 'paused')
           AND i.daily_brief_enabled
"""


def _replace_candidates(body: str) -> None:
    op.execute(_CANDIDATES_HEADER + body + _CANDIDATES_FOOTER)
    op.execute("ALTER FUNCTION platform_sweep_candidates(text) OWNER TO luciel_auth")
    op.execute("GRANT EXECUTE ON FUNCTION platform_sweep_candidates(text) TO luciel_app")


def upgrade() -> None:
    _replace_candidates(_BODY_0056 + _DAILY_BRIEF_BRANCH)


def downgrade() -> None:
    _replace_candidates(_BODY_0056)
