"""leads.email — the volunteered email beside a phone transport key (round 5A #5)

Live-audited 2026-08-24: an SMS/voice lead who later volunteered an email had it
silently DISCARDED — ``contact_identifier`` was already the phone (the stable
transport key, which must not change: it is how inbound routing recognizes the
person), ``_apply_signals`` only fills that column when NULL, and nothing else
stored the address. Consequence: the email never reached the Lead record, never
reached the CRM push (which refuses non-email keys), and was unrecoverable.

This column is the missing home: the lead's volunteered email when the transport
key is not itself an email. NULL means unknown, or that ``contact_identifier``
already is the email. Additive, expand-only (§5.9.1); no backfill is possible
(the discarded addresses were never persisted anywhere).

Revision ID: 0052_lead_email
Revises: 0051_transcript_deterministic
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0052_lead_email"
down_revision: str | None = "0051_transcript_deterministic"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("leads", sa.Column("email", sa.String(length=320), nullable=True))


def downgrade() -> None:
    op.drop_column("leads", "email")
