"""sms contact confirmation: a channel-agnostic proof that a contact can hear a page

Round 7 WP-4b (2026-09-21). An SMS escalation contact was never proven: the "test
text" route persisted nothing, Twilio accepting a message was read as delivery, and an
unconfirmed number — unlike an unconfirmed email address — never had the owner copied.

  * ``escalation_contact_confirmations`` — one row per (tenant, kind, normalized value)
    for BOTH kinds (``email`` | ``sms``): when the contact confirmed, when a code or link
    was last sent, how many wrong codes were entered, and the hashed 6-digit code an SMS
    contact still has to answer. Standard tenant RLS (0054 pattern). The email loop keeps
    its bounce state on ``email_delivery_incidents`` and mirrors its confirmation here.

  * ``escalation_events.delivery_provider_message_id`` — the provider id of the send a
    row's ``delivery_status`` rests on, so a Twilio status callback (a real DLR) can find
    the row it is reporting on and stamp ``delivered`` / ``failed`` from the carrier's
    word rather than from the API accepting the request.

  * ``platform_escalation_backlog()`` grows ``oldest_queued_at`` — the persisted moment
    the current backlog began — so ``/healthz`` can say a backlog has outlived a sweep
    interval without keeping state in a process. Same SECURITY DEFINER posture as 0057;
    the return type changes, so the function is dropped and recreated.

Revision ID: 0069_sms_contact_confirmation
Revises: 0065_embedding_verification
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0069_sms_contact_confirmation"
down_revision: str | None = "0065_embedding_verification"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None

APP_ROLE = "luciel_app"
_PREDICATE = "admin_id = NULLIF(current_setting('app.admin_id', true), '')::uuid"
_TABLE = "escalation_contact_confirmations"

# 0057's function verbatim, restored on downgrade.
_BACKLOG_0057 = """
    CREATE OR REPLACE FUNCTION platform_escalation_backlog()
    RETURNS TABLE(queued bigint, failed_24h bigint)
    LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
        SELECT
            (SELECT count(*) FROM deferred_actions d
              WHERE d.action_type = 'escalation_notification' AND d.status = 'queued'),
            (SELECT count(*) FROM escalation_events e
              WHERE e.delivery_status = 'failed'
                AND e.fired_at > now() - interval '24 hours')
    $$;
"""

_BACKLOG_0069 = """
    CREATE OR REPLACE FUNCTION platform_escalation_backlog()
    RETURNS TABLE(queued bigint, failed_24h bigint, oldest_queued_at timestamptz)
    LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
        SELECT
            (SELECT count(*) FROM deferred_actions d
              WHERE d.action_type = 'escalation_notification' AND d.status = 'queued'),
            (SELECT count(*) FROM escalation_events e
              WHERE e.delivery_status = 'failed'
                AND e.fired_at > now() - interval '24 hours'),
            (SELECT min(d.created_at) FROM deferred_actions d
              WHERE d.action_type = 'escalation_notification' AND d.status = 'queued')
    $$;
"""


def _install_backlog_function(body: str) -> None:
    op.execute("DROP FUNCTION IF EXISTS platform_escalation_backlog()")
    op.execute(body)
    op.execute("ALTER FUNCTION platform_escalation_backlog() OWNER TO luciel_auth")
    op.execute(f"GRANT EXECUTE ON FUNCTION platform_escalation_backlog() TO {APP_ROLE}")


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("admin_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("value_normalized", sa.String(length=320), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_code_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("code_hash", sa.String(length=64), nullable=True),
        sa.Column("code_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "admin_id", "kind", "value_normalized", name="uq_escalation_contact_confirmation"
        ),
    )
    op.create_index(f"ix_{_TABLE}_admin_id", _TABLE, ["admin_id"])
    op.execute(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {_TABLE} USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
    )
    op.execute(f"REVOKE ALL ON {_TABLE} FROM {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {_TABLE} TO {APP_ROLE}")

    op.add_column(
        "escalation_events",
        sa.Column("delivery_provider_message_id", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_escalation_events_provider_message_id",
        "escalation_events",
        ["admin_id", "delivery_provider_message_id"],
    )
    _install_backlog_function(_BACKLOG_0069)


def downgrade() -> None:
    _install_backlog_function(_BACKLOG_0057)
    op.drop_index("ix_escalation_events_provider_message_id", table_name="escalation_events")
    op.drop_column("escalation_events", "delivery_provider_message_id")
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {_TABLE}")
    op.drop_table(_TABLE)
