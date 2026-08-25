"""Billing notice email delivery (Legal §A5 / §A10 + the Vision §7 80% nudge).

§A5's binding sentence: reducing the free starter allowance owes every existing
account at least NOTICE_LEAD_DAYS (30) days' notice "BOTH by email AND as an
in-dashboard notification". The dashboard half has rendered from
``app.billing.notices`` since it shipped; the email half had no sender at all —
the commitment was half-met and silently so. This module is that missing half,
run by the daily ``billing_notices`` sweep for every verified account:

  * every active account notice (the derived §A5 allowance reduction and any
    operator-published §A10/§B6 notice) is emailed ONCE per (tenant, notice_id),
    with the ``notice_deliveries`` ledger making "once" durable across
    at-least-once schedule delivery;
  * the ~80%-through-a-block pay-as-you-go heads-up is emailed at most once per
    billing period (``payg80:<period_start>``) — the email twin of the
    dashboard's ``nearNextBlock`` flag.

Lead-time enforcement (§A5): a reduction announced with less than
NOTICE_LEAD_DAYS of lead left is a compliance defect the OPERATOR must fix by
moving the effective date — the platform cannot conjure the missing days. What
the platform CAN do is refuse to let it pass silently: the first delivery of a
short-lead reduction logs an ERROR — which, running in the sweep container,
feeds the sweep log group's ``SweepErrors`` metric filter and its alarm in
armed environments (compute-stack.ts / observability-stack.ts) — and stamps
``lead_days_below_minimum`` into the audit payload, so the violation is
provable instead of invisible. (Nothing in the codebase auto-applies the
reduction to metering — the cutover is an explicit operator settings change,
and the frozen billing wire pins the current allowance — so there is no
automatic application to suppress here.)

A FAILED email is deliberately not recorded in the ledger: tomorrow's sweep
retries it, and the failure itself is already in the audit log via
``_dispatch``. A ``recorded`` (Null-sender) outcome IS recorded — in mock/CI
that posture is the expected delivery path, and re-sending daily would be noise.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing import metering
from app.billing.notices import (
    ALLOWANCE_REDUCTION,
    NOTICE_LEAD_DAYS,
    AccountNotice,
    account_notices,
)
from app.models.instance import Account, Instance
from app.models.notice_delivery import NoticeDelivery
from app.notifications import email_dispatcher
from app.notifications.email_sender import EmailSender
from app.runtime.budget import period_start_for

logger = logging.getLogger(__name__)

PAYG_NUDGE_PREFIX = "payg80"
PAYG_NUDGE_KIND = "payg_usage_nudge"


def _lead_days(notice: AccountNotice, now: datetime) -> int | None:
    if notice.effective_at is None:
        return None
    return (notice.effective_at - now).days


async def deliver_due_notices(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    now: datetime,
    sender: EmailSender | None = None,
) -> int:
    """Send this tenant every billing notice email not yet delivered. Returns sends."""
    email = (
        await session.execute(select(Account.email).where(Account.id == admin_id))
    ).scalar_one_or_none()
    if not email:
        return 0

    delivered: set[str] = set(
        (
            await session.execute(
                select(NoticeDelivery.notice_id).where(NoticeDelivery.admin_id == admin_id)
            )
        ).scalars()
    )

    sent = 0
    for notice in account_notices(now=now):
        # Defensive clamp to the column width — operator-published noticeIds are
        # free-form strings, and one oversized id must not crash the whole sweep.
        notice_id = notice.notice_id[:200]
        if notice_id in delivered:
            continue

        lead_days = _lead_days(notice, now)
        extra: dict[str, object] = {}
        if lead_days is not None:
            extra["lead_days"] = lead_days
        if notice.kind == ALLOWANCE_REDUCTION and lead_days is not None:
            extra["lead_days_below_minimum"] = lead_days < NOTICE_LEAD_DAYS
            if lead_days < NOTICE_LEAD_DAYS:
                logger.error(
                    "allowance reduction %s gives only %d days' notice — Legal §A5 "
                    "requires at least %d; move free_allowance_reduction_effective_at",
                    notice_id,
                    lead_days,
                    NOTICE_LEAD_DAYS,
                )

        outcome = await email_dispatcher.send_account_notice(
            session,
            admin_id=admin_id,
            to=email,
            notice_id=notice_id,
            kind=notice.kind,
            title=notice.title,
            body=notice.body,
            extra_payload=extra,
            sender=sender,
        )
        if outcome.status == "failed":
            continue  # no ledger row — the next sweep retries; the audit row has the failure
        session.add(
            NoticeDelivery(
                admin_id=admin_id,
                notice_id=notice_id,
                kind=notice.kind,
                delivery_status=outcome.status,
                sent_at=now,
            )
        )
        delivered.add(notice_id)
        sent += 1

    sent += await _send_payg_nudge_if_due(
        session, admin_id=admin_id, email=email, delivered=delivered, now=now, sender=sender
    )
    sent += await _send_card_expiry_notice_if_due(
        session, admin_id=admin_id, email=email, delivered=delivered, now=now, sender=sender
    )
    return sent


# Round 5B item 17: how far ahead of the card's expiry month the heads-up goes.
CARD_EXPIRY_LEAD_DAYS = 45
CARD_EXPIRY_KIND = "card_expiry_notice"


async def _send_card_expiry_notice_if_due(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    email: str,
    delivered: set[str],
    now: datetime,
    sender: EmailSender | None,
) -> int:
    """Proactive card-expiry notice (round 5B item 17): the stored pm_exp_* was
    display-only and the first DECLINED charge was the detector. Once per
    (card expiry month) via the ledger — a replaced card changes the notice id."""
    from datetime import timedelta  # noqa: PLC0415

    account = await metering.get_or_create_billing_account(session, admin_id=admin_id)
    if not account.pm_last4 or not account.pm_exp_month or not account.pm_exp_year:
        return 0
    # The card dies at the END of its expiry month.
    if account.pm_exp_month == 12:
        expires_at = datetime(account.pm_exp_year + 1, 1, 1, tzinfo=now.tzinfo)
    else:
        expires_at = datetime(account.pm_exp_year, account.pm_exp_month + 1, 1, tzinfo=now.tzinfo)
    if now + timedelta(days=CARD_EXPIRY_LEAD_DAYS) < expires_at:
        return 0
    notice_id = f"card_expiry:{account.pm_last4}:{account.pm_exp_year}-{account.pm_exp_month:02d}"
    if notice_id in delivered:
        return 0
    outcome = await email_dispatcher.send_card_expiry_notice(
        session,
        admin_id=admin_id,
        to=email,
        notice_id=notice_id,
        exp_month=account.pm_exp_month,
        exp_year=account.pm_exp_year,
        last4=account.pm_last4,
        sender=sender,
    )
    if outcome.status == "failed":
        return 0
    session.add(
        NoticeDelivery(
            admin_id=admin_id,
            notice_id=notice_id,
            kind=CARD_EXPIRY_KIND,
            delivery_status=outcome.status,
            sent_at=now,
        )
    )
    return 1


async def _send_payg_nudge_if_due(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    email: str,
    delivered: set[str],
    now: datetime,
    sender: EmailSender | None,
) -> int:
    instance_id = (
        (await session.execute(select(Instance.id).where(Instance.admin_id == admin_id)))
        .scalars()
        .first()
    )
    if instance_id is None:
        return 0

    budget = await metering.build_budget(
        session, admin_id=admin_id, instance_id=instance_id, now=now
    )
    # near_next_block is already gated on a live card and usage above the free
    # allowance (metering.build_budget) — a free-cap tenant can never be nudged.
    if not budget.near_next_block:
        return 0

    account = await metering.get_or_create_billing_account(session, admin_id=admin_id)
    period = period_start_for(account, now)
    notice_id = f"{PAYG_NUDGE_PREFIX}:{period.isoformat()}"
    if notice_id in delivered:
        return 0

    outcome = await email_dispatcher.send_payg_usage_nudge(
        session,
        admin_id=admin_id,
        to=email,
        notice_id=notice_id,
        conversations_this_period=budget.conversations_this_period,
        sender=sender,
    )
    if outcome.status == "failed":
        return 0
    session.add(
        NoticeDelivery(
            admin_id=admin_id,
            notice_id=notice_id,
            kind=PAYG_NUDGE_KIND,
            delivery_status=outcome.status,
            sent_at=now,
        )
    )
    return 1
