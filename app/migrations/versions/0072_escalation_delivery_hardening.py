"""escalation delivery hardening: the sweep sees the rows the queue cannot

Round 7 Phase 2 review (dimension ``delivery``). Two kinds of escalation row were
invisible to everything that promises the owner hears about a hot lead:

  * ``pending`` — the off-turn notification task died (a deploy's shutdown cancelled it,
    the process was killed, the transaction aborted) before anything was paged or
    queued. Nothing read ``delivery_status = 'pending'``: not the deferred sweep (its
    candidates are tenants with queued ``deferred_actions``), not the backlog, not the
    alarm. The hot lead was dropped silently.
  * ``sent`` — the provider accepted the page and the carrier's delivery receipt never
    came (lost, blocked, or posted before the sending transaction committed). The row
    read ``sent`` forever and a carrier failure never reached the owner.

Both become the deferred sweep's business:

  * ``platform_sweep_candidates('deferred')`` also returns tenants with a ``pending``
    row older than ten minutes or a ``sent`` row (with a provider message id) older
    than fifteen; the sweep retries the first through the normal path and asks the
    provider for the second's status (``escalation_dispatch.sweep_stale_escalations``).
    The ``sent`` window runs from the new ``escalation_events.sent_at`` (the moment the
    provider accepted the page; ``fired_at`` for rows stamped before this revision) —
    a page the sweep itself replays an hour after the escalation is seconds old.
  * ``platform_escalation_backlog()`` counts the stale ``pending`` rows in ``queued``
    and lets ``oldest_queued_at`` reach back to the oldest of them, so ``/healthz`` and
    the EscalationBacklog alarm see a lost notification exactly like a queued retry.

The bounds are copies of ``escalation_dispatch.PENDING_STALE_AFTER`` /
``SENT_UNCONFIRMED_AFTER``; tests/integration/test_migration_0072_escalation_delivery_hardening.py
refuses a drift. Same SECURITY DEFINER posture as 0018 / 0057 / 0069; ``luciel_auth``
has held SELECT on ``escalation_events`` since 0057. Both functions are restated
verbatim on downgrade (0065's candidate body, 0069's backlog body).

This revision id is 34 characters and alembic creates its own ``alembic_version``
table as ``varchar(32)`` (0040 sits exactly at the ceiling): the column is widened
to 64 here, in the same transaction, before alembic stamps the id. Never narrowed
back — a downgrade stamps a shorter id into the wider column.

Revision ID: 0072_escalation_delivery_hardening
Revises: 0070_knowledge_answer_flags
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0072_escalation_delivery_hardening"
down_revision: str | None = "0070_knowledge_answer_flags"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None

APP_ROLE = "luciel_app"

# --- platform_sweep_candidates ------------------------------------------------------ #

_CANDIDATES_HEADER = """
    CREATE OR REPLACE FUNCTION platform_sweep_candidates(p_kind text)
    RETURNS TABLE(admin_id uuid)
    LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
"""

_CANDIDATES_FOOTER = "    $$;"

# 0065's body verbatim (0056's branches + daily_brief + knowledge_chunks).
_BODY_0065 = """
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
        UNION
        SELECT i.admin_id FROM instances i
         WHERE p_kind = 'daily_brief'
           AND i.state IN ('active', 'paused')
           AND i.daily_brief_enabled
        UNION
        SELECT DISTINCT k.admin_id FROM knowledge_chunks k
         WHERE p_kind = 'knowledge_sync'
"""

# The two bounds, as SQL intervals: copies of escalation_dispatch.PENDING_STALE_AFTER
# and SENT_UNCONFIRMED_AFTER (the migration test pins them to the Python constants).
PENDING_STALE_INTERVAL = "10 minutes"
SENT_UNCONFIRMED_INTERVAL = "15 minutes"

_STALE_ESCALATIONS_BRANCH = f"""
        UNION
        SELECT DISTINCT e.admin_id FROM escalation_events e
         WHERE p_kind = 'deferred'
           AND (
                (e.delivery_status = 'pending'
                 AND e.fired_at < now() - interval '{PENDING_STALE_INTERVAL}')
             OR (e.delivery_status = 'sent'
                 AND e.delivery_provider_message_id IS NOT NULL
                 AND COALESCE(e.sent_at, e.fired_at)
                     < now() - interval '{SENT_UNCONFIRMED_INTERVAL}')
           )
"""  # noqa: S608 — the intervals are the two module constants above, never input


def _replace_candidates(body: str) -> None:
    op.execute(_CANDIDATES_HEADER + body + _CANDIDATES_FOOTER)
    op.execute("ALTER FUNCTION platform_sweep_candidates(text) OWNER TO luciel_auth")
    op.execute(f"GRANT EXECUTE ON FUNCTION platform_sweep_candidates(text) TO {APP_ROLE}")


# --- platform_escalation_backlog ---------------------------------------------------- #

# 0069's function verbatim, restored on downgrade.
_BACKLOG_0069 = """
    CREATE OR REPLACE FUNCTION platform_escalation_backlog()
    RETURNS TABLE(queued bigint, failed_24h bigint, oldest_queued_at timestamptz)
    LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
        SELECT
            (SELECT count(*) FROM deferred_actions d
              WHERE d.action_type = 'escalation_notification' AND d.status = 'queued'),
            (SELECT count(*) FROM escalation_events e
              WHERE e.delivery_status = 'failed'
                AND e.fired_at > now() - interval '24 hours'),
            (SELECT min(d.created_at) FROM deferred_actions d
              WHERE d.action_type = 'escalation_notification' AND d.status = 'queued')
    $$;
"""

# ``queued`` = retries the queue holds + notifications whose task was lost (a pending
# row past the bound); ``oldest_queued_at`` = the oldest of either. LEAST ignores a NULL
# side, so one empty set does not hide the other.
_BACKLOG_0072 = f"""
    CREATE OR REPLACE FUNCTION platform_escalation_backlog()
    RETURNS TABLE(queued bigint, failed_24h bigint, oldest_queued_at timestamptz)
    LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
        SELECT
            (SELECT count(*) FROM deferred_actions d
              WHERE d.action_type = 'escalation_notification' AND d.status = 'queued')
            + (SELECT count(*) FROM escalation_events e
                WHERE e.delivery_status = 'pending'
                  AND e.fired_at < now() - interval '{PENDING_STALE_INTERVAL}'),
            (SELECT count(*) FROM escalation_events e
              WHERE e.delivery_status = 'failed'
                AND e.fired_at > now() - interval '24 hours'),
            LEAST(
                (SELECT min(d.created_at) FROM deferred_actions d
                  WHERE d.action_type = 'escalation_notification' AND d.status = 'queued'),
                (SELECT min(e.fired_at) FROM escalation_events e
                  WHERE e.delivery_status = 'pending'
                    AND e.fired_at < now() - interval '{PENDING_STALE_INTERVAL}')
            )
    $$;
"""  # noqa: S608 — the interval is the module constant above, never input


def _install_backlog_function(body: str) -> None:
    op.execute("DROP FUNCTION IF EXISTS platform_escalation_backlog()")
    op.execute(body)
    op.execute("ALTER FUNCTION platform_escalation_backlog() OWNER TO luciel_auth")
    op.execute(f"GRANT EXECUTE ON FUNCTION platform_escalation_backlog() TO {APP_ROLE}")


def upgrade() -> None:
    # Room for this revision's 34-character id (see the module docstring); DDL is
    # transactional here, so alembic's own version stamp follows in the same commit.
    op.execute("ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(64)")
    # When the provider accepted the page a ``sent`` row rests on: the receipt window
    # runs from the send, not from ``fired_at`` (a replay an hour later is seconds old).
    op.add_column("escalation_events", sa.Column("sent_at", sa.DateTime(timezone=True)))
    _replace_candidates(_BODY_0065 + _STALE_ESCALATIONS_BRANCH)
    _install_backlog_function(_BACKLOG_0072)


def downgrade() -> None:
    _install_backlog_function(_BACKLOG_0069)
    _replace_candidates(_BODY_0065)
    op.drop_column("escalation_events", "sent_at")
