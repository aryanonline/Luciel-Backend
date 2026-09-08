"""Round 6 WP-F: the morning brief is counts and links, never narration."""

from __future__ import annotations

from datetime import UTC, date, datetime

from app.employee.brief import brief_notice_id, render_daily_brief
from app.employee.status import ON_DUTY, DayCounts, EmployeeStatus, Need


def _status(
    *, yesterday: DayCounts, needs: list[Need] | None = None, **over: object
) -> EmployeeStatus:
    base = dict(
        assistant_name="Aurora",
        business_short_name="Acme Patios",
        on_duty=ON_DUTY,
        conversations_used=12,
        free_allowance=50,
        free_remaining=38,
        billed_blocks=0,
        billable=True,
        period_resets_at=datetime(2026, 10, 1, tzinfo=UTC),
        timezone="America/Toronto",
        team_reachable_now=None,
        next_reachable=None,
        capabilities=[],
        needs=needs or [],
        yesterday=yesterday,
        today=DayCounts(date="2026-09-08"),
        daily_brief_enabled=True,
    )
    base.update(over)
    return EmployeeStatus(**base)  # type: ignore[arg-type]


def test_a_busy_day_reads_as_counts_by_channel_and_where_leads_went():
    status = _status(
        yesterday=DayCounts(
            date="2026-09-07",
            conversations=7,
            by_channel={"widget": 5, "sms": 2},
            leads=3,
            leads_to_crm=2,
            escalations=2,
            escalations_reached=1,
            bookings=1,
            callbacks_scheduled=1,
            human_takeovers=1,
        ),
        needs=[
            Need(
                "email_no_mailbox",
                "attention",
                "Email channel is on but no mailbox is connected",
                "Connect it.",
                "/x",
            ),
            Need("team_availability_unset", "info", "Optional thing", "Later.", "/y"),
        ],
        billed_blocks=1,
    )
    subject, body = render_daily_brief(status, local_date=date(2026, 9, 8), due_callbacks_today=2)
    assert subject == "Yesterday at Acme Patios: 7 conversations, 3 leads — Aurora's brief"
    assert "- 7 conversations (5 on website chat, 2 on text messages)." in body
    assert "- 3 leads captured, 2 sent to your CRM." in body
    assert "- 2 hand-offs to the team; 1 reached a configured contact." in body
    assert "- 1 appointment booked." in body
    assert "- 1 follow-up scheduled." in body
    assert "- 1 conversation taken over by a person." in body
    assert "- 12 of 50 free conversations used; 1 paid block." in body
    assert "- Email channel is on but no mailbox is connected. Connect it." in body
    assert "Optional thing" not in body  # only attention needs make the brief
    assert "- 2 follow-ups due to go out today." in body
    assert "Aurora is on duty around the clock." in body
    assert "closed" not in body.lower() and "offline" not in body.lower()


def test_a_quiet_day_is_still_a_brief_and_says_so():
    status = _status(yesterday=DayCounts(date="2026-09-07"), business_short_name=None)
    subject, body = render_daily_brief(status, local_date=date(2026, 9, 8), due_callbacks_today=0)
    assert subject == "A quiet day at your business — Aurora's brief"
    assert "- A quiet day: no conversations, leads, hand-offs or bookings." in body
    assert "- Nothing right now." in body
    assert "- No follow-ups scheduled for today." in body


def test_notice_id_is_keyed_on_the_local_date():
    assert brief_notice_id(date(2026, 9, 8)) == "brief:2026-09-08"
