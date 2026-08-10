"""sessions gain participant_key + ended_at — conversation = billed session (§3.4.8)

The owner-reported defect: on the non-widget channels every inbound message opened a
brand-new ``sessions`` row (and therefore billed as a new conversation), because the
resolver was only ever given a key on the widget path. The session key ratified in
§3.4.8 is (instance_id, participant_id, channel) — so the resolver needs a participant
identifier that exists from the FIRST message, before lead capture has resolved a lead.

``participant_key`` is a tenant-salted SHA-256 of the channel's transport identifier
(``sha256(admin_id ‖ channel ‖ normalized customer_identifier)``, hex, 64 chars):
  * privacy-lean — no second plaintext copy of a phone number/email in a new index;
  * erasure-safe — deleting the lead's transcripts/sessions removes the data; the hash
    alone identifies nobody;
  * tenant-salted — equal phone numbers on two tenants produce unrelated keys, so the
    index can never correlate participants across tenants (belt to RLS's suspenders).

``ended_at`` is the explicit session-END stamp (§3.4.8): NULL while the session is
live/implicitly-expired, set by the finalization sweep (inactivity timeout) or an
explicit end signal (widget close beacon, voice hangup). A session with ``ended_at``
set is never resumed, even inside the inactivity window — an ended conversation is
ended.

No backfill: historic sessions keep ``participant_key`` NULL. That is correct — their
liveness windows have long passed, so resuming them is not a behavior we want, and a
NULL key simply never matches the resolver's lookup.

Revision ID: 0033_session_participant_key
Revises: 0032_split_meta_channels
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033_session_participant_key"
down_revision: str | None = "0032_split_meta_channels"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("participant_key", sa.String(length=64), nullable=True))
    op.add_column("sessions", sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True))
    # Partial composite index for the resolver's branch-C lookup: newest live session
    # for (tenant, instance, channel, participant). Partial keeps it off widget/legacy
    # rows that carry no key.
    op.create_index(
        "ix_sessions_participant_resume",
        "sessions",
        ["admin_id", "instance_id", "channel", "participant_key", sa.text("last_message_at DESC")],
        postgresql_where=sa.text("participant_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_sessions_participant_resume", table_name="sessions")
    op.drop_column("sessions", "ended_at")
    op.drop_column("sessions", "participant_key")
