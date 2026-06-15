"""arc14b voice: voice_call_records table (disclosure-delivery log) + RLS

Per-inbound-voice-call metadata, including the MANDATORY disclosure-played timestamp
(Legal §3.1.2 / Architecture §3.1.2 — the recording/AI disclosure delivery is logged
so the consent posture is auditable). Tenant-scoped with the three-layer Layer-2 RLS
(§3.7.2b) so the generic isolation sweep auto-covers it. No raw transcript/PII is
stored here — the transcript lives in ``transcripts`` under retention (§3.4.10).

Revision ID: 0008_arc14b_voice_call_records
Revises: 0007_arc14_runtime_budget
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_arc14b_voice_call_records"
down_revision: str | None = "0007_arc14_runtime_budget"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None

APP_ROLE = "luciel_app"


def upgrade() -> None:
    op.create_table(
        "voice_call_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("admin_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("instance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider_call_id", sa.String(length=64), nullable=False),
        sa.Column("from_number", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "disclosure_played_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_voice_call_admin_instance", "voice_call_records", ["admin_id", "instance_id"]
    )
    op.create_index("ix_voice_call_provider_call_id", "voice_call_records", ["provider_call_id"])
    predicate = "admin_id = NULLIF(current_setting('app.admin_id', true), '')::uuid"
    op.execute("ALTER TABLE voice_call_records ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE voice_call_records FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON voice_call_records "
        f"USING ({predicate}) WITH CHECK ({predicate})"
    )
    op.execute(f"REVOKE ALL ON voice_call_records FROM {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON voice_call_records TO {APP_ROLE}")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON voice_call_records")
    op.drop_table("voice_call_records")
