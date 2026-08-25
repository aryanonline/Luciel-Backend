"""email_delivery_incidents — bounce state + contact verification (round 5B #13)

The escalation contact was a bare string: SES accepting a message set
``accepted=True``, which suppressed the owner-email fallback and the retry queue
— so a typo'd-but-deliverable-looking address swallowed every hot-lead
notification with a green audit trail, and nothing anywhere recorded a bounce.

One row per (tenant, address): the SES events webhook writes bounce/complaint
state, the verification loop writes confirmation state, and escalation dispatch
derives suppression from the two. Sibling table (not escalation-JSONB markers):
the pillar PUT is a strict full-replace the frontend echoes back, and the bounce
writer is a webhook racing it. Standard tenant RLS (0043 pattern).

Revision ID: 0054_email_delivery_incidents
Revises: 0053_crm_link_lead_id
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0054_email_delivery_incidents"
down_revision: str | None = "0053_crm_link_lead_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None

APP_ROLE = "luciel_app"
_PREDICATE = "admin_id = NULLIF(current_setting('app.admin_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "email_delivery_incidents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("admin_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email_lower", sa.String(length=320), nullable=False),
        sa.Column("hard_bounced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("complained_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("soft_bounce_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_event_detail", sa.String(length=200), nullable=True),
        sa.Column("verification_pending_jti", sa.String(length=64), nullable=True),
        sa.Column("verification_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("admin_id", "email_lower", name="uq_email_incident_address"),
    )
    op.create_index(
        "ix_email_delivery_incidents_admin_id", "email_delivery_incidents", ["admin_id"]
    )
    op.execute("ALTER TABLE email_delivery_incidents ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE email_delivery_incidents FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON email_delivery_incidents "
        f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
    )
    op.execute(f"REVOKE ALL ON email_delivery_incidents FROM {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON email_delivery_incidents TO {APP_ROLE}")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON email_delivery_incidents")
    op.drop_table("email_delivery_incidents")
