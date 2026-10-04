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
    #: The exception CLASS name behind a ``failed`` status (never the message: a
    #: provider's text can carry the recipient or the sender identity). Lets a caller
    #: say "failed:<reason>" on the wire instead of a bare "failed" (round 7 WP-5, item 4).
    reason: str | None = None

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
        message_id = await sender.send(
            to=to,
            subject=subject,
            body_text=body,
            # Round 5B item 13: bounce attribution rides every platform send.
            tags={"vm_admin_id": str(admin_id), "vm_purpose": notice_type},
        )
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
) -> DeliveryOutcome:
    """Email the data-export download link (§5.10). Delivery-ONLY: the caller (export
    flow) already emits the ``data_export_ready`` audit event, so this must NOT emit a
    second one — it just sends the email.

    Returns the outcome rather than a bare message id (round 7 WP-5, item 4): the
    export response tells the owner whether the link actually went out, so a mail
    failure is "failed:<reason>" on the wire and never a silent 200. A failure never
    breaks the export flow — the link is in the response body regardless."""
    body = (
        "Your VantageMind data export is ready.\n\n"
        f"Download it here (link valid for {ttl_days} days):\n\n{download_url}\n\n"
        "For your security the link expires; request a new export if it lapses."
    )
    sender = sender or build_email_sender()
    try:
        message_id = await sender.send(to=to, subject="Your data export is ready", body_text=body)
    except Exception as exc:  # noqa: BLE001 — delivery failure never breaks the export flow
        logging.getLogger(__name__).exception(
            "email delivery failed: notice_type=export_ready recipient=%s error=%s", to, exc
        )
        return DeliveryOutcome(status="failed", reason=type(exc).__name__)
    status: DeliveryStatus = "sent" if not isinstance(sender, NullEmailSender) else "recorded"
    return DeliveryOutcome(status=status, message_id=message_id)


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


def _affected_sources(source_names: list[str], cap: int = 10) -> str:
    shown = ", ".join(source_names[:cap])
    more = len(source_names) - cap
    tail = f", and {more} more" if more > 0 else ""
    noun = "source" if len(source_names) == 1 else "sources"
    return f"Affected {noun} ({len(source_names)}): {shown}{tail}."


async def send_knowledge_pause_warning(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    to: str,
    provider_name: str,
    source_names: list[str],
    stops_at: datetime,
    sender: EmailSender | None = None,
) -> DeliveryOutcome:
    """Round 7 WP-4c #5 (owner decision D10): a knowledge connection's saved sign-in
    was revoked or expired, so the sources synced from it are no longer being kept
    true. They keep answering for a fixed number of days and then stop; the owner
    hears this on day 1 (here) and again when it happens
    (``send_knowledge_pause_stopped``). Once per incident — the knowledge_sync sweep
    keys both on the notice_deliveries ledger."""
    when = stops_at.strftime("%d %B %Y")
    subject = f"Action needed: your {provider_name} knowledge will stop answering on {when}"
    body = (
        f"Luciel can no longer reach your {provider_name} connection: its saved sign-in "
        "was revoked or expired, so the knowledge synced from it is no longer being kept "
        f"up to date.\n\n{_affected_sources(source_names)}\n\n"
        f"These sources will keep answering until {when}. After that Luciel stops using "
        "them until you reconnect, so nobody is answered from content that may have gone "
        "stale.\n\n"
        "To fix it, open the Configure page of your VantageMind dashboard and use "
        "Reconnect on that connection — one sign-in, and the next sync switches "
        "everything back on by itself."
    )
    return await _dispatch(
        session,
        admin_id=admin_id,
        to=to,
        subject=subject,
        body=body,
        notice_type="knowledge_pause_warning",
        audit_event="knowledge_pause_notice_emailed",
        extra_payload={
            "provider": provider_name,
            "sources": len(source_names),
            "stops_at": stops_at.isoformat(),
        },
        sender=sender,
    )


async def send_knowledge_pause_stopped(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    to: str,
    provider_name: str,
    source_names: list[str],
    stopped_at: datetime,
    grace_days: int,
    sender: EmailSender | None = None,
) -> DeliveryOutcome:
    """The day-7 half of D10 (see ``send_knowledge_pause_warning``): the connection
    was not reconnected in time and its sources have now stopped answering."""
    when = stopped_at.strftime("%d %B %Y")
    subject = f"Your {provider_name} knowledge has stopped answering — reconnect to restore it"
    body = (
        f"Your {provider_name} connection was not reconnected within {grace_days} days of "
        f"its saved sign-in being revoked or expired, so as of {when} Luciel no longer "
        "answers from the knowledge synced from it. Questions those sources used to "
        "answer are now handed to your team instead of being guessed.\n\n"
        f"{_affected_sources(source_names)}\n\n"
        "To restore them, open the Configure page of your VantageMind dashboard and use "
        "Reconnect on that connection — the next sync brings every source back."
    )
    return await _dispatch(
        session,
        admin_id=admin_id,
        to=to,
        subject=subject,
        body=body,
        notice_type="knowledge_pause_stopped",
        audit_event="knowledge_pause_notice_emailed",
        extra_payload={
            "provider": provider_name,
            "sources": len(source_names),
            "stopped_at": stopped_at.isoformat(),
        },
        sender=sender,
    )


async def send_crm_push_failed(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    to: str,
    lead_label: str,
    detail: str,
    sender: EmailSender | None = None,
) -> DeliveryOutcome:
    """2026-09-05 audit F134: a lead did not reach the tenant's CRM — say so, once.

    Sent the first time a push for a given lead FAILS at the CRM (refused, HTTP
    error, unreachable) — never for a broker refusal the owner chose (tool off).
    The lead is safe in the dashboard; the Leads page shows the same state and
    offers the retry."""
    subject = "A lead did not reach your CRM"
    body = (
        f"Luciel captured {lead_label}, but could not push the record to your CRM "
        f"({detail}). The lead is safe in your VantageMind dashboard.\n\n"
        "Open the Leads page to see the details and use “Retry CRM push” once your "
        "CRM connection is healthy. Luciel also retries on its own the next time it "
        "learns something new about this person.\n\n"
        "You will not be emailed again about this lead."
    )
    return await _dispatch(
        session,
        admin_id=admin_id,
        to=to,
        subject=subject,
        body=body,
        notice_type="crm_push_failed",
        audit_event="crm_push_failure_emailed",
        extra_payload={"detail": detail[:160]},
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


async def send_payg_block_started(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    to: str,
    notice_id: str,
    block_number: int,
    conversations_this_period: int,
    sender: EmailSender | None = None,
) -> DeliveryOutcome:
    """A new pay-as-you-go block has started (§3.4.1b block notification).

    Informational: the block is billed in arrears on the next invoice, Luciel keeps
    answering, nothing changes automatically. Sent once per block per billing period
    (``paygblock:<period_start>:<block>``)."""
    settings = get_settings()
    price_dollars = settings.payg_price_per_block_cents // 100
    ordinal = {1: "first", 2: "second", 3: "third"}.get(block_number, f"{block_number}th")
    body = (
        "A quick update from VantageMind.\n\n"
        f"Luciel has handled {conversations_this_period} conversations so far this billing "
        f"period, which is past your {settings.free_allowance} free conversations and into "
        f"your {ordinal} pay-as-you-go block of {settings.payg_block_size} conversations "
        f"(${price_dollars}).\n\n"
        "This block will appear on your next invoice. Nothing changes automatically and "
        "Luciel keeps answering your customers as usual. You can review your usage and "
        "billing setup any time from the Billing page of your dashboard."
    )
    return await _dispatch(
        session,
        admin_id=admin_id,
        to=to,
        subject=f"Your {ordinal} pay-as-you-go block has started",
        body=body,
        notice_type="payg_block_started",
        audit_event="billing_notice_sent",
        extra_payload={
            "notice_id": notice_id,
            "kind": "payg_block_started",
            "block_number": block_number,
            "conversations_this_period": conversations_this_period,
        },
        sender=sender,
    )


async def send_escalation_contact_confirm(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    to: str,
    confirm_url: str,
    sender: EmailSender | None = None,
) -> DeliveryOutcome:
    """Escalation-contact confirmation link (round 5B item 13).

    Hot-lead notifications go to this address; until it is confirmed the owner's
    account email is copied on every escalation, so a typo cannot black-hole a
    lead. Clicking proves the mailbox both delivers and is wanted."""
    body = (
        "This address is set as an escalation contact for a VantageMind Luciel — the "
        "AI assistant will send its hand-off alerts (a customer asking for a person, "
        "a high-value lead) here.\n\n"
        f"Please confirm you can receive these: {confirm_url}\n\n"
        "Until confirmed, alerts are also copied to the account owner's email. If you "
        "didn't expect this, you can ignore it — nothing else changes."
    )
    return await _dispatch(
        session,
        admin_id=admin_id,
        to=to,
        subject="Confirm this escalation contact for your Luciel",
        body=body,
        notice_type="escalation_contact_confirm",
        audit_event="escalation_contact_verification_sent",
        extra_payload={"recipient": to},
        sender=sender,
    )


async def send_deferred_abandon_notice(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    to: str,
    action_type: str,
    conversation_ref: str | None,
    sender: EmailSender | None = None,
) -> DeliveryOutcome:
    """Owner email for an ABANDONED deferred item (§3.8.7 rule D — round 5B #13).

    The abandon used to be audit-only (flagged in the queue's own docstring):
    "abandoned WITH an owner notification" was a promise the audit page kept and
    the inbox did not. Escalation retries get the hot-lead wording."""
    # The queue files escalation retries under ``escalation_notification`` (the
    # dispatch module's constant); the legacy ``escalation_retry`` spelling is kept so
    # an item queued under it still gets the hot-lead wording (2026-09-05 audit).
    if action_type in ("escalation_retry", "escalation_notification"):
        ref = f" (conversation #{conversation_ref})" if conversation_ref else ""
        body = (
            "VantageMind could not deliver an escalation alert after retrying for 24 "
            f"hours, and has stopped retrying{ref}.\n\n"
            "You may have missed a hot lead — please open the conversation in your "
            "dashboard and check your escalation contact on the Configure page."
        )
        subject = "A Luciel escalation alert could not be delivered"
    else:
        body = (
            "Luciel queued a follow-up action that could not be completed after "
            "retrying for 24 hours, and has stopped retrying.\n\n"
            "The details are in your dashboard's audit log; the related connection "
            "may need attention on the Configure page."
        )
        subject = "A queued Luciel follow-up was abandoned"
    return await _dispatch(
        session,
        admin_id=admin_id,
        to=to,
        subject=subject,
        body=body,
        notice_type="deferred_abandon",
        audit_event="deferred_abandon_emailed",
        extra_payload={"action_type": action_type},
        sender=sender,
    )


async def send_card_expiry_notice(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    to: str,
    notice_id: str,
    exp_month: int,
    exp_year: int,
    last4: str | None,
    sender: EmailSender | None = None,
) -> DeliveryOutcome:
    """Proactive card-expiry heads-up (round 5B item 17).

    The expiry was stored and displayed but never compared to the clock — the
    first DECLINED charge was the detector, which put the account into dunning
    over a fully predictable event. Sent once per (card, expiry month) by the
    daily billing_notices sweep; nothing changes automatically."""
    card = f"card ending {last4}" if last4 else "payment card"
    body = (
        "A quick heads-up from VantageMind.\n\n"
        f"The {card} on your account expires {exp_month:02d}/{exp_year}. Updating it "
        "before then keeps pay-as-you-go billing uninterrupted — an expired card's "
        "first failed charge would start the normal 7-day retry window.\n\n"
        "You can update your payment method any time from the Billing page of your "
        "dashboard. Nothing changes automatically."
    )
    return await _dispatch(
        session,
        admin_id=admin_id,
        to=to,
        subject="Your payment card is expiring soon",
        body=body,
        notice_type="card_expiry_notice",
        audit_event="billing_notice_sent",
        extra_payload={"notice_id": notice_id, "kind": "card_expiry_notice"},
        sender=sender,
    )


#: ``notice_type`` of the day-7 reduction notice (Legal §A3 / §3.6.7) — distinct from
#: the per-retry ``payment_failed_retry`` so the audit trail says which one went out.
DUNNING_REDUCED_NOTICE = "dunning_reduced"
PAYMENT_FAILED_RETRY_NOTICE = "payment_failed_retry"
PAYMENT_METHOD_REMOVED_NOTICE = "payment_method_removed"

# What the owner keeps in every billing state (owner decision D1, 2026-09-14): their
# own mailbox and Twilio are theirs, and a conversation in flight is never cut off
# (Legal §A3 #3). Said in every dunning / removal notice so the consequence is exact.
_KEEPS = (
    "Your own email and SMS channels keep working, and a conversation already in "
    "progress is never cut off."
)
_BILLING_PAGE = "You can add or update your card any time from the Billing page of your dashboard."


def _dunning_body(dunning_state: str, attempt_number: int | None) -> tuple[str, str]:
    """(subject, body) for the dunning state the account is in RIGHT NOW.

    One fixed body used to go out regardless of state (round 7 WP-1 #3b), so the day-7
    reduction notice read like retry number one. ``retrying`` says what the schedule is
    and what happens when it runs out; ``reduced`` says what has already happened.
    """
    if dunning_state == "reduced":
        subject = "Your VantageMind account is now on the free allowance"
        body = (
            "We could not collect your VantageMind payment after retrying on days 1, 3, 5 "
            "and 7.\n\n"
            "Your account is now on the free allowance: your Luciel answers the first 50 "
            "conversations of each period, and action tools (calendar, CRM, record "
            "lookups, webhooks) are paused until a valid card is added. "
            f"{_KEEPS}\n\n"
            f"{_BILLING_PAGE}"
        )
        return subject, body
    attempt = f"This was retry attempt {attempt_number}. " if attempt_number is not None else ""
    subject = "Action needed: payment issue on your VantageMind account"
    body = (
        "We couldn't process your latest VantageMind payment.\n\n"
        f"{attempt}We will retry automatically on days 1, 3, 5 and 7 after the first "
        "failure. After that your Luciel answers only the free 50 conversations for the "
        "period and action tools (calendar, CRM, record lookups, webhooks) pause until a "
        f"valid card is added. {_KEEPS}\n\n"
        f"{_BILLING_PAGE}"
    )
    return subject, body


async def send_payment_failed(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    to: str,
    dunning_state: str,
    attempt_number: int | None,
    sender: EmailSender | None = None,
) -> DeliveryOutcome:
    """Dunning notice (Legal §A3 / §3.6.7): one at each retry, and the final one at the
    day-7 reduction. The body states the consequence for the state the account is in
    (see :func:`_dunning_body`); ``attempt_number`` is None when the notice is the
    schedule-driven reduction (``dunning.advance_if_due``) rather than a Stripe retry."""
    subject, body = _dunning_body(dunning_state, attempt_number)
    notice_type = (
        DUNNING_REDUCED_NOTICE if dunning_state == "reduced" else PAYMENT_FAILED_RETRY_NOTICE
    )
    return await _dispatch(
        session,
        admin_id=admin_id,
        to=to,
        subject=subject,
        body=body,
        notice_type=notice_type,
        audit_event="lifecycle_notice_sent",
        extra_payload={"dunning_state": dunning_state, "attempt_number": attempt_number},
        sender=sender,
    )


def payment_method_removed_text(reason: str) -> str:
    """The one-paragraph consequence of a card removal (D1) — shared by the email body
    and the SMS leg so both say the same thing."""
    how = (
        "was removed from your Stripe customer"
        if reason == "stripe_detached"
        else "was removed from your account"
    )
    return (
        f"The payment card on your VantageMind account {how}. Luciel keeps answering: "
        "after this period's free 50 conversations, new conversations wait until a card "
        f"is added. Nothing else changes. {_KEEPS}"
    )


async def send_payment_method_removed(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    to: str,
    reason: str,
    sender: EmailSender | None = None,
) -> DeliveryOutcome:
    """Card-removed notice (round 7 WP-1 #3c; Legal §A3 / §3.6.7).

    Neither removal path — the admin's DELETE nor Stripe's payment_method.detached —
    told the owner anything; a detach the owner did not initiate (an issuer pulling
    the card) was silent until the cap bit. ``reason`` is the same value the
    ``payment_method_removed`` audit event carries."""
    body = f"{payment_method_removed_text(reason)}\n\n{_BILLING_PAGE}"
    return await _dispatch(
        session,
        admin_id=admin_id,
        to=to,
        subject="The payment card on your VantageMind account was removed",
        body=body,
        notice_type=PAYMENT_METHOD_REMOVED_NOTICE,
        audit_event="lifecycle_notice_sent",
        extra_payload={"reason": reason},
        sender=sender,
    )


async def send_daily_brief(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    to: str,
    subject: str,
    body: str,
    notice_id: str,
    sender: EmailSender | None = None,
) -> DeliveryOutcome:
    """The owner's morning brief (round 6 WP-F) — rendered by app.employee.brief from
    rows that already exist; sent once per local day (the sweep keys it
    ``brief:<date>`` in the notice ledger)."""
    return await _dispatch(
        session,
        admin_id=admin_id,
        to=to,
        subject=subject,
        body=body,
        notice_type="daily_brief",
        audit_event="daily_brief_sent",
        extra_payload={"notice_id": notice_id, "kind": "daily_brief"},
        sender=sender,
    )
