"""Notification dispatch (Architecture §3.6.7 / §3.5 / §5.10 / §3.4.5).

Renders the doc-mandated customer/admin notifications and delivers them via the
injected EmailSender, THEN writes the durable audit record. Delivery may degrade to
NullEmailSender (mock/CI or unconfigured live) — the audit event is still written, so
the compliance artifact (cadence, recipient, purpose) is never lost. This is the real
send that the notifier's AWS-DEFERRED seam described.

All sends go through ``_dispatch`` so every notification is uniformly audited with a
``delivery_status`` of ``sent`` (real message id) or ``recorded`` (Null fallback).

A delivery error must never break the calling flow (signup still creates the account),
but it must not be invisible either: ``_dispatch`` returns a ``DeliveryOutcome`` whose
``degraded`` flag lets the caller tell the client that the mail did not leave the
system, without leaking the provider error.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log as audit
from app.config import get_settings
from app.models.audit import ActorType
from app.notifications.email_sender import EmailSender, NullEmailSender, build_email_sender

DeliveryStatus = Literal["sent", "recorded", "failed"]


@dataclass(frozen=True)
class DeliveryOutcome:
    """The result of one notification dispatch.

    ``degraded`` is True whenever the platform intended to put mail on the wire and
    it did not get there — a hard send failure in any mode, or a live deployment that
    silently fell back to ``NullEmailSender`` because no sender identity is bound. In
    mock/CI the Null fallback is the expected posture and is NOT degraded.
    """

    status: DeliveryStatus
    message_id: str | None = None

    @property
    def degraded(self) -> bool:
        if self.status == "failed":
            return True
        return self.status != "sent" and get_settings().integration_mode == "live"


async def _dispatch(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    to: str,
    subject: str,
    body: str,
    notice_type: str,
    audit_event: str,
    extra_payload: dict[str, object] | None = None,
    sender: EmailSender | None = None,
) -> DeliveryOutcome:
    sender = sender or build_email_sender()
    message_id: str | None = None
    delivery_status: DeliveryStatus = "recorded"
    try:
        message_id = await sender.send(to=to, subject=subject, body_text=body)
        delivery_status = "sent" if not isinstance(sender, NullEmailSender) else "recorded"
    except Exception as exc:  # noqa: BLE001 — never let a delivery error break the flow
        delivery_status = "failed"
        extra_payload = {**(extra_payload or {}), "delivery_error": type(exc).__name__}
        # Observability: a swallowed delivery error was previously invisible in logs
        # (only the exception CLASS was written to the audit payload). Log the full
        # error so email-delivery failures are diagnosable in CloudWatch.
        logging.getLogger(__name__).exception(
            "email delivery failed: notice_type=%s recipient=%s error=%s",
            notice_type,
            to,
            exc,
        )

    outcome = DeliveryOutcome(status=delivery_status, message_id=message_id)
    await audit.emit(
        session,
        admin_id=admin_id,
        actor_type=ActorType.platform,
        event_type=audit_event,
        payload={
            "notice_type": notice_type,
            "recipient": to,
            "delivery_method": "email",
            "delivery_status": delivery_status,
            "delivery_degraded": outcome.degraded,
            "message_id": message_id,
            "sent_at": datetime.now(UTC).isoformat(),
            **(extra_payload or {}),
        },
    )
    return outcome


# --- concrete notifications ------------------------------------------------------- #


async def send_verify_email(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    to: str,
    verify_url: str,
    sender: EmailSender | None = None,
) -> DeliveryOutcome:
    """Signup verification link (CJ Phase 2). The link is emailed, never returned."""
    body = (
        "Welcome to VantageMind.\n\n"
        "Confirm your email to activate your account:\n\n"
        f"{verify_url}\n\n"
        "This link expires in 24 hours. If you didn't sign up, ignore this email."
    )
    return await _dispatch(
        session,
        admin_id=admin_id,
        to=to,
        subject="Confirm your VantageMind email",
        body=body,
        notice_type="verify_email",
        audit_event="verification_resent",
        sender=sender,
    )


async def send_reset_email(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    to: str,
    reset_url: str,
    sender: EmailSender | None = None,
) -> DeliveryOutcome:
    """Password-reset link (CJ Phase 2). The link is emailed, never returned."""
    body = (
        "We received a request to reset your VantageMind password.\n\n"
        "Choose a new password here:\n\n"
        f"{reset_url}\n\n"
        "This link expires in 1 hour and can only be used once. If you didn't ask "
        "for a reset, ignore this email — your password is unchanged."
    )
    return await _dispatch(
        session,
        admin_id=admin_id,
        to=to,
        subject="Reset your VantageMind password",
        body=body,
        notice_type="password_reset",
        audit_event="password_reset_requested",
        sender=sender,
    )


async def send_export_ready(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    to: str,
    download_url: str,
    ttl_days: int,
    sender: EmailSender | None = None,
) -> str | None:
    """Email the data-export download link (§5.10). Delivery-ONLY: the caller (export
    flow) already emits the ``data_export_ready`` audit event with the link, so this
    must NOT emit a second one — it just sends the email."""
    body = (
        "Your VantageMind data export is ready.\n\n"
        f"Download it here (link valid for {ttl_days} days):\n\n{download_url}\n\n"
        "For your security the link expires; request a new export if it lapses."
    )
    sender = sender or build_email_sender()
    try:
        return await sender.send(to=to, subject="Your data export is ready", body_text=body)
    except Exception:  # noqa: BLE001 — delivery failure never breaks the export flow
        return None


async def send_account_notice(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    to: str,
    notice_id: str,
    kind: str,
    title: str,
    body: str,
    extra_payload: dict[str, object] | None = None,
    sender: EmailSender | None = None,
) -> DeliveryOutcome:
    """The EMAIL half of an account notice (Legal §A5 / §A10 / §B6).

    §A5 owes an allowance reduction notice "BOTH by email AND as an in-dashboard
    notification" — the dashboard half renders from ``app.billing.notices``; this
    sends the same copy by mail. Called once per (tenant, notice_id) by the
    ``billing_notices`` sweep, whose ``notice_deliveries`` ledger is what makes
    "once" true; this function just sends and audits.
    """
    full_body = (
        f"{body}\n\n"
        "You can review your usage and billing setup any time from the Billing page "
        "of your VantageMind dashboard, where this notice also appears."
    )
    return await _dispatch(
        session,
        admin_id=admin_id,
        to=to,
        subject=title,
        body=full_body,
        notice_type="account_notice",
        audit_event="billing_notice_sent",
        extra_payload={"notice_id": notice_id, "kind": kind, **(extra_payload or {})},
        sender=sender,
    )


async def send_connection_attention(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    to: str,
    provider_name: str,
    surface_label: str,
    status: str,
    detail: str | None,
    sender: EmailSender | None = None,
) -> DeliveryOutcome:
    """§3.8.5: a connection stopped working — tell the owner, with the fix.

    Sent by the health worker at the moment a row TRANSITIONS into
    expired/error (once per incident — a still-broken row on the next sweep is
    not a new transition and sends nothing). Until Phase 6.6 wired this, the
    owner's only signal was noticing the chip on their next dashboard visit."""
    subject = f"Action needed: reconnect {provider_name}"
    reason = (
        "its saved sign-in expired and could not be renewed"
        if status == "expired"
        else "its saved credentials stopped working"
    )
    body = (
        f"Your {provider_name} connection (your {surface_label}) needs attention: "
        f"{reason}. Luciel has stopped using it so nothing fails silently.\n\n"
        "To fix it, open the Configure page of your VantageMind dashboard and use "
        "Reconnect on that connection — one sign-in, and everything that depends "
        "on it switches back on by itself.\n\n"
        f"Detail on the row: {detail or 'reconnect needed'}"
    )
    return await _dispatch(
        session,
        admin_id=admin_id,
        to=to,
        subject=subject,
        body=body,
        notice_type="connection_attention",
        audit_event="connection_attention_emailed",
        extra_payload={"status": status},
        sender=sender,
    )


async def send_payg_usage_nudge(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    to: str,
    notice_id: str,
    conversations_this_period: int,
    sender: EmailSender | None = None,
) -> DeliveryOutcome:
    """The ~80%-through-a-block heads-up email (Vision §7 / §3.4.1b).

    A nudge, never a cap: nothing changes automatically and Luciel keeps
    answering. The dashboard half is ``nearNextBlock`` on the billing read; this
    is the email the docs promise alongside it, sent at most once per billing
    period (the sweep keys it ``payg80:<period_start>``).
    """
    settings = get_settings()
    price_dollars = settings.payg_price_per_block_cents // 100
    body = (
        "A quick heads-up from VantageMind.\n\n"
        f"Luciel has handled {conversations_this_period} conversations so far this "
        f"billing period, which puts you close to starting your next block of "
        f"{settings.payg_block_size} pay-as-you-go conversations (${price_dollars}).\n\n"
        "Nothing changes automatically and there is nothing you need to do — Luciel "
        "keeps answering your customers as usual. You can review your usage and "
        "billing setup any time from the Billing page of your dashboard."
    )
    return await _dispatch(
        session,
        admin_id=admin_id,
        to=to,
        subject="Heads-up: you're approaching your next pay-as-you-go block",
        body=body,
        notice_type="payg_usage_nudge",
        audit_event="billing_notice_sent",
        extra_payload={
            "notice_id": notice_id,
            "kind": "payg_usage_nudge",
            "conversations_this_period": conversations_this_period,
        },
        sender=sender,
    )


async def send_payment_failed(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    to: str,
    dunning_state: str,
    attempt_number: int,
    sender: EmailSender | None = None,
) -> DeliveryOutcome:
    """Dunning retry notice (Legal §A3 / §3.6.7 — a notice at each retry)."""
    body = (
        "We couldn't process your latest VantageMind payment.\n\n"
        f"This was retry attempt {attempt_number}. We'll try again automatically.\n"
        "Please check your payment method to avoid an interruption to your service."
    )
    return await _dispatch(
        session,
        admin_id=admin_id,
        to=to,
        subject="Action needed: payment issue on your VantageMind account",
        body=body,
        notice_type="payment_failed_retry",
        audit_event="lifecycle_notice_sent",
        extra_payload={"dunning_state": dunning_state, "attempt_number": attempt_number},
        sender=sender,
    )
