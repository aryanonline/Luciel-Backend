"""sessions.input_ask_attempts — the §3.3.7 ask-bound counter (audit round 3, C4)

Required-input resolution rule 4: asking for a missing tool input is bounded, not
infinite. The orchestrator increments this per session on a turn that stalled on a
broker ``missing_input``/``ambiguous`` rejection with nothing resolving, resets it
on any ``ok`` outcome, and at ASK_ATTEMPT_BOUND hands off to a human instead of
re-asking. Persisted (not in-memory) because the stall spans TURNS — each turn is
its own request/process, so an in-memory counter would reset on every message and
the bound would never fire.

Additive, expand-only (§5.9.1): nullable-free with a server default so existing
rows read 0 — no backfill, no contract step needed.

Revision ID: 0046_session_ask_attempts
Revises: 0045_unique_sms_destination
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0046_session_ask_attempts"
down_revision: str | None = "0045_unique_sms_destination"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column("input_ask_attempts", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("sessions", "input_ask_attempts")
