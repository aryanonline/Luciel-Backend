"""persist real retriever chunk references on transcripts (harmony wave 2, item 2a)

The retriever (app/knowledge/service.py::retrieve) already returns
KnowledgeChunk rows carrying source_id/text, but the orchestrator's retriever
seam (app/runtime/knowledge_retrieval.py::hybrid_retrieve) collapsed them to
plain strings before persist_turn ever saw them, so Transcript.source_chunks
never existed and the dashboard's evidence endpoint always rendered
`sourceChunks: []` -- which the frontend then rendered as "No knowledge source
backed this answer", even for a genuinely KB-backed reply. This adds a nullable
JSONB column: NULL for rows where attribution was never captured (all existing
rows, and any future row on a path that doesn't retrieve), a real list of
{sourceId, sourceName, text} for luciel replies going forward whose retriever
returned chunks it used, and an empty list ONLY when retrieval genuinely ran and
found nothing. NULL vs [] is a real distinction the evidence endpoint now
exposes via attributionStatus rather than fabricating -- existing rows stay
NULL rather than being backfilled with an invented value.

Revision ID: 0031_transcript_source_chunks
Revises: 0030_transcript_grounding_score
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0031_transcript_source_chunks"
down_revision: str | None = "0030_transcript_grounding_score"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "transcripts",
        sa.Column("source_chunks", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("transcripts", "source_chunks")
