"""unverified-reap sweep candidates: give the 7-day reaper a caller (§3.7.1a, P0-8)

``reap_unverified_accounts`` has existed since the auth work and nothing has ever
called it. §3.7.1a and Legal §A1 both promise the account is "permanently deleted
after 7 days", and that deletion is also what releases the email address for signup —
so the missing scheduler does not merely hoard dead credentials, it permanently blocks
every address that was ever typed into signup and abandoned.

Wiring it into ``app/jobs/sweeps`` needs cross-tenant discovery, which is the one thing
the NON-BYPASSRLS app role cannot do for itself, so ``platform_sweep_candidates`` gains
the kind. The branch filters on state ONLY, not age: the window lives in config
(``unverified_reap_after_seconds``) and a second copy of it here would silently
under-reap the day someone shortened it. Discovery returns a superset; the per-tenant
``reap_account_if_due`` re-derives the cutoff, which is the invariant every other sweep
already holds.

Revision ID: 0026_unverified_reap_candidates
Revises: 0025_usage_watermark
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0026_unverified_reap_candidates"
down_revision: str | None = "0025_usage_watermark"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None

_WITH_UNVERIFIED = """
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
        UNION
        SELECT a.admin_id FROM accounts a
         WHERE p_kind = 'unverified_reap' AND a.state = 'unverified'
    $$;
"""

_WITHOUT_UNVERIFIED = """
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


def _replace(body: str) -> None:
    op.execute(body)
    # Ownership is the whole point of the seam: the function reads across tenants
    # because luciel_auth is BYPASSRLS, and CREATE OR REPLACE would otherwise leave it
    # owned by the migration role.
    op.execute("ALTER FUNCTION platform_sweep_candidates(text) OWNER TO luciel_auth")
    op.execute("GRANT EXECUTE ON FUNCTION platform_sweep_candidates(text) TO luciel_app")


def upgrade() -> None:
    _replace(_WITH_UNVERIFIED)


def downgrade() -> None:
    _replace(_WITHOUT_UNVERIFIED)
