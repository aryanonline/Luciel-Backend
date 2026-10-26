"""connection sync job: the manual knowledge sync's state lives on the connection

Round 7, live walk run 8 (2026-10-25). The manual "crawl a website" ran inside the
owner's request: 8 pages fetched in a second, then one embeddings call and one LLM
graph call per page (4-16 s each), and the load balancer's 60 s idle timeout cut the
request with a CORS-less 504 the dashboard read as "Network request failed." — while
the sync kept working server-side. The sync is now a job: the request waits a bounded
time and answers 202 when the job is still running, and the job's state is kept on the
connection row so any later read (GET /connections, GET /sync-connections) can say
"syncing", "synced" with the result, "failed" with the reason, or "shrink_pending".

``instance_connections.sync_job`` is a nullable JSONB (``state``, ``started_at``,
``finished_at``, ``result``, ``error``, ``shrink``, ``trigger``). No backfill: a row
that never ran a job reads NULL. Schema-only (no row is touched), so no
``luciel_auth`` step is needed.

Revision ID: 0077_connection_sync_job
Revises: 0076_m10_followups
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0077_connection_sync_job"
down_revision: str | None = "0076_m10_followups"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "instance_connections",
        sa.Column("sync_job", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("instance_connections", "sync_job")
