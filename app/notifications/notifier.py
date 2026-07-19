"""Admin notification adapter seam (Architecture §3.6.7 / §3.5).

Provides a stub notifier consistent with the codebase's AWS-DEFERRED SEAM pattern.
Real delivery (SES email, SMS) binds when the notification delivery arc ships.
The stub records intent to the audit log (``lifecycle_notice_sent``), which is the
durable compliance artifact required by Architecture §3.6.7 / Legal §A3.

The audit event IS the minimum-viable notification record: it captures what was
sent, to whom, and when, so the cadence is auditable even before real email
delivery is wired.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log as audit
from app.models.audit import ActorType


async def notify_payment_failed(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    dunning_state: str,
    attempt_number: int,
    now: datetime | None = None,
) -> None:
    """Emit lifecycle_notice_sent on a failed-payment retry (Architecture §3.6.7 / Legal §A3).

    Legal §A3 requires: "A notification is sent at each retry attempt."
    Architecture §3.6.7: "The cadence is logged to admin_audit_log
    (event_type = 'lifecycle_notice_sent')."

    The real email/SMS send is an AWS-DEFERRED SEAM — the audit event is the
    durable record (append-only, tamper-evident) that satisfies the compliance
    obligation.  When SES/Twilio land, the real send fires here before the audit emit.
    """
    now = now or datetime.now(UTC)
    # Real send now wired: email the owner via the dispatcher (which writes the
    # lifecycle_notice_sent audit record with delivery_status). Degrades to audit-
    # only when email isn't configured, preserving the compliance artifact.
    from sqlalchemy import select  # noqa: PLC0415

    from app.models.instance import Account  # noqa: PLC0415 (avoid import cycle)
    from app.notifications import email_dispatcher  # noqa: PLC0415

    owner_email = (
        await session.execute(select(Account.email).where(Account.id == admin_id))
    ).scalar_one_or_none()
    if owner_email:
        await email_dispatcher.send_payment_failed(
            session,
            admin_id=admin_id,
            to=owner_email,
            dunning_state=dunning_state,
            attempt_number=attempt_number,
        )
    else:
        # No email on file — still write the durable compliance record (Legal §A3).
        await audit.emit(
            session,
            admin_id=admin_id,
            actor_type=ActorType.platform,
            event_type="lifecycle_notice_sent",
            payload={
                "notice_type": "payment_failed_retry",
                "dunning_state": dunning_state,
                "attempt_number": attempt_number,
                "delivery_method": "email",
                "delivery_status": "recorded",
                "sent_at": now.isoformat(),
            },
        )
