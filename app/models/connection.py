"""instance_connections (Architecture §3.8.2). The one model for everything external.

Secret material NEVER lives here — only ``secret_ref`` (a Secrets Manager ARN /
SSM path pointer) and ``non_secret_config`` (Architecture §3.8.3).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin

#: The status a Luciel delete writes on every connection (round 7 WP-5, item 1). A row
#: in this state is DRAINING: the inbound resolver still routes its destination to the
#: tenant (migration 0068) so a customer mid-conversation at the delete instant keeps
#: getting answers on their channel, and the outbound seams judge it by the pre-delete
#: status kept in ``status_stashed`` — but the tool broker, the health sweep, the claim
#: gate and the single-active index all treat it exactly as ``revoked``. Defined here,
#: on the model, so every module that keys on it (lifecycle, gate, outbound, the
#: mailbox poll) can import it without a cycle; ``lifecycle.state_machine`` carries the
#: full rationale.
QUIESCING = "quiescing"

#: Statuses that mean "not a live connection": ``revoked`` (terminal — a disconnect, a
#: pre-WP-5 delete, or a superseded drain) and ``quiescing`` (a delete drain in
#: progress). Restore revives both; the day-30 cascade destroys both alike.
NOT_SERVING_STATUSES = frozenset({"revoked", QUIESCING})


class InstanceConnection(Base, TenantMixin, TimestampMixin):
    __tablename__ = "instance_connections"

    connection_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    instance_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    connection_type: Mapped[str] = mapped_column(String(32), nullable=False)
    auth_class: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="unconfigured")
    secret_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    non_secret_config: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    last_health_check_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    # §3.6.4 restore-to-pre-delete-status (C8, migration 0049): delete_luciel stashes
    # the live status/detail here before overwriting with QUIESCING (was 'revoked'
    # before round 7 WP-5); restore puts them back (minus foreign-claimed destinations)
    # and clears the stash. While the row drains, the outbound seams read the stash
    # through ``state_machine.serving_status``. NULL outside a grace window and on
    # legacy rows that entered grace before the column existed.
    status_stashed: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status_detail_stashed: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The knowledge sync JOB (migration 0077, ``app.knowledge.sync_job``): the last
    # manual or scheduled sync's state — ``state`` (syncing | synced | failed |
    # shrink_pending), ``started_at``, ``finished_at``, ``result``, ``error``,
    # ``shrink``, ``trigger``. Read through ``sync_job.effective_job`` (a "syncing"
    # older than the stale window reads as failed). NULL on a row that never synced.
    sync_job: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
