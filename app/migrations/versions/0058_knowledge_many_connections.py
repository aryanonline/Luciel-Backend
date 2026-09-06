"""knowledge sources: many connections per tenant + the embedding model behind each chunk

Two knowledge-pillar corrections from the 2026-09-05 product-owner audit:

  * ``uq_conn_active_type`` no longer covers ``knowledge_source`` (F092/F093). The
    single-active-per-type rule (§3.8.2) is right for a sender or a CRM — one Twilio,
    one mailbox, one CRM — and wrong for knowledge: a business has a Drive folder AND
    a Notion workspace AND its website, and "connect Notion" used to reset the Drive
    row to unconfigured and drop its token. Every other connection type keeps the
    rule byte-for-byte. Each knowledge row carries its own ``secret_ref``
    (``vm/{admin}/{instance}/knowledge_source/{connection_id}``); rows from before
    this migration keep the legacy shared ref they were written with.

  * ``knowledge_chunks.embedding_model`` (F121) — which embedding model produced each
    vector. Vectors are model-specific: a query embedded by a newer model scored
    against chunks embedded by the old one is silently wrong, and nothing recorded
    which chunks needed re-embedding after a model change. Backfilled to the model
    configured at migration time — the only model that has ever run, since the
    embedding pin is never auto-swapped (app/runtime/model_currency.py). The
    knowledge_sync sweep re-embeds chunks whose model differs from the current pin.

Revision ID: 0058_knowledge_many_connections
Revises: 0057_escalation_delivery_truth
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0058_knowledge_many_connections"
down_revision: str | None = "0057_escalation_delivery_truth"
branch_labels: str | None = None
depends_on: str | None = None

_DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"


def _configured_embedding_model() -> str:
    try:
        from app.config import get_settings  # noqa: PLC0415

        return str(get_settings().knowledge_embedding_model or _DEFAULT_EMBEDDING_MODEL)
    except Exception:  # noqa: BLE001 — a migration must not depend on a full runtime config
        return _DEFAULT_EMBEDDING_MODEL


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_conn_active_type")
    op.execute(
        "CREATE UNIQUE INDEX uq_conn_active_type ON instance_connections "
        "(admin_id, instance_id, connection_type) "
        "WHERE status <> 'revoked' AND connection_type <> 'knowledge_source'"
    )
    op.add_column("knowledge_chunks", sa.Column("embedding_model", sa.String(64), nullable=True))
    op.execute(
        sa.text(
            "UPDATE knowledge_chunks SET embedding_model = :model WHERE embedding_model IS NULL"
        ).bindparams(model=_configured_embedding_model())
    )


def downgrade() -> None:
    op.drop_column("knowledge_chunks", "embedding_model")
    # Restoring the single-active rule fails if a tenant holds several live knowledge
    # connections — that is the point of the upgrade; revoke the extras first.
    op.execute("DROP INDEX IF EXISTS uq_conn_active_type")
    op.execute(
        "CREATE UNIQUE INDEX uq_conn_active_type ON instance_connections "
        "(admin_id, instance_id, connection_type) WHERE status <> 'revoked'"
    )
