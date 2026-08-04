"""persist the real per-turn grounding score on transcripts (harmony fix 2026-08-04, item 3)

The runtime already computes a real grounding score for every Luciel reply
(app/runtime/grounding.py::score_grounding, used to gate the cannot_answer
escalation in app/runtime/escalation.py::evaluate_outcome) but never persisted
it anywhere. The dashboard's per-message evidence endpoint
(GET /conversations/{session_id}/messages/{message_id}/evidence) therefore had
nothing real to read and returned a hardcoded 0.50 for every message regardless
of what actually happened at runtime -- the observable symptom behind "every
answer scores exactly 0.50" from the owner walkthrough.

This adds a nullable float column: NULL for transcript rows where no grounding
was computed (lead/human_agent turns, and the llm_unavailable technical-issue
ack, which never reached a scored model reply), the real score for every
scored luciel reply going forward. Existing rows stay NULL rather than being
backfilled with a fabricated value.

Revision ID: 0030_transcript_grounding_score
Revises: 0029_audit_retention_candidates
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030_transcript_grounding_score"
down_revision: str | None = "0029_audit_retention_candidates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "transcripts",
        sa.Column("grounding_score", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("transcripts", "grounding_score")
