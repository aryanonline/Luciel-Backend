"""mailbox_poll candidates: a draining BYO mailbox is still polled (round 7 WP-5)

The Luciel delete writes ``status = 'quiescing'`` on every connection (migration 0068)
so the in-flight conversation on each channel is still answered during the 30-day
grace window. ``poll_mailbox`` was taught to read a quiescing Outlook row judged by its
stashed status — but the poll is only ever reached through
``platform_sweep_candidates('mailbox_poll')``, whose branch (0038, restated verbatim
through 0065) admitted ``status = 'connected'`` alone. The tenant dropped out of the
candidate list the moment the delete landed, the mailbox was never read again during
grace, and the customer mid-thread at the delete instant never got their follow-up
seen. The drain was reachable on SMS, Meta and legacy SES email and unreachable on
the only email mode still offered.

The mailbox_poll branch now admits ``status = 'connected' OR (status = 'quiescing'
AND status_stashed = 'connected')`` — the same judgement
``app.lifecycle.state_machine.serving_status`` makes in Python — so 0038's intent is
kept exactly: the sweep never wakes for a mailbox that could not be polled before the
delete either (an expired or errored row stashed as such stays out of the list), and
a platform with no mailbox tenants still pays one index scan per run.

Everything else is 0065's body byte for byte; function replacement is wholesale with
OWNER + GRANT restated (0018/0034/0043/0048/0056/0062/0065 pattern). No tenant or
audit-table change — the usual post-upgrade audit-lock step is unaffected. Downgrade
restores the 0065 body.

One bookkeeping change rides along: this revision id is the first in the chain longer
than the 32 characters alembic's own ``alembic_version.version_num`` column holds by
default, and alembic stamps the id AFTER ``upgrade()`` returns, so the column is
widened to 64 here first (alembic's documented remedy; there is no configuration
knob). Idempotent, and any later long id needs nothing. It is deliberately NOT
narrowed on downgrade: the row still holds this id when ``downgrade()`` runs, and a
32-character column could not take it.

Revision ID: 0071_mailbox_poll_drains_quiescing
Revises: 0070_knowledge_answer_flags
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0071_mailbox_poll_drains_quiescing"
down_revision: str | None = "0070_knowledge_answer_flags"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None

_CANDIDATES_HEADER = """
    CREATE OR REPLACE FUNCTION platform_sweep_candidates(p_kind text)
    RETURNS TABLE(admin_id uuid)
    LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
"""

_CANDIDATES_FOOTER = "    $$;"

# 0056's body verbatim (as 0065 restated it), split around the one branch this
# revision changes so the rest is provably untouched.
_BODY_0056_BEFORE_MAILBOX = """
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
"""

# 0038's branch as 0056/0065 carried it: a CONNECTED outlook row only.
_MAILBOX_POLL_BRANCH_0065 = """
        SELECT c.admin_id FROM instance_connections c
         WHERE p_kind = 'mailbox_poll'
           AND c.connection_type = 'email_sender'
           AND c.provider = 'outlook'
           AND c.status = 'connected'
"""

# This revision: a draining row is judged by the status the delete stashed, exactly
# as state_machine.serving_status judges it in Python.
_MAILBOX_POLL_BRANCH_0071 = """
        SELECT c.admin_id FROM instance_connections c
         WHERE p_kind = 'mailbox_poll'
           AND c.connection_type = 'email_sender'
           AND c.provider = 'outlook'
           AND (c.status = 'connected'
                OR (c.status = 'quiescing' AND c.status_stashed = 'connected'))
"""

_BODY_0056_AFTER_MAILBOX = """
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


_KNOWLEDGE_CHUNKS_BRANCH = """
        UNION
        SELECT DISTINCT k.admin_id FROM knowledge_chunks k
         WHERE p_kind = 'knowledge_sync'
"""


def _body(mailbox_poll_branch: str) -> str:
    return (
        _BODY_0056_BEFORE_MAILBOX
        + mailbox_poll_branch.strip("\n")
        + _BODY_0056_AFTER_MAILBOX
        + _DAILY_BRIEF_BRANCH
        + _KNOWLEDGE_CHUNKS_BRANCH
    )


#: The whole function body at this revision / at 0065 — exported for the round-trip
#: test, which compares ``pg_proc.prosrc`` against each end of the move.
BODY_0071 = _body(_MAILBOX_POLL_BRANCH_0071)
BODY_0065 = _body(_MAILBOX_POLL_BRANCH_0065)


def _replace_candidates(body: str) -> None:
    op.execute(_CANDIDATES_HEADER + body + _CANDIDATES_FOOTER)
    op.execute("ALTER FUNCTION platform_sweep_candidates(text) OWNER TO luciel_auth")
    op.execute("GRANT EXECUTE ON FUNCTION platform_sweep_candidates(text) TO luciel_app")


def upgrade() -> None:
    # See the module docstring: alembic stamps this 34-character id after upgrade()
    # returns, into a column that is VARCHAR(32) by default.
    op.execute("ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(64)")
    _replace_candidates(BODY_0071)


def downgrade() -> None:
    _replace_candidates(BODY_0065)
