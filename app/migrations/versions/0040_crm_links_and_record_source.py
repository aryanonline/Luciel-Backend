"""crm_lead_links + record_source_records — the stores behind push_to_crm/lookup_record

Two advertised tools finally gain their effect layer (owner scope decision):

  * ``crm_lead_links`` — maintains "the SAME lead's CRM record" (§3.3.6): pins
    ``lead_key → crm_record_id`` per provider so every later push updates the
    record the first push created — update-in-place, never a duplicate, even if
    the lead's email changes. Unique on (admin, instance, provider, lead_key).
  * ``record_source_records`` — the ``csv`` provider's live system of record for
    ``lookup_record``: the admin's uploaded rows, queried at call time (upload
    replaces; never a knowledge-store snapshot).

Both TenantMixin: RLS ENABLE+FORCE + tenant_isolation policy, same grants as
every tenant table; joined to the delete cascade in app/lifecycle/cascade.py.

Revision ID: 0040_crm_links_and_record_source
Revises: 0039_embed_key_stash
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0040_crm_links_and_record_source"
down_revision: str | None = "0039_embed_key_stash"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None

APP_ROLE = "luciel_app"
_PREDICATE = "admin_id = NULLIF(current_setting('app.admin_id', true), '')::uuid"


def _lock_down(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
    )
    op.execute(f"REVOKE ALL ON {table} FROM {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")


def upgrade() -> None:
    op.create_table(
        "crm_lead_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("admin_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("instance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("lead_key", sa.String(length=320), nullable=False),
        sa.Column("crm_record_id", sa.String(length=128), nullable=False),
        sa.Column(
            "last_pushed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "admin_id", "instance_id", "provider", "lead_key", name="uq_crm_lead_link"
        ),
    )
    op.create_index("ix_crm_lead_links_admin_id", "crm_lead_links", ["admin_id"])
    _lock_down("crm_lead_links")

    op.create_table(
        "record_source_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("admin_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("instance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("fields", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "uploaded_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_record_source_records_admin_id", "record_source_records", ["admin_id"])
    _lock_down("record_source_records")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON record_source_records")
    op.drop_table("record_source_records")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON crm_lead_links")
    op.drop_table("crm_lead_links")
