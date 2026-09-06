"""Lead stewardship (2026-09-05 audit WP6).

* ``leads.phone`` — the lead's volunteered phone in E.164 (F111). Until now a caller
  who typed their number into the widget had it kept only as a raw ``contact``
  string or dropped once an email existed.
* ``lead_identifiers`` — every identifier a lead has ever been known by (email,
  phone, transport id), one row per value (F125/F127). Unifying two rows used to
  keep only the survivor's two columns, so the absorbed identity stopped resolving
  and the next contact from it became a fresh duplicate. Backfilled from the
  existing columns; TenantMixin + RLS like every tenant table; erased with the
  lead and destroyed by the tenant cascade.
* ``leads.crm_push_status/detail/pushed_at/failure_notified_at`` — the honest
  CRM state the dashboard shows (F134): synced / failed, the non-secret reason,
  when, and whether the owner was already emailed about this lead (once).

Revision ID: 0059_lead_stewardship
Revises: 0058_knowledge_many_connections
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0059_lead_stewardship"
down_revision: str | None = "0058_knowledge_many_connections"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None

APP_ROLE = "luciel_app"
_PREDICATE = "admin_id = NULLIF(current_setting('app.admin_id', true), '')::uuid"


def _lock_down(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
    )
    op.execute(f"REVOKE ALL ON {table} FROM {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")


def upgrade() -> None:
    op.add_column("leads", sa.Column("phone", sa.String(length=32), nullable=True))
    op.add_column("leads", sa.Column("crm_push_status", sa.String(length=16), nullable=True))
    op.add_column("leads", sa.Column("crm_push_detail", sa.String(length=160), nullable=True))
    op.add_column("leads", sa.Column("crm_pushed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "leads",
        sa.Column("crm_failure_notified_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "lead_identifiers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("admin_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("instance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("lead_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("value", sa.String(length=320), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("admin_id", "instance_id", "kind", "value", name="uq_lead_identifier"),
    )
    op.create_index("ix_lead_identifiers_admin_id", "lead_identifiers", ["admin_id"])
    op.create_index("ix_lead_identifiers_lead", "lead_identifiers", ["admin_id", "lead_id"])
    _lock_down("lead_identifiers")

    # Backfill: every identifier the existing rows already carry, so the new lookup
    # resolves old leads exactly as the old column-only lookup did.
    for column, folded in (("contact_identifier", False), ("email", True)):
        value = f"lower({column})" if folded else column
        op.execute(
            sa.text(
                f"""
                INSERT INTO lead_identifiers (id, admin_id, instance_id, lead_id, kind, value)
                SELECT gen_random_uuid(), admin_id, instance_id, id,
                       CASE WHEN {column} LIKE '%@%' THEN 'email'
                            WHEN {column} ~ '^\\+[1-9][0-9]{{7,14}}$' THEN 'phone'
                            ELSE 'transport' END,
                       CASE WHEN {column} LIKE '%@%' THEN lower({column}) ELSE {value} END
                FROM leads
                WHERE {column} IS NOT NULL AND {column} <> ''
                ON CONFLICT ON CONSTRAINT uq_lead_identifier DO NOTHING
                """  # noqa: S608 — column names are literals from this file, never input
            )
        )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON lead_identifiers")
    op.drop_index("ix_lead_identifiers_lead", table_name="lead_identifiers")
    op.drop_index("ix_lead_identifiers_admin_id", table_name="lead_identifiers")
    op.drop_table("lead_identifiers")
    for column in (
        "crm_failure_notified_at",
        "crm_pushed_at",
        "crm_push_detail",
        "crm_push_status",
        "phone",
    ):
        op.drop_column("leads", column)
