"""Voice call records — per-call metadata + mandatory disclosure delivery log.

Legal §3.1.2 / Architecture §3.1.2 require that the pre-call AI-identity +
recording/transcription disclosure delivery is LOGGED ("call metadata: disclosure
played at call start") so the consent posture is auditable, and that these logs are
available to the Admin on request (Legal §3.1.2 voice-channel disclosure logs). This
table is the durable record of that: one row per inbound voice call, carrying the
provider call id, the resolved tenant, and ``disclosure_played_at`` — which is set,
non-nullably at insert, by the adapter BEFORE any caller audio is transcribed.

TenantMixin so the three-layer isolation (§3.7.2b) + the generic isolation sweep
cover it automatically — no per-tenant call metadata is ever cross-tenant visible.
No raw transcript/PII is stored here (the transcript lives in ``transcripts`` under
the retention policy §3.4.10); this row holds only call-level metadata.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin


class VoiceCallRecord(Base, TenantMixin, TimestampMixin):
    """One inbound voice call's metadata, including the disclosure-played timestamp."""

    __tablename__ = "voice_call_records"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    instance_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    # The Twilio Voice CallSid — the provider's stable call identifier.
    provider_call_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # Caller E.164 (a strong identity, §3.4.9). Stored to link the call to a lead/session;
    # it is the same identifier SMS uses, not extra PII beyond the channel's nature.
    from_number: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    # The session the call's turns are persisted under (set once the loop opens one).
    session_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    # MANDATORY (§3.1.2): the moment the recording/AI disclosure was played at call
    # start. NOT NULL — a call record cannot exist without the disclosure having been
    # delivered first, which is the auditable proof the consent posture held.
    disclosure_played_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
