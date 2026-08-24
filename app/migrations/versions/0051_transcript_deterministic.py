"""transcripts.deterministic — fixed-message marker for evidence honesty (#5c)

Live-caught on the 2026-08-23 dev walkthrough: a BRAND-NEW session's greeting
row rendered "Not scored (before scoring existed)" in the evidence panel — a
temporal claim that is false for a row written minutes earlier. The evidence
endpoint had only ``grounding_score IS NULL`` to go on, which cannot separate
a genuine pre-scoring legacy row from a deterministic code-composed reply
(greeting, at-cap notice, escalation ack, LLM-outage ack) that was never a
knowledge answer in the first place.

This column persists that fact at write time: ``deterministic = true`` means
"composed in code, scoring does not apply" and the evidence endpoint serves
``scoringStatus: "not_applicable"`` for it. Existing NULL-score rows keep the
default ``false`` — the backend genuinely cannot vouch for their origin, so
they honestly stay ``not_scored_legacy`` (no backfill; a fixed reply persisted
before this marker existed is indistinguishable from a legacy row).

Additive, expand-only (§5.9.1).

Revision ID: 0051_transcript_deterministic
Revises: 0050_lead_outcome
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0051_transcript_deterministic"
down_revision: str | None = "0050_lead_outcome"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "transcripts",
        sa.Column("deterministic", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("transcripts", "deterministic")
