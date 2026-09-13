"""embedding verification: a stored vector must match its own text

Round 6 follow-up (2026-09-13 dev walk, F121b). ``knowledge_chunks.embedding_verified_at``
records when a chunk's vector was last checked against a fresh embedding of its text;
the knowledge_sync sweep verifies a bounded batch per tenant per pass and re-embeds any
vector that disagrees (a hash vector stamped with the live model's name by migration
0058 sat at cosine distance ~1.0 from every query and was dropped by the relevance
floor on every turn). The sweep's candidate list widens to any tenant with chunks, so a
tenant with only pasted or uploaded knowledge is visited too.

Revision ID: 0065_embedding_verification
Revises: 0064_escalation_reasons
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0065_embedding_verification"
down_revision: str | None = "0064_escalation_reasons"
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


_KNOWLEDGE_CHUNKS_BRANCH = """
        UNION
        SELECT DISTINCT k.admin_id FROM knowledge_chunks k
         WHERE p_kind = 'knowledge_sync'
"""


def _replace_candidates(body: str) -> None:
    op.execute(_CANDIDATES_HEADER + body + _CANDIDATES_FOOTER)
    op.execute("ALTER FUNCTION platform_sweep_candidates(text) OWNER TO luciel_auth")
    op.execute("GRANT EXECUTE ON FUNCTION platform_sweep_candidates(text) TO luciel_app")


def upgrade() -> None:
    op.add_column(
        "knowledge_chunks",
        sa.Column("embedding_verified_at", sa.DateTime(timezone=True), nullable=True),
    )
    # The function runs as luciel_auth (BYPASSRLS); grant it SELECT on what it now reads
    # (same pattern as 0006 / 0018 for the other branch tables).
    op.execute("GRANT SELECT ON knowledge_chunks TO luciel_auth")
    _replace_candidates(_BODY_0056 + _DAILY_BRIEF_BRANCH + _KNOWLEDGE_CHUNKS_BRANCH)


def downgrade() -> None:
    _replace_candidates(_BODY_0056 + _DAILY_BRIEF_BRANCH)
    op.execute("REVOKE SELECT ON knowledge_chunks FROM luciel_auth")
    op.drop_column("knowledge_chunks", "embedding_verified_at")
