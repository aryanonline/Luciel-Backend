"""internal_access_log: append-only platform staff access audit artifact (§5.11 / C23)

Architecture §5.11 / Commitment C23: The internal access log is "a concrete,
append-only artifact, not a placeholder" that captures platform staff access events
for the no-standing-access / break-glass policy. Separate from admin_audit_log.

Table fields per §5.11:
  - acting staff id (staff_id)
  - second-person approver id (approver_id)
  - admin_id / instance_id / data_class (scope)
  - access start / end timestamps
  - justification + ticket reference
  - access_type: break_glass | support_scoped

Append-only enforcement:
  - Application role ``luciel_app`` is granted INSERT only (no UPDATE/DELETE).
  - A BEFORE UPDATE/DELETE trigger (guard) raises an exception on any attempt to
    mutate existing rows — same pattern as admin_audit_log (B1 / §5.3).

Expand-contract: the table is created fresh (no existing data). Downgrade drops it.

Revision ID: 0012_internal_access_log
Revises: 0011_arc18_billing_accounts
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_internal_access_log"
down_revision: str | None = "0011_arc18_billing_accounts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "internal_access_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        # Staff actors (§5.11).
        sa.Column("staff_id", sa.String(255), nullable=False),
        sa.Column("approver_id", sa.String(255), nullable=True),
        # Scope: tenant / instance / data class (nullable — not all access is per-tenant).
        sa.Column("admin_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("instance_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("data_class", sa.String(64), nullable=True),
        # Access time window (§5.11: start + end).
        sa.Column("access_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("access_ended_at", sa.DateTime(timezone=True), nullable=True),
        # Access type: break_glass | support_scoped (§5.11).
        sa.Column("access_type", sa.String(32), nullable=False),
        # Justification + ticket (§5.11 mandatory for break_glass).
        sa.Column("justification", sa.Text(), nullable=True),
        sa.Column("ticket_ref", sa.String(255), nullable=True),
        # Timestamp (created_at = when the row was inserted — append-only).
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # Index on admin_id for lookup by tenant in audit support queries.
    op.create_index(
        "ix_internal_access_log_admin_id",
        "internal_access_log",
        ["admin_id"],
    )
    # Index on staff_id for looking up all access events by a given staff member.
    op.create_index(
        "ix_internal_access_log_staff_id",
        "internal_access_log",
        ["staff_id"],
    )

    # Append-only guard trigger: BEFORE UPDATE/DELETE → raise exception.
    # Same pattern as the admin_audit_log guard (B1, scripts/lock_audit_log.sql).
    op.execute(
        """
        CREATE OR REPLACE FUNCTION guard_internal_access_log_immutable()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'internal_access_log is append-only (Architecture §5.11)';
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_internal_access_log_no_update
        BEFORE UPDATE OR DELETE ON internal_access_log
        FOR EACH ROW EXECUTE FUNCTION guard_internal_access_log_immutable();
        """
    )

    # INSERT-only grant for the application role (luciel_app).
    # Revoke UPDATE/DELETE just to be explicit (they are not granted by default but
    # belt-and-suspenders matches the admin_audit_log pattern, §5.3).
    op.execute("GRANT INSERT ON internal_access_log TO luciel_app;")
    op.execute("REVOKE UPDATE, DELETE ON internal_access_log FROM luciel_app;")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_internal_access_log_no_update ON internal_access_log;")
    op.execute("DROP FUNCTION IF EXISTS guard_internal_access_log_immutable();")
    op.drop_index("ix_internal_access_log_staff_id", table_name="internal_access_log")
    op.drop_index("ix_internal_access_log_admin_id", table_name="internal_access_log")
    op.drop_table("internal_access_log")
