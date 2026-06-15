"""arc12 tools: instance_tool_authorizations table (default-deny) + RLS

The broker's first gate (Architecture §3.3.3): a tool is usable only if a live
authorization row exists for (instance, tool). Default-deny — absence means the
tool cannot dispatch. Tenant-scoped with the three-layer Layer-2 RLS.

Revision ID: 0005_arc12_tool_authorizations
Revises: 0004_arc11_knowledge_vectors
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_arc12_tool_authorizations"
down_revision: str | None = "0004_arc11_knowledge_vectors"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None

APP_ROLE = "luciel_app"


def upgrade() -> None:
    op.create_table(
        "instance_tool_authorizations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("admin_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("instance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tool_id", sa.String(64), nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_tool_auth_admin_instance", "instance_tool_authorizations", ["admin_id", "instance_id"]
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_tool_auth ON instance_tool_authorizations "
        "(admin_id, instance_id, tool_id)"
    )
    predicate = "admin_id = NULLIF(current_setting('app.admin_id', true), '')::uuid"
    op.execute("ALTER TABLE instance_tool_authorizations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE instance_tool_authorizations FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON instance_tool_authorizations "
        f"USING ({predicate}) WITH CHECK ({predicate})"
    )
    op.execute(f"REVOKE ALL ON instance_tool_authorizations FROM {APP_ROLE}")
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON instance_tool_authorizations TO {APP_ROLE}"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON instance_tool_authorizations")
    op.drop_table("instance_tool_authorizations")
