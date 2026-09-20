"""Admin billing-lifecycle notices (Architecture §3.6.7 / §3.5 / Legal §A3).

The owner-facing side of the dunning and card lifecycle: each function renders and
sends the notice through the email dispatcher (SES in live mode, the Null sender
otherwise) and — for the card-removed notice — the owner's escalation SMS number when
one is on file, through their own Twilio (``sms_notifier``, exactly as escalation
delivery does). Every send writes the durable ``lifecycle_notice_sent`` audit record
with its delivery status, so the cadence is auditable even where delivery degraded
(Architecture §3.6.7: "The cadence is logged to admin_audit_log").

Nothing here raises into a billing transition: a notice that cannot leave the system
is recorded as such, never fabricated and never allowed to strand the state change.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log as audit
from app.models.audit import ActorType

_log = logging.getLogger(__name__)


async def _owner_email(session: AsyncSession, admin_id: uuid.UUID) -> str | None:
    from app.models.instance import Account  # noqa: PLC0415 (avoid import cycle)

    return (
        await session.execute(select(Account.email).where(Account.id == admin_id))
    ).scalar_one_or_none()


async def _record_only(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    notice_type: str,
    now: datetime,
    delivery_method: str = "email",
    extra: dict[str, object] | None = None,
) -> None:
    """No address on file — still write the durable compliance record (Legal §A3)."""
    await audit.emit(
        session,
        admin_id=admin_id,
        actor_type=ActorType.platform,
        event_type="lifecycle_notice_sent",
        payload={
            "notice_type": notice_type,
            "delivery_method": delivery_method,
            "delivery_status": "recorded",
            "sent_at": now.isoformat(),
            **(extra or {}),
        },
    )


async def notify_payment_failed(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    dunning_state: str,
    attempt_number: int | None,
    now: datetime | None = None,
) -> None:
    """Send the dunning notice for the state the account is in (Legal §A3 / §3.6.7).

    Legal §A3: "A notification is sent at each retry attempt." Architecture §3.6.7: "The
    cadence is logged to admin_audit_log (event_type = 'lifecycle_notice_sent')." The
    body branches on ``dunning_state`` (``email_dispatcher._dunning_body``) so the day-7
    reduction says what has happened, not "retry attempt 1".
    """
    now = now or datetime.now(UTC)
    from app.notifications import email_dispatcher  # noqa: PLC0415 (avoid import cycle)

    owner_email = await _owner_email(session, admin_id)
    if owner_email:
        await email_dispatcher.send_payment_failed(
            session,
            admin_id=admin_id,
            to=owner_email,
            dunning_state=dunning_state,
            attempt_number=attempt_number,
        )
        return
    notice_type = (
        email_dispatcher.DUNNING_REDUCED_NOTICE
        if dunning_state == "reduced"
        else email_dispatcher.PAYMENT_FAILED_RETRY_NOTICE
    )
    await _record_only(
        session,
        admin_id=admin_id,
        notice_type=notice_type,
        now=now,
        extra={"dunning_state": dunning_state, "attempt_number": attempt_number},
    )


async def notify_dunning_reduced(
    session: AsyncSession, *, admin_id: uuid.UUID, now: datetime | None = None
) -> None:
    """The FINAL reduction notice (round 7 WP-1 #3a): the schedule-driven day-7 drop to
    ``reduced`` (``dunning.advance_if_due``) used to happen silently — the webhook path
    sent a notice because a retry had just failed, the sweep path sent nothing. There is
    no Stripe attempt behind this one, so the body carries no attempt number."""
    await notify_payment_failed(
        session, admin_id=admin_id, dunning_state="reduced", attempt_number=None, now=now
    )


async def _escalation_sms_target(
    session: AsyncSession, admin_id: uuid.UUID
) -> tuple[uuid.UUID, str] | None:
    """(instance_id, E.164 number) when the owner has an escalation SMS contact on
    file — the same contact the hot-lead page goes to (§3.5.3 ``primary_sms``)."""
    from app.models.instance import Instance  # noqa: PLC0415 (avoid import cycle)
    from app.schemas_api.luciel import EscalationContactWire  # noqa: PLC0415

    row = (
        await session.execute(
            select(Instance.id, Instance.escalation).where(Instance.admin_id == admin_id)
        )
    ).first()
    if row is None:
        return None
    instance_id, escalation = row
    try:
        cfg = EscalationContactWire.model_validate(dict(escalation or {}))
    except Exception:  # noqa: BLE001 — a malformed stored config must not block the notice
        _log.warning("escalation config unreadable for SMS notice admin_id=%s", admin_id)
        return None
    number = cfg.primary_sms or cfg.secondary_sms
    return (instance_id, number) if number else None


async def notify_payment_method_removed(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    reason: str,
    now: datetime | None = None,
) -> None:
    """Card-removed notice (round 7 WP-1 #3c) from BOTH removal paths — the admin's
    DELETE (``reason="admin_removed"``) and Stripe's ``payment_method.detached``
    (``reason="stripe_detached"``). Email to the account address; ALSO a text to the
    escalation SMS number when one is on file, through the owner's own Twilio the way
    escalation delivery pages them. Each leg is audited as ``lifecycle_notice_sent``
    with its own ``delivery_method`` / ``delivery_status`` (sent | recorded | failed) —
    a text that could not leave the system is a failed record, never a fabricated one.
    """
    now = now or datetime.now(UTC)
    from app.config import get_settings  # noqa: PLC0415
    from app.notifications import email_dispatcher  # noqa: PLC0415 (avoid import cycle)
    from app.notifications.sms_notifier import SmsNotifier  # noqa: PLC0415

    notice_type = email_dispatcher.PAYMENT_METHOD_REMOVED_NOTICE
    owner_email = await _owner_email(session, admin_id)
    if owner_email:
        await email_dispatcher.send_payment_method_removed(
            session, admin_id=admin_id, to=owner_email, reason=reason
        )
    else:
        await _record_only(
            session, admin_id=admin_id, notice_type=notice_type, now=now, extra={"reason": reason}
        )

    target = await _escalation_sms_target(session, admin_id)
    if target is None:
        return
    instance_id, number = target
    base = get_settings().app_base_url.rstrip("/")
    body = f"VantageMind: {email_dispatcher.payment_method_removed_text(reason)}"
    if base:
        body = f"{body} Update it: {base}/dashboard/billing"
    notifier = SmsNotifier()
    detail: str | None
    if not notifier.verify_config(number):
        receipt_status, detail, sid = "failed", "malformed_contact", None
    else:
        receipt = await notifier.send_text(
            session, admin_id=admin_id, instance_id=instance_id, to=number, body=body
        )
        if not receipt.accepted:
            receipt_status, detail, sid = "failed", receipt.detail, None
        elif receipt.detail == "recorded":
            receipt_status, detail, sid = "recorded", None, receipt.provider_message_id
        else:
            receipt_status, detail, sid = "sent", None, receipt.provider_message_id
    if receipt_status == "failed":
        _log.warning(
            "card-removed SMS notice not delivered admin_id=%s reason=%s", admin_id, detail
        )
    await audit.emit(
        session,
        admin_id=admin_id,
        instance_id=instance_id,
        actor_type=ActorType.platform,
        event_type="lifecycle_notice_sent",
        payload={
            "notice_type": notice_type,
            "recipient": number,
            "delivery_method": "sms",
            "delivery_status": receipt_status,
            "delivery_degraded": receipt_status == "failed",
            "message_id": sid,
            "failure_reason": detail,
            "reason": reason,
            "sent_at": now.isoformat(),
        },
    )
