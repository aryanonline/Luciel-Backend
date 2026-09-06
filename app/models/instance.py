"""Account/owner and the Luciel instance — the foundational tenant entities.

One account == one owner == one Luciel (Architecture §3.7.1, Locked Decision §7 #12).
``admin_id`` is the tenant key; ``instance_id`` is one-to-one with it but kept as a
stable internal identifier (Architecture §3.1.1 note) and to leave a clean
multi-Luciel migration path.

The ``accounts`` table is the auth root. It is itself tenant-scoped (admin_id ==
its own id) so the single three-layer pattern applies uniformly; auth-time lookups
(by email) run on a narrow path before a tenant subject exists (see session.py).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin


class AccountState(str, enum.Enum):
    unverified = "unverified"
    verified = "verified"
    paused = "paused"
    pending_deletion = "pending_deletion"
    closed = "closed"


class LucielState(str, enum.Enum):
    active = "active"
    paused = "paused"
    luciel_grace_window = "luciel_grace_window"
    luciel_hard_deleted = "luciel_hard_deleted"


#: States in which Luciel must not answer a customer (§3.6.2). ``active`` is the only
#: serving state; everything else is paused, mid-grace, or gone.
NON_SERVING_STATES = frozenset(
    {LucielState.paused, LucielState.luciel_grace_window, LucielState.luciel_hard_deleted}
)


def is_paused(inst: Instance) -> bool:
    """True when the instance must not run a turn on any channel."""
    return inst.state in NON_SERVING_STATES


#: Channel ids that are never switchable off through the pillar: the widget is the
#: Luciel's own front door and has no enabled flag on the wire.
ALWAYS_ENABLED_CHANNELS = frozenset({"widget"})


def channel_enabled(inst: Instance, channel: str) -> bool:
    """Whether the owner has this channel switched ON in the channels pillar (§3.1).

    Disabling a channel never disconnects its provider (root CLAUDE.md) — but it must
    stop NEW conversations on it (2026-09-05 audit F058: only Voice checked the flag;
    SMS, email, WhatsApp, Messenger and Instagram kept answering and billing while the
    pillar said "off"). The legacy combined ``instagram_messenger`` session id (§3.1.2
    split) reads as on when either of its halves is on.
    """
    if channel in ALWAYS_ENABLED_CHANNELS:
        return True
    wanted = {"instagram", "messenger"} if channel == "instagram_messenger" else {channel}
    for entry in inst.channels or []:
        if isinstance(entry, dict) and entry.get("id") in wanted and bool(entry.get("enabled")):
            return True
    return False


class Account(Base, TenantMixin, TimestampMixin):
    """The single account owner. ``admin_id`` mirrors the primary key (self-tenant)."""

    __tablename__ = "accounts"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    # email_lower is the uniqueness/lookup key; UNIQUE only over verified rows would
    # require a partial index — handled in migration. Stored lowercased for lookup.
    email_lower: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    state: Mapped[AccountState] = mapped_column(
        Enum(AccountState, name="account_state"), default=AccountState.unverified, nullable=False
    )
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    has_completed_first_run: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Stripe customer id (created lazily at checkout). Non-secret reference.
    stripe_customer_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Session epoch: bumped on password reset to revoke all sessions (§3.7.1a).
    session_epoch: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Single-use link token tracking (jti of the currently-valid link; reissue invalidates).
    pending_verify_jti: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pending_reset_jti: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Resend rate-limit window (3 / 15 min): count + window start.
    resend_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    resend_window_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Instance(Base, TenantMixin, TimestampMixin):
    """The one Luciel per account."""

    __tablename__ = "instances"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    website_url: Mapped[str] = mapped_column(String(2048), nullable=False, default="")
    business_one_liner: Mapped[str] = mapped_column(String(280), nullable=False, default="")
    state: Mapped[LucielState] = mapped_column(
        Enum(LucielState, name="luciel_state"), default=LucielState.active, nullable=False
    )
    embed_key_public_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # The key's value while the Luciel sits in the delete grace window (§3.6.3
    # quiesce): resolution keys on embed_key_public_id, so a stashed key accepts
    # no inbound — but restore revives the SAME key, keeping the customer's
    # deployed snippet alive ("exactly as it was", §3.6.4). Cleared on restore;
    # destroyed with the row by the day-30 cascade.
    embed_key_stashed: Mapped[str | None] = mapped_column(String(64), nullable=True)
    grace_window_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Five-pillar config stored as JSONB (camelCase wire shapes round-trip cleanly).
    # Channels/tools/escalation/personality are per-instance config the owner edits;
    # connection health is layered in from instance_connections at read time (Arc 17).
    channels: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False, default=list)
    tools: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False, default=list)
    escalation: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    personality: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    # Opt-in lead auto-prune rule (§3.4.10a / Legal §B5). NULL means OFF, and off is the
    # default: captured Leads are the Admin's own business data, so the platform never
    # timer-deletes them unless the Admin asked it to. When set, it is the Admin's own
    # retention choice — leads with no activity for this many days are pruned by the
    # retention sweep.
    lead_retention_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Soft-delete marker mirrored onto the state machine (grace window).
    soft_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
