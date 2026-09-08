"""The morning brief (round 6 WP-F): "what I did yesterday, what I need today".

Sent once per tenant per LOCAL day, at or after ``BRIEF_HOUR`` in the owner's
timezone (UTC when unset), by the hourly ``daily_brief`` sweep. Idempotency is the
``notice_deliveries`` ledger (``brief:<date>``), never the schedule — a retried or
overlapping sweep cannot double-send. ``daily_brief_enabled`` is the opt-out. A quiet
day still gets a (short) brief, because "nothing happened" is information too.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.employee.status import (
    CHANNEL_LABELS,
    EmployeeStatus,
    build_employee_status,
    local_day_bounds,
)
from app.models.deferred import DeferredAction
from app.models.instance import Account, Instance, LucielState
from app.models.notice_delivery import NoticeDelivery
from app.notifications import email_dispatcher
from app.notifications.email_sender import EmailSender

BRIEF_HOUR = 7
BRIEF_KIND = "daily_brief"


def brief_notice_id(local_date: date) -> str:
    return f"brief:{local_date.isoformat()}"


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def render_daily_brief(
    status: EmployeeStatus, *, local_date: date, due_callbacks_today: int
) -> tuple[str, str]:
    """(subject, body) — counts and links, never narration."""
    business = status.business_short_name or "your business"
    y = status.yesterday
    name = status.assistant_name
    if y.quiet:
        subject = f"A quiet day at {business} — {name}'s brief"
    else:
        subject = (
            f"Yesterday at {business}: {_plural(y.conversations, 'conversation')}, "
            f"{_plural(y.leads, 'lead')} — {name}'s brief"
        )
    lines: list[str] = [
        f"Good morning. Here is {name}'s brief for {local_date.strftime('%A %d %b')}.",
        "",
        "Yesterday",
    ]
    if y.quiet:
        lines.append("- A quiet day: no conversations, leads, hand-offs or bookings.")
    else:
        if y.conversations:
            mix = ", ".join(
                f"{n} on {CHANNEL_LABELS.get(ch, ch).lower()}"
                for ch, n in sorted(y.by_channel.items(), key=lambda kv: -kv[1])
            )
            lines.append(f"- {_plural(y.conversations, 'conversation')} ({mix}).")
        else:
            lines.append("- No conversations.")
        if y.leads:
            crm = f", {y.leads_to_crm} sent to your CRM" if y.leads_to_crm else ""
            lines.append(f"- {_plural(y.leads, 'lead')} captured{crm}.")
        if y.escalations:
            lines.append(
                f"- {_plural(y.escalations, 'hand-off')} to the team; "
                f"{y.escalations_reached} reached a configured contact."
            )
        if y.bookings:
            lines.append(f"- {_plural(y.bookings, 'appointment')} booked.")
        if y.callbacks_scheduled:
            lines.append(f"- {_plural(y.callbacks_scheduled, 'follow-up')} scheduled.")
        if y.human_takeovers:
            lines.append(f"- {_plural(y.human_takeovers, 'conversation')} taken over by a person.")
    lines += [
        "",
        "This billing period",
        f"- {status.conversations_used} of {status.free_allowance} free conversations used"
        + (f"; {_plural(status.billed_blocks, 'paid block')}." if status.billed_blocks else "."),
    ]
    attention = [n for n in status.needs if n.severity == "attention"][:3]
    lines += ["", "What I need from you"]
    if attention:
        lines += [f"- {n.title}. {n.detail}" for n in attention]
    else:
        lines.append("- Nothing right now.")
    lines += ["", "Today"]
    if due_callbacks_today:
        lines.append(f"- {_plural(due_callbacks_today, 'follow-up')} due to go out today.")
    else:
        lines.append("- No follow-ups scheduled for today.")
    lines += [
        "",
        f"{name} is on duty {status.on_duty}. You can turn this brief off from your dashboard.",
    ]
    return subject, "\n".join(lines)


async def send_daily_brief_if_due(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    now: datetime | None = None,
    sender: EmailSender | None = None,
) -> int:
    """Send this tenant's brief for the current LOCAL day if it is time and it has not
    gone out. Returns the number of briefs sent (0 or 1)."""
    moment = now or datetime.now(UTC)
    inst = (
        await session.execute(
            select(Instance).where(
                Instance.admin_id == admin_id,
                Instance.state.in_((LucielState.active, LucielState.paused)),
            )
        )
    ).scalar_one_or_none()
    if inst is None or not inst.daily_brief_enabled:
        return 0
    yesterday_start, today_start, local_now = local_day_bounds(inst, moment)
    if local_now.hour < BRIEF_HOUR:
        return 0
    notice_id = brief_notice_id(local_now.date())
    already = (
        await session.execute(
            select(NoticeDelivery.id).where(
                NoticeDelivery.admin_id == admin_id, NoticeDelivery.notice_id == notice_id
            )
        )
    ).scalar_one_or_none()
    if already is not None:
        return 0
    email = (
        await session.execute(select(Account.email).where(Account.id == admin_id))
    ).scalar_one_or_none()
    if not email:
        return 0
    status = await build_employee_status(
        session, admin_id=admin_id, instance_id=inst.id, now=moment
    )
    if status is None:
        return 0
    tomorrow_start = today_start + timedelta(days=1)
    due_today = (
        await session.execute(
            select(func.count()).where(
                DeferredAction.admin_id == admin_id,
                DeferredAction.instance_id == inst.id,
                DeferredAction.action_type == "schedule_callback",
                DeferredAction.status == "queued",
                DeferredAction.next_attempt_at >= today_start,
                DeferredAction.next_attempt_at < tomorrow_start,
            )
        )
    ).scalar_one()
    subject, body = render_daily_brief(
        status, local_date=local_now.date(), due_callbacks_today=int(due_today)
    )
    outcome = await email_dispatcher.send_daily_brief(
        session,
        admin_id=admin_id,
        to=email,
        subject=subject,
        body=body,
        notice_id=notice_id,
        sender=sender,
    )
    if outcome.status == "failed":
        return 0  # no ledger row — the next hourly pass retries; the audit row has the failure
    session.add(
        NoticeDelivery(
            admin_id=admin_id,
            notice_id=notice_id,
            kind=BRIEF_KIND,
            delivery_status=outcome.status,
            sent_at=moment,
        )
    )
    await session.flush()
    return 1


__all__ = [
    "BRIEF_HOUR",
    "BRIEF_KIND",
    "brief_notice_id",
    "render_daily_brief",
    "send_daily_brief_if_due",
]
