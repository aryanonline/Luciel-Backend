"""knowledge delete grace + audit retention reaches purged tenants

Two data-lifecycle corrections from the 2026-09-05 product-owner audit:

  * ``knowledge_sources.deleted_by`` — who tombstoned a source. ``owner`` deletes are
    undo-able for 30 days and then purged by the retention pass; ``lifecycle``
    tombstones belong to the Luciel delete and come back with the Luciel. Restore
    used to revive EVERY tombstone (F024) while an owner delete hard-removed the
    chunks in the same call (F109), so the advertised undo window was fiction.
    Existing tombstones are backfilled: sources under a Luciel in its grace window
    are ``lifecycle``; the rest are ``owner`` (their chunks are already gone; the
    purge will take the tombstone row at the next pass).

  * ``knowledge_sources.original_key`` / ``original_content_type`` — where the
    customer's uploaded file is kept (the originals store) so the account export can
    contain "your original uploaded files" as §5.10 and the bundle README promise
    (F016). Existing sources have no original on file; the export says so per row.

  * ``knowledge_sources.original_key`` / ``original_content_type`` — where the
    customer's uploaded file is kept (the originals store) so the account export can
    contain "your original uploaded files" as §5.10 and the bundle README promise
    (F016). Existing sources have no original on file; the export says so per row.

  * ``platform_sweep_candidates('audit_retention')`` now also yields tenants that
    only exist in ``admin_audit_log`` any more (F023): a purged account's rows were
    never candidates again once its ``accounts`` row went, so the one-year audit
    retention was unenforceable for exactly the tenants that churned.

Function replacement is wholesale with OWNER + GRANT restated (0018/0034/0043/0048
pattern); the body is 0048's with the one extra UNION branch.

Revision ID: 0056_knowledge_delete_grace
Revises: 0055_billing_period_truth
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0056_knowledge_delete_grace"
down_revision: str | None = "0055_billing_period_truth"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None

_CANDIDATES_HEADER = """
    CREATE OR REPLACE FUNCTION platform_sweep_candidates(p_kind text)
    RETURNS TABLE(admin_id uuid)
    LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
"""

_CANDIDATES_FOOTER = "    $$;"

_AUDIT_RETENTION_ACCOUNTS = """
        SELECT a.admin_id FROM accounts a
         WHERE p_kind = 'audit_retention'
           AND a.created_at < now() - interval '365 days'
"""

_AUDIT_RETENTION_PURGED = """
        SELECT DISTINCT l.admin_id FROM admin_audit_log l
         WHERE p_kind = 'audit_retention'
           AND l.created_at < now() - interval '365 days'
"""

_BODY_0048 = """
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
__AUDIT_RETENTION__
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


def _body(*, purged_tenants: bool) -> str:
    branch = _AUDIT_RETENTION_ACCOUNTS
    if purged_tenants:
        branch = _AUDIT_RETENTION_ACCOUNTS + "        UNION\n" + _AUDIT_RETENTION_PURGED
    # Placeholder substitution of a hardcoded literal — no runtime input reaches this SQL.
    return _BODY_0048.replace("__AUDIT_RETENTION__", branch)


def _replace_candidates(body: str) -> None:
    op.execute(_CANDIDATES_HEADER + body + _CANDIDATES_FOOTER)
    op.execute("ALTER FUNCTION platform_sweep_candidates(text) OWNER TO luciel_auth")
    op.execute("GRANT EXECUTE ON FUNCTION platform_sweep_candidates(text) TO luciel_app")


def upgrade() -> None:
    op.add_column("knowledge_sources", sa.Column("deleted_by", sa.String(16), nullable=True))
    op.add_column("knowledge_sources", sa.Column("original_key", sa.String(1024), nullable=True))
    op.add_column(
        "knowledge_sources", sa.Column("original_content_type", sa.String(128), nullable=True)
    )
    op.execute(
        """
        UPDATE knowledge_sources ks
           SET deleted_by = CASE
                 WHEN EXISTS (SELECT 1 FROM instances i
                               WHERE i.id = ks.instance_id
                                 AND i.state = 'luciel_grace_window')
                 THEN 'lifecycle' ELSE 'owner' END
         WHERE ks.soft_deleted_at IS NOT NULL AND ks.deleted_by IS NULL
        """
    )
    _replace_candidates(_body(purged_tenants=True))


def downgrade() -> None:
    _replace_candidates(_body(purged_tenants=False))
    op.drop_column("knowledge_sources", "original_content_type")
    op.drop_column("knowledge_sources", "original_key")
    op.drop_column("knowledge_sources", "deleted_by")
