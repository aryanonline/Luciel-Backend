"""Round 6 WP-F: the morning brief goes out once per tenant per LOCAL day.

Pinned here: candidate discovery honours the opt-out; nothing is sent before 07:00
local; the ledger — not the schedule — makes "once" durable; a quiet day still gets
its (short) brief; a failed delivery leaves no ledger row so the next pass retries.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from app.db.session import tenant_session
from app.employee.brief import send_daily_brief_if_due
from app.jobs import sweeps
from app.models.instance import Account, AccountState, Instance, LucielState
from app.models.notice_delivery import NoticeDelivery
from sqlalchemy import select

# 2026-09-08 12:00 UTC = 08:00 EDT (Toronto): past the 07:00 local send hour.
MORNING = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
# 2026-09-08 09:00 UTC = 05:00 EDT: too early locally, even though UTC says 09:00.
BEFORE_SEVEN_LOCAL = datetime(2026, 9, 8, 9, 0, tzinfo=UTC)


class _CapturingSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def send(self, *, to: str, subject: str, body_text: str, tags=None) -> str:
        self.sent.append((to, subject, body_text))
        return f"msg-{len(self.sent)}"


class _FailingSender:
    async def send(self, *, to: str, subject: str, body_text: str, tags=None) -> str:
        raise ConnectionError("smtp down")


async def _seed(*, enabled: bool = True, timezone: str | None = "America/Toronto") -> uuid.UUID:
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            Account(
                id=admin,
                admin_id=admin,
                user_id=admin,
                email=f"owner-{admin.hex[:6]}@acme.example",
                email_lower=f"owner-{admin.hex[:6]}@acme.example",
                password_hash="x",
                state=AccountState.verified,
                email_verified=True,
            )
        )
        s.add(
            Instance(
                id=iid,
                admin_id=admin,
                name="Aurora",
                state=LucielState.active,
                channels=[{"id": "widget", "enabled": True}],
                tools=[],
                escalation={},
                personality={},
                timezone=timezone,
                business_short_name="Acme",
                daily_brief_enabled=enabled,
            )
        )
        await s.flush()
    return admin


async def _ledger(admin: uuid.UUID) -> list[NoticeDelivery]:
    async with tenant_session(admin) as s:
        return list((await s.execute(select(NoticeDelivery))).scalars().all())


@pytest.mark.usefixtures("db_engine_reset")
async def test_candidates_are_active_tenants_with_the_brief_on():
    wants = await _seed(enabled=True)
    opted_out = await _seed(enabled=False)
    candidates = set(await sweeps.sweep_candidates("daily_brief"))
    assert wants in candidates
    assert opted_out not in candidates


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_sent_once_per_local_day_and_not_before_seven_local():
    admin = await _seed()
    sender = _CapturingSender()
    async with tenant_session(admin) as s:
        assert (
            await send_daily_brief_if_due(s, admin_id=admin, now=BEFORE_SEVEN_LOCAL, sender=sender)
            == 0
        )
        assert await send_daily_brief_if_due(s, admin_id=admin, now=MORNING, sender=sender) == 1
        # Same local day, later hour: the ledger says it already went out.
        assert (
            await send_daily_brief_if_due(
                s, admin_id=admin, now=MORNING.replace(hour=20), sender=sender
            )
            == 0
        )
    assert len(sender.sent) == 1
    to, subject, body = sender.sent[0]
    assert to.startswith("owner-")
    assert subject == "A quiet day at Acme — Aurora's brief"
    assert "Aurora is on duty around the clock." in body
    rows = await _ledger(admin)
    assert [(r.notice_id, r.kind, r.delivery_status) for r in rows] == [
        ("brief:2026-09-08", "daily_brief", "sent")
    ]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_opt_out_sends_nothing():
    admin = await _seed(enabled=False)
    sender = _CapturingSender()
    async with tenant_session(admin) as s:
        assert await send_daily_brief_if_due(s, admin_id=admin, now=MORNING, sender=sender) == 0
    assert sender.sent == []
    assert await _ledger(admin) == []


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_failed_delivery_leaves_no_ledger_row_so_the_next_pass_retries():
    admin = await _seed()
    async with tenant_session(admin) as s:
        assert (
            await send_daily_brief_if_due(s, admin_id=admin, now=MORNING, sender=_FailingSender())
            == 0
        )
    assert await _ledger(admin) == []
    sender = _CapturingSender()
    async with tenant_session(admin) as s:
        assert await send_daily_brief_if_due(s, admin_id=admin, now=MORNING, sender=sender) == 1
    assert len(sender.sent) == 1


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_no_timezone_reads_the_clock_in_utc():
    admin = await _seed(timezone=None)
    sender = _CapturingSender()
    async with tenant_session(admin) as s:
        # 09:00 UTC is past 07:00 UTC — due for a tenant with no zone set.
        assert (
            await send_daily_brief_if_due(s, admin_id=admin, now=BEFORE_SEVEN_LOCAL, sender=sender)
            == 1
        )
    assert len(sender.sent) == 1
