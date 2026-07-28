"""platform sweep candidate discovery (scheduler prerequisite, Legal §B5)

The background sweeps (retention prune, grace-window purge, close-account purge,
dunning advance) each need to know WHICH tenants to visit before any tenant GUC is
set. That discovery is inherently cross-tenant, and the app connects as the
NON-BYPASSRLS ``luciel_app`` role, so a plain ``SELECT admin_id FROM instances``
with no ``app.admin_id`` returns zero rows.

Mirrors the established pattern (``auth_lookup_account``, ``channel_resolve_*``): a
narrow SECURITY DEFINER function owned by the BYPASSRLS ``luciel_auth`` role that
returns ONLY tenant ids — never tenant data. Each sweep then does its real work
inside a per-tenant RLS-scoped transaction.

An unknown ``p_kind`` returns zero rows, so a typo'd schedule sweeps nothing rather
than everything.

Revision ID: 0018_platform_sweep_candidates
Revises: 0017_lead_capture_fields
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0018_platform_sweep_candidates"
down_revision: str | None = "0017_lead_capture_fields"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
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
    op.execute("GRANT EXECUTE ON FUNCTION platform_sweep_candidates(text) TO luciel_app")
    # The function runs as luciel_auth (BYPASSRLS); grant it SELECT on what it reads.
    # instances is already granted by 0006; accounts by 0001.
    op.execute("GRANT SELECT ON billing_accounts TO luciel_auth")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS platform_sweep_candidates(text)")
