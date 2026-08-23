"""paused widget resolves gracefully + grace-window tenants keep their sweeps (C7)

Two honesty fixes in the channel/sweep discovery layer:

1. ``channel_resolve_embed_key`` matched ``state IN ('active')`` only, so a PAUSED
   Luciel's widget got HTTP 404 — the documented §3.6.2 graceful paused render
   (empty <div>, no error) was dead code, and a visitor mid-conversation at the
   pause instant saw a send failure instead of the promised quiet finish. The
   resolver now admits ``paused`` AND ``luciel_grace_window``: the routes serve
   the honest ``render_state="paused"`` directive to new visitors, and an
   in-flight session opened BEFORE the pause/delete is allowed to finish (the
   §3.6.2 carve-out / §3.6.3 fused-quiesce drain — new intake stops, a live lead
   is never dropped mid-sentence). Hard-deleted stays unresolvable (the instance
   row is gone).

2. ``platform_sweep_candidates`` excluded ``luciel_grace_window`` tenants from
   ``session_finalize`` and ``transcript_archive``, so sessions open at the
   delete instant were never sealed or summarized (the fused-quiesce drain's
   backstop, §3.6.3 step 0) and their transcripts never offloaded during grace.
   Both branches now include grace-window tenants; the per-tenant passes remain
   the arbiters of what is actually due (0026 superset discipline).

Function replacements are wholesale with OWNER + GRANT restated (0018/0034/0043
pattern) — a SECURITY DEFINER replace that dropped the ACL would break every
tenant's widget at once.

Revision ID: 0048_widget_pause_grace
Revises: 0047_trace_store
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0048_widget_pause_grace"
down_revision: str | None = "0047_trace_store"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None

_RESOLVER_HEADER = """
    CREATE OR REPLACE FUNCTION channel_resolve_embed_key(p_key text)
    RETURNS TABLE(admin_id uuid, instance_id uuid)
    LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
        SELECT admin_id, id FROM instances
        WHERE embed_key_public_id = p_key
"""

_RESOLVER_ACTIVE_ONLY = "          AND state IN ('active')\n"
_RESOLVER_WITH_PAUSED = "          AND state IN ('active', 'paused', 'luciel_grace_window')\n"

_RESOLVER_FOOTER = """        LIMIT 1
    $$;
"""


def _replace_resolver(states_line: str) -> None:
    op.execute(_RESOLVER_HEADER + states_line + _RESOLVER_FOOTER)
    op.execute("ALTER FUNCTION channel_resolve_embed_key(text) OWNER TO luciel_auth")
    op.execute("GRANT EXECUTE ON FUNCTION channel_resolve_embed_key(text) TO luciel_app")


def _candidates_body(*, grace_in_finalize: bool) -> str:
    finalize_states = (
        "('active', 'paused', 'luciel_grace_window')"
        if grace_in_finalize
        else "('active', 'paused')"
    )
    # Placeholder substitution, not string formatting: the only injected value is a
    # literal chosen above from two hardcoded constants — no runtime input reaches
    # this SQL (same trust as the fully-static bodies in 0018/0034/0043).
    return _CANDIDATES_TEMPLATE.replace("__FINALIZE_STATES__", finalize_states)


_CANDIDATES_TEMPLATE = """
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
         WHERE p_kind = 'session_finalize' AND i.state IN __FINALIZE_STATES__
        UNION
        SELECT i.admin_id FROM instances i
         WHERE p_kind = 'transcript_archive' AND i.state IN __FINALIZE_STATES__
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


_CANDIDATES_HEADER = """
    CREATE OR REPLACE FUNCTION platform_sweep_candidates(p_kind text)
    RETURNS TABLE(admin_id uuid)
    LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
"""

_CANDIDATES_FOOTER = "    $$;"


def _replace_candidates(body: str) -> None:
    op.execute(_CANDIDATES_HEADER + body + _CANDIDATES_FOOTER)
    op.execute("ALTER FUNCTION platform_sweep_candidates(text) OWNER TO luciel_auth")
    op.execute("GRANT EXECUTE ON FUNCTION platform_sweep_candidates(text) TO luciel_app")


def upgrade() -> None:
    _replace_resolver(_RESOLVER_WITH_PAUSED)
    _replace_candidates(_candidates_body(grace_in_finalize=True))


def downgrade() -> None:
    _replace_resolver(_RESOLVER_ACTIVE_ONLY)
    _replace_candidates(_candidates_body(grace_in_finalize=False))
