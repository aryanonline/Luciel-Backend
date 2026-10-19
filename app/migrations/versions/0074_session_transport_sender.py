"""session transport sender: the raw sms / voice / email sender on the session

Round 7 phase-2 hardening (security review, takeover delivery routing).

``sessions.participant_key`` is a tenant-salted hash of the transport identifier, so a
reply that has to reach the person in a conversation could only re-derive the
destination from the LEAD record — and a lead known by two phones (a second number,
or one joined to the record by a typed claim) sent the owner's takeover reply to the
number the record was keyed on, not the one in the conversation, stamped delivered.

``transport_sender`` carries the raw sender the channel itself proved (the SMS From,
the caller's number, the email From), persisted at ingress exactly as 0066 persisted
``meta_sender_id`` for Meta. Nullable, no backfill: sessions from before the column
are answered from the call record / participant-key match, or honestly not at all.
Schema-only (no row is touched), so no ``luciel_auth`` step is needed.

Revision ID: 0074_session_transport_sender
Revises: 0070_knowledge_answer_flags
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0074_session_transport_sender"
down_revision: str | None = "0072_escalation_delivery_hardening"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("transport_sender", sa.String(320), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "transport_sender")
