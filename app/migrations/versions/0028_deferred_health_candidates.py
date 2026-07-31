"""deferred + connection-health sweep candidates: give two more sweeps a caller (P1-7/P1-8)

Same defect as 0026, twice over. ``deferred_queue.run_sweep`` and
``health.run_health_sweep`` were both written, both tested, and both unreachable — no
scheduler, no worker, no caller anywhere outside the test suite. The cost is not
theoretical: rule D's promise is that a queued follow-up is retried when the connection
recovers and abandoned WITH an owner notification if it never does, and neither half can
happen if nothing sweeps. Likewise §3.8.5's dormant/expired/error transitions are what
the universal gate reads before it lets a tool run, so an unswept platform gates every
decision on a status last computed the day the admin connected.

Scheduling them needs cross-tenant discovery, which the NON-BYPASSRLS app role cannot do,
so ``platform_sweep_candidates`` gains both kinds.

Both branches filter on the coarse "has anything to look at" predicate only — a queued
row, a non-revoked connection — never on age or due-time. The cutoffs live in the
per-tenant functions (``next_attempt_at``, ``expires_at``, ``REFRESH_LEAD``,
``DORMANT_AFTER``), and a second copy of them here would drift the first time one moved.
Discovery returns a superset; the per-tenant pass re-derives what is actually due, which
is the invariant every other sweep already holds.

Revision ID: 0028_deferred_health_candidates
Revises: 0027_contact_submission
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0028_deferred_health_candidates"
down_revision: str | None = "0027_contact_submission"
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
"""

_EXTRA = """
        UNION
        SELECT d.admin_id FROM deferred_actions d
         WHERE p_kind = 'deferred' AND d.status = 'queued'
        UNION
        SELECT c.admin_id FROM instance_connections c
         WHERE p_kind = 'health' AND c.status <> 'revoked'
"""

_HEADER = """
    CREATE OR REPLACE FUNCTION platform_sweep_candidates(p_kind text)
    RETURNS TABLE(admin_id uuid)
    LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
"""

_FOOTER = "    $$;"


def _replace(body: str) -> None:
    op.execute(_HEADER + body + _FOOTER)
    # Ownership is the whole point of the seam: the function reads across tenants
    # because luciel_auth is BYPASSRLS, and CREATE OR REPLACE would otherwise leave it
    # owned by the migration role.
    op.execute("ALTER FUNCTION platform_sweep_candidates(text) OWNER TO luciel_auth")
    op.execute("GRANT EXECUTE ON FUNCTION platform_sweep_candidates(text) TO luciel_app")


def upgrade() -> None:
    # The function body is planned as a whole, so a missing grant on ANY branch's table
    # fails every kind, not just the new one. instance_connections was already granted
    # by 0006.
    op.execute("GRANT SELECT ON deferred_actions TO luciel_auth")
    _replace(_COMMON + _EXTRA)


def downgrade() -> None:
    _replace(_COMMON)
    op.execute("REVOKE SELECT ON deferred_actions FROM luciel_auth")
