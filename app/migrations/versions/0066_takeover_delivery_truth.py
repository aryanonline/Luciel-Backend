"""takeover delivery truth: reply context on the session, delivery outcome on the row

Round 7 WP-3b (live takeover truth on every channel).

``sessions`` gains what a later human reply needs to go out AS the conversation it
answers, persisted at ingress instead of re-derived (and mis-derived) at reply time:
  * ``inbound_subject`` / ``inbound_message_id`` — the customer's latest email subject
    and provider message id (the Graph id on a BYO mailbox, the RFC 5322 Message-ID on
    the legacy SES path), so a takeover reply threads under their mail instead of
    landing as a new "Re: your inquiry".
  * ``meta_destination_id`` / ``meta_sender_id`` — the tenant surface the Meta
    conversation arrived on (page / IG account / WhatsApp phone-number id) and the
    customer's platform-scoped id, so the reply is sent AS that surface TO that id
    rather than resolved from a channel name (a legacy ``instagram_messenger`` row
    resolved the wrong grant).
  * ``human_controlled_since`` — when the current takeover began; the hand-back audit
    event carries the real ``duration_seconds`` off it.

``transcripts`` gains ``delivery_status`` / ``delivery_detail`` / ``delivered_at`` for
``human_agent`` rows: the delivery outcome was returned once and audited but never
persisted, so a reply Twilio rejected read like any other message when the owner
reopened the conversation. NULL on every other row (and on rows from before the
column existed) — never a fabricated ``delivered``.

Revision ID: 0066_takeover_delivery_truth
Revises: 0065_embedding_verification
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0066_takeover_delivery_truth"
down_revision: str | None = "0065_embedding_verification"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("inbound_subject", sa.String(998), nullable=True))
    op.add_column("sessions", sa.Column("inbound_message_id", sa.String(998), nullable=True))
    op.add_column("sessions", sa.Column("meta_destination_id", sa.String(128), nullable=True))
    op.add_column("sessions", sa.Column("meta_sender_id", sa.String(128), nullable=True))
    op.add_column(
        "sessions",
        sa.Column("human_controlled_since", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("transcripts", sa.Column("delivery_status", sa.String(16), nullable=True))
    op.add_column("transcripts", sa.Column("delivery_detail", sa.String(64), nullable=True))
    op.add_column(
        "transcripts", sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("transcripts", "delivered_at")
    op.drop_column("transcripts", "delivery_detail")
    op.drop_column("transcripts", "delivery_status")
    op.drop_column("sessions", "human_controlled_since")
    op.drop_column("sessions", "meta_sender_id")
    op.drop_column("sessions", "meta_destination_id")
    op.drop_column("sessions", "inbound_message_id")
    op.drop_column("sessions", "inbound_subject")
