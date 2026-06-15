"""admin_audit_log — append-only, SHA-256 hash-chained per admin_id.

Architecture §5.2 (schema + event enumeration) and §5.3 (immutability):
  - INSERT only; no UPDATE/DELETE grant to the app role (enforced in rls.py grants).
  - prev_hash = SHA-256 of the prior row's (event_id || created_at || event_payload)
    for the same admin_id; genesis row has prev_hash = NULL.
  - No raw lead PII in event_payload — only hashed resolved_lead_id + non-identifying
    metadata (Architecture §3.4.11 erasure-safe-by-design).
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import BigInteger, DateTime, Enum, Sequence, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin

# Pre-existing DB sequence (created in migration 0003_arc10_audit_seq). Binding it to the
# column makes SQLAlchemy OMIT seq from the INSERT column list and fetch the assigned value
# back via RETURNING — instead of sending an explicit NULL that overrides the column's
# DEFAULT nextval(...). Without this binding, every row was written with seq=NULL, so the
# per-admin "ORDER BY seq" used by both the writer (chain-head lookup) and verify_chain was
# nondeterministic — the intermittent root cause of the audit-chain verification failures.
_audit_seq = Sequence("admin_audit_log_seq")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ActorType(str, enum.Enum):
    admin_user = "admin_user"
    platform = "platform"
    luciel_runtime = "luciel_runtime"


class AdminAuditLog(Base, TenantMixin):
    __tablename__ = "admin_audit_log"

    event_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    instance_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    actor_type: Mapped[ActorType] = mapped_column(
        Enum(ActorType, name="actor_type"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    event_payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    # created_at is APP-ASSIGNED at insert (datetime.now(UTC)), NOT a server default. The
    # hash material includes created_at (§5.2), so the value hashed by the writer must be
    # the EXACT value persisted; an app-assigned value round-trips identically and removes
    # any dependence on server now()/asyncpg timestamptz decoding for chain integrity.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    prev_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Strict monotonic chain-ordering key. Bound to the DB sequence (server_default) so the
    # DB assigns it and SQLAlchemy reads it back — NEVER sent as NULL by the ORM. created_at
    # is transaction-constant (same txn → same timestamp), so seq is the ONLY reliable order
    # key for the per-admin hash chain (§5.3). Nullable kept for the pre-0003 backfilled rows.
    seq: Mapped[int | None] = mapped_column(
        BigInteger, _audit_seq, server_default=_audit_seq.next_value(), nullable=True
    )
