"""m10 follow-ups: the provider message id a human reply went out under

Round 7 M10 (item 9). A human take-over reply on SMS (or a voice reply bridged to a
text) was stamped from Twilio's API ACCEPT and never again: the carrier's delivery
receipt updated ``escalation_events`` only, because nothing on the transcript row said
which message the receipt was about. A reply the carrier then failed to deliver kept
reading as sent on the dashboard.

``transcripts.delivery_provider_message_id`` keeps the provider's id for the reply
(Twilio's MessageSid) so the receipt route can find the row and record the carrier's
word on it. Nullable, no backfill (rows from before the column simply never match a
receipt), with a partial index for the receipt lookup — only rows that carry an id
are indexed. Schema-only (no row is touched), so no ``luciel_auth`` step is needed.

Revision ID: 0076_m10_followups
Revises: 0074_session_transport_sender
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0076_m10_followups"
down_revision: str | None = "0075_audit_created_at_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None

_INDEX = "ix_transcripts_delivery_provider_message_id"


def upgrade() -> None:
    op.add_column(
        "transcripts",
        sa.Column("delivery_provider_message_id", sa.String(64), nullable=True),
    )
    op.create_index(
        _INDEX,
        "transcripts",
        ["admin_id", "delivery_provider_message_id"],
        postgresql_where=sa.text("delivery_provider_message_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="transcripts")
    op.drop_column("transcripts", "delivery_provider_message_id")
