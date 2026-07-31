"""audit-retention sweep candidates: give the 1-year audit commitment a caller (P1-15)

§B5 publishes a 1-year retention on the audit log, and nothing enforced it — the log
was append-only with no ceiling, so the commitment was one a tenant could disprove by
waiting. Ruling 6 keeps the commitment and specifies the shape: a narrow SECURITY
DEFINER procedure owned by ``luciel_audit_owner``, exempted by the append-only trigger,
deleting only rows past 365 days.

The PROCEDURE lives in ``scripts/lock_audit_log.sql``, not here, and cannot move: it
must be OWNED by ``luciel_audit_owner`` for the trigger's identity check to exempt it,
and alembic runs as ``luciel_app``, which is deliberately not a member of that role
(the same reason migration 0019 could not transfer table ownership). This migration
adds the half a migration legitimately owns — cross-tenant discovery.

Discovery is every account rather than "accounts with an aged row": reading
``admin_audit_log`` from here would need ``GRANT SELECT ... TO luciel_auth`` on a table
this role cannot grant on after the lock has run, and the whole function body is planned
as one unit, so that one branch would fail EVERY sweep kind. It is also the discipline
0026 already follows — discovery returns a superset, the per-tenant pass re-derives what
is actually due — and the per-tenant pass here is a single indexed DELETE on
``ix_audit_admin_created``.

Revision ID: 0029_audit_retention_candidates
Revises: 0028_deferred_health_candidates
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0029_audit_retention_candidates"
down_revision: str | None = "0028_deferred_health_candidates"
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

_EXTRA = """
        UNION
        SELECT a.admin_id FROM accounts a
         WHERE p_kind = 'audit_retention'
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
    _replace(_COMMON + _EXTRA)


def downgrade() -> None:
    _replace(_COMMON)
