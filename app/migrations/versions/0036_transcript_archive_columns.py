"""sessions gain transcript-archive markers — hot→cold offload at 90 days (#38)

Commitment #38 / Architecture §3.4.10: transcripts move from hot Postgres to S3 cold
storage after 90 days and hard-delete at the 1-year boundary, so the hot store and
the cold store both PLATEAU. The markers live on the SESSION (not per transcript
row) because "moved" means the hot rows are deleted once the object is durably
written — a per-row marker would be deleted with the rows it marks.

  * ``transcripts_archived_at``  — when the archive sweep sealed this session's
    transcript into S3 (NULL = still hot, or zero-turn).
  * ``archive_object_key``      — the JSONL object's key
    (``vm/{admin_id}/transcripts/{instance_id}/{session_id}.jsonl``). What the
    dashboard read-through, the export bundle, the retention sweep's object
    deletion, and the cascade's prefix sweep all navigate by.

Partial index for the sweep's candidate scan: ended, unarchived sessions by age.

Revision ID: 0036_transcript_archive_columns
Revises: 0035_lifetime_conversations
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036_transcript_archive_columns"
down_revision: str | None = "0035_lifetime_conversations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column("transcripts_archived_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("sessions", sa.Column("archive_object_key", sa.String(length=255), nullable=True))
    op.create_index(
        "ix_sessions_archive_due",
        "sessions",
        ["admin_id", "last_message_at"],
        postgresql_where=sa.text("transcripts_archived_at IS NULL AND ended_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_sessions_archive_due", table_name="sessions")
    op.drop_column("sessions", "archive_object_key")
    op.drop_column("sessions", "transcripts_archived_at")
