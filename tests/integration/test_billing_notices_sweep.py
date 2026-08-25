"""Billing notice emails (Legal §A5 / §A10 + Vision §7 80% nudge).

§A5 owes an allowance reduction "BOTH by email AND as an in-dashboard
notification". The dashboard half has rendered since it shipped; before the
``billing_notices`` sweep nothing ever sent the email, so the commitment was met
only for owners who happened to log in. Pinned here:

  - candidate discovery finds ONLY verified accounts;
  - an announced allowance reduction is emailed exactly ONCE per tenant
    (the ``notice_deliveries`` ledger, not the schedule, is the idempotency);
  - a reduction announced with less than the §A5 30-day lead is emailed anyway
    (the tenant deserves the notice even late) but logs an operator-facing ERROR;
  - the ~80%-through-a-block pay-as-you-go heads-up goes out at most once per
    billing period, and never to a cardless (free-cap) tenant;
  - a FAILED delivery leaves no ledger row, so the next sweep retries it.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from app.billing.notice_delivery import deliver_due_notices
from app.config import get_settings
from app.db.session import tenant_session
from app.jobs import sweeps
from app.models.billing import BillingAccount
from app.models.budget import ConversationBudget
from app.models.instance import Account, AccountState, Instance, LucielState
from app.models.notice_delivery import NoticeDelivery
from sqlalchemy import select

pytestmark = [pytest.mark.asyncio]

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)


class _CapturingSender:
    """Records every send; the assertion surface for 'what mail left the platform'."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def send(self, *, to: str, subject: str, body_text: str, tags=None) -> str:
        self.sent.append((to, subject, body_text))
        return f"msg-{len(self.sent)}"


class _FailingSender:
    async def send(self, *, to: str, subject: str, body_text: str, tags=None) -> str:
        raise ConnectionError("smtp down")


async def _seed(
    *, state: AccountState = AccountState.verified, with_instance: bool = True
) -> tuple[uuid.UUID, uuid.UUID]:
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            Account(
                id=admin,
                admin_id=admin,
                user_id=admin,
                email=f"{admin}@x.com",
                email_lower=f"{admin}@x.com",
                password_hash="x",
                state=state,
                email_verified=state is AccountState.verified,
            )
        )
        if with_instance:
            s.add(
                Instance(
                    id=iid,
                    admin_id=admin,
                    name="L",
                    state=LucielState.active,
                    channels=[],
                    tools=[],
                    escalation={},
                    personality={},
                )
            )
    return admin, iid


def _announce(monkeypatch, *, to: int, effective_at: datetime) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "free_allowance_reduction_effective_at", effective_at.isoformat())
    monkeypatch.setattr(settings, "free_allowance_reduction_to", to)


async def _delivery_rows(admin: uuid.UUID) -> list[NoticeDelivery]:
    async with tenant_session(admin) as s:
        return list(
            (
                await s.execute(select(NoticeDelivery).where(NoticeDelivery.admin_id == admin))
            ).scalars()
        )


async def test_discovery_finds_only_verified_accounts() -> None:
    verified_admin, _ = await _seed(state=AccountState.verified)
    unverified_admin, _ = await _seed(state=AccountState.unverified)

    candidates = await sweeps.sweep_candidates(sweeps.BILLING_NOTICES)
    assert verified_admin in candidates
    assert unverified_admin not in candidates


async def test_allowance_reduction_is_emailed_exactly_once(monkeypatch) -> None:
    admin, _ = await _seed()
    _announce(monkeypatch, to=25, effective_at=NOW + timedelta(days=40))
    sender = _CapturingSender()

    async with tenant_session(admin) as s:
        first = await deliver_due_notices(s, admin_id=admin, now=NOW, sender=sender)
    async with tenant_session(admin) as s:
        second = await deliver_due_notices(
            s, admin_id=admin, now=NOW + timedelta(days=1), sender=sender
        )

    assert (first, second) == (1, 0)
    assert len(sender.sent) == 1
    to, subject, body = sender.sent[0]
    assert to == f"{admin}@x.com"
    assert subject == "Your free monthly conversation allowance is changing"
    assert "25" in body and "Billing page" in body

    (row,) = await _delivery_rows(admin)
    assert row.kind == "allowance_reduction"
    assert row.delivery_status == "sent"


async def test_short_lead_reduction_still_sends_but_screams(monkeypatch, caplog) -> None:
    """§A5 wants ≥30 days' lead. The platform cannot conjure the missing days, so a
    late announcement is delivered anyway (the tenant deserves the notice) while the
    violation is made loud and operator-facing instead of silently blessed."""
    admin, _ = await _seed()
    _announce(monkeypatch, to=25, effective_at=NOW + timedelta(days=10))
    sender = _CapturingSender()

    with caplog.at_level(logging.ERROR, logger="app.billing.notice_delivery"):
        async with tenant_session(admin) as s:
            sent = await deliver_due_notices(s, admin_id=admin, now=NOW, sender=sender)

    assert sent == 1 and len(sender.sent) == 1
    assert any(
        "Legal §A5" in r.getMessage() and "10 days" in r.getMessage() for r in caplog.records
    )


async def test_payg_nudge_goes_out_once_per_period_and_never_to_free_cap() -> None:
    period = date(2026, 8, 1)  # calendar fallback for NOW — no Stripe period yet

    payg_admin, payg_iid = await _seed()
    capless_admin, capless_iid = await _seed()
    async with tenant_session(payg_admin) as s:
        s.add(
            BillingAccount(
                id=uuid.uuid4(),
                admin_id=payg_admin,
                billing_state="payg_enabled",
                dunning_state="none",
            )
        )
        s.add(
            ConversationBudget(
                id=uuid.uuid4(),
                admin_id=payg_admin,
                instance_id=payg_iid,
                billing_period_start=period,
                counted_conversations=131,  # 81 into the current block → ≥80% through it
                counted_session_ids=[],
            )
        )
    async with tenant_session(capless_admin) as s:
        s.add(  # same usage, but no card: at_cap territory, never a billing nudge
            ConversationBudget(
                id=uuid.uuid4(),
                admin_id=capless_admin,
                instance_id=capless_iid,
                billing_period_start=period,
                counted_conversations=131,
                counted_session_ids=[],
            )
        )

    sender = _CapturingSender()
    async with tenant_session(payg_admin) as s:
        first = await deliver_due_notices(s, admin_id=payg_admin, now=NOW, sender=sender)
    async with tenant_session(payg_admin) as s:
        second = await deliver_due_notices(
            s, admin_id=payg_admin, now=NOW + timedelta(days=2), sender=sender
        )
    async with tenant_session(capless_admin) as s:
        capless = await deliver_due_notices(s, admin_id=capless_admin, now=NOW, sender=sender)

    assert (first, second, capless) == (1, 0, 0)
    assert len(sender.sent) == 1
    _, subject, body = sender.sent[0]
    assert "pay-as-you-go" in subject
    assert "131" in body and "$39" in body

    (row,) = await _delivery_rows(payg_admin)
    assert row.notice_id == f"payg80:{period.isoformat()}"
    assert row.kind == "payg_usage_nudge"


async def test_failed_delivery_leaves_no_ledger_row_so_the_next_sweep_retries(
    monkeypatch,
) -> None:
    admin, _ = await _seed()
    _announce(monkeypatch, to=25, effective_at=NOW + timedelta(days=40))

    async with tenant_session(admin) as s:
        sent = await deliver_due_notices(s, admin_id=admin, now=NOW, sender=_FailingSender())
    assert sent == 0
    assert await _delivery_rows(admin) == []

    retry = _CapturingSender()
    async with tenant_session(admin) as s:
        sent = await deliver_due_notices(
            s, admin_id=admin, now=NOW + timedelta(days=1), sender=retry
        )
    assert sent == 1 and len(retry.sent) == 1
    (row,) = await _delivery_rows(admin)
    assert row.kind == "allowance_reduction"


async def test_the_full_sweep_runner_acts_and_is_idempotent(monkeypatch) -> None:
    """End-to-end through run_billing_notices: mock posture (Null sender → ``recorded``)
    still writes the ledger, so the daily schedule cannot re-send in any mode."""
    admin, _ = await _seed()
    _announce(monkeypatch, to=25, effective_at=NOW + timedelta(days=45))

    acted = await sweeps.run_billing_notices(now=NOW)
    assert str(admin) in acted

    again = await sweeps.run_billing_notices(now=NOW + timedelta(days=1))
    assert str(admin) not in again

    (row,) = await _delivery_rows(admin)
    assert row.delivery_status == "recorded"  # Null sender is the honest mock posture
