"""Email delivery-incident recording + escalation-contact verification (5B #13).

The service half over ``models.email_incident``: the SES events webhook records
bounces/complaints here, the escalation dispatcher reads suppression state, and
the contact-verification loop mints/consumes its single-use confirmations.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log as audit
from app.db.session import tenant_session
from app.models.audit import ActorType
from app.models.email_incident import EmailDeliveryIncident


async def _row_for(
    session: AsyncSession, admin_id: uuid.UUID, email: str
) -> EmailDeliveryIncident | None:
    return (
        await session.execute(
            select(EmailDeliveryIncident).where(
                EmailDeliveryIncident.admin_id == admin_id,
                EmailDeliveryIncident.email_lower == email.strip().lower(),
            )
        )
    ).scalar_one_or_none()


async def _upsert_row(
    session: AsyncSession, admin_id: uuid.UUID, email: str
) -> EmailDeliveryIncident:
    row = await _row_for(session, admin_id, email)
    if row is None:
        row = EmailDeliveryIncident(admin_id=admin_id, email_lower=email.strip().lower())
        session.add(row)
        await session.flush()
    return row


async def record_ses_event(
    *, admin_id: uuid.UUID, event_type: str, event: dict[str, Any], purpose: str
) -> bool:
    """Record one SES Bounce/Complaint notification for its tenant.

    Opens its own tenant session (the caller is a webhook with none). Permanent
    bounces and complaints suppress the address (until re-verified); Transient
    bounces are counted but non-gating — SES retries those itself. Returns True
    when at least one recipient row was recorded."""
    now = datetime.now(UTC)
    if event_type == "Bounce":
        section = event.get("bounce") or {}
        bounce_type = str(section.get("bounceType") or "")
        sub_type = str(section.get("bounceSubType") or "")
        recipients = [
            str(r.get("emailAddress") or "") for r in (section.get("bouncedRecipients") or [])
        ]
        detail = f"bounce:{bounce_type}:{sub_type}"[:200]
    else:  # Complaint
        section = event.get("complaint") or {}
        bounce_type = ""
        recipients = [
            str(r.get("emailAddress") or "") for r in (section.get("complainedRecipients") or [])
        ]
        detail = f"complaint:{section.get('complaintFeedbackType') or 'unspecified'}"[:200]

    recipients = [r for r in recipients if r and "@" in r]
    if not recipients:
        return False
    async with tenant_session(admin_id) as s:
        for address in recipients:
            row = await _upsert_row(s, admin_id, address)
            if event_type == "Complaint":
                row.complained_at = now
            elif bounce_type == "Permanent":
                row.hard_bounced_at = now
            else:
                row.soft_bounce_count += 1
            row.last_event_at = now
            row.last_event_detail = detail
            await audit.emit(
                s,
                admin_id=admin_id,
                actor_type=ActorType.platform,
                event_type="email_contact_bounced",
                payload={
                    # The recipient is the admin's OWN configured contact — same
                    # privacy posture as escalation_delivery_failed rows.
                    "recipient": address,
                    "event": detail,
                    "purpose": purpose,
                },
            )
        await s.flush()
    return True


async def states_for(
    session: AsyncSession, admin_id: uuid.UUID, addresses: list[str]
) -> dict[str, EmailDeliveryIncident]:
    """Incident/verification rows for these addresses, keyed lower-case."""
    wanted = [a.strip().lower() for a in addresses if a]
    if not wanted:
        return {}
    rows = (
        (
            await session.execute(
                select(EmailDeliveryIncident).where(
                    EmailDeliveryIncident.admin_id == admin_id,
                    EmailDeliveryIncident.email_lower.in_(wanted),
                )
            )
        )
        .scalars()
        .all()
    )
    return {r.email_lower: r for r in rows}


async def begin_verification(
    session: AsyncSession, *, admin_id: uuid.UUID, email: str, jti: str, now: datetime | None = None
) -> EmailDeliveryIncident:
    """Stash the confirmation link's single-use jti on the address's row."""
    row = await _upsert_row(session, admin_id, email)
    row.verification_pending_jti = jti
    row.verification_sent_at = now or datetime.now(UTC)
    await session.flush()
    return row


async def confirm_verification(
    session: AsyncSession, *, admin_id: uuid.UUID, jti: str
) -> EmailDeliveryIncident | None:
    """Consume a pending jti → stamp verified_at. None when nothing matches
    (expired-and-reissued, already consumed, or address since replaced)."""
    row = (
        await session.execute(
            select(EmailDeliveryIncident).where(
                EmailDeliveryIncident.admin_id == admin_id,
                EmailDeliveryIncident.verification_pending_jti == jti,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    row.verification_pending_jti = None
    row.verified_at = datetime.now(UTC)
    await session.flush()
    return row
