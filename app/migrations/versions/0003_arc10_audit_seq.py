"""arc10 audit chain ordering: monotonic seq column

Within one transaction Postgres ``now()`` is constant, so multiple audit rows
written in the same transaction (e.g. the hard-delete cascade) share an identical
``created_at``. Ordering the hash chain by ``created_at`` is then ambiguous and
breaks chain verification. A BIGSERIAL ``seq`` gives a strict, monotonic,
gap-tolerant order independent of timestamp. The chain is ordered by ``seq``;
the hash still incorporates created_at + event_id + payload.

Expand-only, add-column on admin_audit_log (allowed: never removes, no UPDATE/DELETE
trigger) — second-reviewer required per §5.3 (flagged in PR).

Revision ID: 0003_arc10_audit_seq
Revises: 0002_arc10_lifecycle
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_arc10_audit_seq"
down_revision: str | None = "0002_arc10_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("admin_audit_log", sa.Column("seq", sa.BigInteger, nullable=True))
    op.execute("CREATE SEQUENCE IF NOT EXISTS admin_audit_log_seq")
    op.execute(
        "ALTER TABLE admin_audit_log ALTER COLUMN seq SET DEFAULT nextval('admin_audit_log_seq')"
    )
    # Backfill existing rows deterministically. The app role has INSERT+SELECT only
    # on this table (append-only, §5.3); the backfill UPDATE is a one-time migration
    # maintenance op, so grant UPDATE for the duration of the backfill, then revoke
    # it again so the runtime append-only grant is preserved.
    op.execute("GRANT UPDATE ON admin_audit_log TO luciel_app")
    op.execute("UPDATE admin_audit_log SET seq = nextval('admin_audit_log_seq') WHERE seq IS NULL")
    op.execute("REVOKE UPDATE ON admin_audit_log FROM luciel_app")
    op.create_index("ix_audit_admin_seq", "admin_audit_log", ["admin_id", "seq"])
    # Grant the app role usage on the sequence (INSERT needs nextval).
    op.execute("GRANT USAGE, SELECT ON SEQUENCE admin_audit_log_seq TO luciel_app")


def downgrade() -> None:
    op.drop_index("ix_audit_admin_seq", table_name="admin_audit_log")
    op.execute("ALTER TABLE admin_audit_log ALTER COLUMN seq DROP DEFAULT")
    op.drop_column("admin_audit_log", "seq")
    op.execute("DROP SEQUENCE IF EXISTS admin_audit_log_seq")
