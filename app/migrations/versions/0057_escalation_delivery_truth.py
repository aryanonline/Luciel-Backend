"""escalation delivery truth: one row per (session, signal, gate) + a delivery status

Three escalation corrections from the 2026-09-05 product-owner audit:

  * ``uq_escalation_once`` — an escalation fires at most ONCE per session per signal
    per gate (F053/F061). The runtime pre-checked for an existing row and then
    inserted, so two turns racing on the same session paged the owner twice; the
    unique index makes the insert itself the arbiter (``ON CONFLICT DO NOTHING``).
    Existing duplicates collapse to the earliest fired row — the one the owner was
    actually paged for.

  * ``delivery_status`` / ``delivery_detail`` / ``delivered_at`` — what happened to the
    notification for that row (F079/F083): ``delivered`` (a configured contact
    heard), ``owner_fallback`` (only the account owner heard; the contact is still
    being retried), ``queued`` (nobody yet; the deferred queue owns it), ``failed``
    (terminal), ``pending`` (just written). Rows from before this migration are
    ``unknown`` — the truth was never recorded for them, and saying "delivered" would
    be a fabrication.

  * ``platform_escalation_backlog()`` — the platform-wide count of queued escalation
    retries and terminal misses in the last 24 h, for ``/healthz`` (F079). SECURITY
    DEFINER owned by the BYPASSRLS auth role, the 0048/0056 ``platform_sweep_candidates``
    pattern: the app role cannot read across tenants, and must not be able to.

Revision ID: 0057_escalation_delivery_truth
Revises: 0056_knowledge_delete_grace
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0057_escalation_delivery_truth"
down_revision: str | None = "0056_knowledge_delete_grace"
branch_labels: str | None = None
depends_on: str | None = None

_BACKLOG_FUNCTION = """
    CREATE OR REPLACE FUNCTION platform_escalation_backlog()
    RETURNS TABLE(queued bigint, failed_24h bigint)
    LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
        SELECT
            (SELECT count(*) FROM deferred_actions d
              WHERE d.action_type = 'escalation_notification' AND d.status = 'queued'),
            (SELECT count(*) FROM escalation_events e
              WHERE e.delivery_status = 'failed'
                AND e.fired_at > now() - interval '24 hours')
    $$;
"""


def upgrade() -> None:
    # Collapse duplicates to the earliest fired row per (tenant, session, signal, gate):
    # that is the page the owner actually received; the later rows are the double
    # pages. Server-only rows without a session are untouched (nothing to key on).
    op.execute(
        """
        DELETE FROM escalation_events a
        USING escalation_events b
        WHERE a.admin_id = b.admin_id
          AND a.session_id IS NOT NULL
          AND a.session_id = b.session_id
          AND a.signal = b.signal
          AND a.gate = b.gate
          AND (
            a.fired_at > b.fired_at
            OR (a.fired_at = b.fired_at AND a.escalation_id > b.escalation_id)
          )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_escalation_once ON escalation_events "
        "(admin_id, session_id, signal, gate) WHERE session_id IS NOT NULL"
    )
    op.add_column(
        "escalation_events",
        sa.Column("delivery_status", sa.String(16), nullable=False, server_default="unknown"),
    )
    op.add_column("escalation_events", sa.Column("delivery_detail", sa.String(64), nullable=True))
    op.add_column(
        "escalation_events",
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
    )
    # The definer role reads across tenants only through this function; it needs a
    # plain SELECT on the table (0028 pattern for deferred_actions).
    op.execute("GRANT SELECT ON escalation_events TO luciel_auth")
    op.execute(_BACKLOG_FUNCTION)
    op.execute("ALTER FUNCTION platform_escalation_backlog() OWNER TO luciel_auth")
    op.execute("GRANT EXECUTE ON FUNCTION platform_escalation_backlog() TO luciel_app")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS platform_escalation_backlog()")
    op.execute("REVOKE SELECT ON escalation_events FROM luciel_auth")
    op.drop_column("escalation_events", "delivered_at")
    op.drop_column("escalation_events", "delivery_detail")
    op.drop_column("escalation_events", "delivery_status")
    op.execute("DROP INDEX IF EXISTS uq_escalation_once")
