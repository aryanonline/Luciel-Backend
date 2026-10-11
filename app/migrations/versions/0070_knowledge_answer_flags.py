"""knowledge serving truth after a revocation + the persisted answer flag

Round 7 WP-4c (owner decision D10, 2026-09-14):

  * ``knowledge_sources.paused_since`` — when a source's sync was paused by a
    revoked/expired credential (``sync_status = paused_reconnect_needed``). Until now
    a revoked source kept answering forever: retrieval filtered only on tenant and
    tombstone. Retrieval and graph traversal now exclude a source paused for more than
    PAUSED_SERVING_DAYS (7); the knowledge_sync sweep warns the owner on day 1 and
    tells them on day 7, once per incident (the notice_deliveries ledger). A clean
    sync clears the clock. Sources already paused when this runs start their clock
    NOW — the owner gets the day-1 warning at the next sweep and the full seven days.

  * ``transcripts.flagged_by_admin_at`` — the admin's answer flag (§3.4.13) was
    audit-only: the evidence read returned ``flagged_by_admin = False`` for every
    message, including one the owner had just flagged.

Revision ID: 0070_knowledge_answer_flags
Revises: 0065_embedding_verification
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0070_knowledge_answer_flags"
down_revision: str | None = "0069_sms_contact_confirmation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "knowledge_sources",
        sa.Column("paused_since", sa.DateTime(timezone=True), nullable=True),
    )
    # knowledge_sources is FORCE RLS and alembic runs as luciel_app with no tenant
    # pinned: a plain UPDATE here matches ZERO rows and the backfill is a silent no-op
    # (round 7 review; 0067 and 0068 avoid the same hazard the same way). The narrow
    # BYPASSRLS role does the write (SELECT too: the WHERE reads columns), under a grant
    # that lasts only for this statement.
    op.execute("GRANT SELECT, UPDATE ON knowledge_sources TO luciel_auth")
    op.execute("SET LOCAL ROLE luciel_auth")
    op.execute(
        """
        UPDATE knowledge_sources
           SET paused_since = now()
         WHERE sync_status = 'paused_reconnect_needed'
           AND soft_deleted_at IS NULL
           AND paused_since IS NULL
        """
    )
    op.execute("RESET ROLE")
    op.execute("REVOKE SELECT, UPDATE ON knowledge_sources FROM luciel_auth")
    op.add_column(
        "transcripts",
        sa.Column("flagged_by_admin_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("transcripts", "flagged_by_admin_at")
    op.drop_column("knowledge_sources", "paused_since")
