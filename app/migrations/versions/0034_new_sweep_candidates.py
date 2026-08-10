"""sweep candidates: session_finalize + transcript_archive branches; narrower audit scan

Three discovery changes in one function replacement (the whole body is one planned
unit, so the function is always replaced wholesale — 0018/0026/0028/0029 pattern):

1. ``session_finalize`` (§3.4.7/§3.4.8) — every active/paused instance is a candidate;
   the per-tenant pass (app/runtime/session_close.py) re-derives which sessions are
   actually past their channel window. Superset discovery, same 0026 discipline.
2. ``transcript_archive`` (Decision #38, hot→cold at 90 days) — same superset shape;
   the per-tenant pass selects only ended sessions past the 90-day threshold. The
   branch lands here (one migration serves both sweeps) so the wiring commits stay
   lockstep with their tests.
3. ``audit_retention`` narrowed with a GRANT-FREE age predicate: an account younger
   than a year cannot own an audit row older than a year, so
   ``a.created_at < now() - interval '365 days'`` shrinks the weekly scan from
   O(all tenants) to O(tenants old enough to have aged rows) — without the
   ``admin_audit_log`` SELECT grant that 0029 documented as impossible post-lock.

Revision ID: 0034_new_sweep_candidates
Revises: 0033_session_participant_key
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0034_new_sweep_candidates"
down_revision: str | None = "0033_session_participant_key"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None

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
"""

_AUDIT_BROAD = """
        UNION
        SELECT a.admin_id FROM accounts a
         WHERE p_kind = 'audit_retention'
"""

_AUDIT_AGED = """
        UNION
        SELECT a.admin_id FROM accounts a
         WHERE p_kind = 'audit_retention'
           AND a.created_at < now() - interval '365 days'
"""

_NEW_SWEEPS = """
        UNION
        SELECT i.admin_id FROM instances i
         WHERE p_kind = 'session_finalize' AND i.state IN ('active', 'paused')
        UNION
        SELECT i.admin_id FROM instances i
         WHERE p_kind = 'transcript_archive' AND i.state IN ('active', 'paused')
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
    _replace(_COMMON + _AUDIT_AGED + _NEW_SWEEPS)


def downgrade() -> None:
    _replace(_COMMON + _AUDIT_BROAD)
