"""Sweep fault isolation and bounded lists (2026-09-05 audit WP7).

* One tenant's failure never ends a sweep for the others (F036); it is an ERROR
  line (the SweepErrors alarm) and a SweepTenantFailure metric, never a crash.
* The dashboard lists are paged and stable-ordered.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.api.deps import Subject
from app.api.v1.dashboard import list_conversations, list_escalations, list_leads
from app.db.session import tenant_session
from app.jobs import sweeps
from app.models.conversation import EscalationEvent
from app.models.conversation import Session as SessionModel
from app.models.instance import Account, AccountState, Instance, LucielState
from app.models.lead import Lead, LeadState


async def _seed(admin: uuid.UUID) -> uuid.UUID:
    iid = uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            Account(
                id=admin,
                admin_id=admin,
                user_id=admin,
                email=f"{admin.hex[:8]}@x.com",
                email_lower=f"{admin.hex[:8]}@x.com",
                password_hash="x",
                state=AccountState.verified,
                email_verified=True,
            )
        )
        s.add(
            Instance(
                id=iid,
                admin_id=admin,
                name="P",
                state=LucielState.active,
                channels=[],
                tools=[],
                escalation={},
                personality={},
            )
        )
        await s.flush()
    return iid


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_one_tenants_failure_never_ends_the_sweep_for_the_others(monkeypatch, caplog):
    bad, good = uuid.uuid4(), uuid.uuid4()
    await _seed(bad)
    await _seed(good)

    async def candidates(kind: str) -> list[uuid.UUID]:
        return [bad, good]

    monkeypatch.setattr(sweeps, "sweep_candidates", candidates)
    seen: list[uuid.UUID] = []

    async def work(session, *, admin_id, now):  # noqa: ANN001
        seen.append(admin_id)
        if admin_id == bad:
            raise RuntimeError("provider exploded")
        return 1

    with caplog.at_level(logging.ERROR, logger="app.jobs.sweeps"):
        acted = await sweeps._per_tenant("health", work, None)
    assert seen == [bad, good]  # the run continued past the failure
    assert acted == [str(good)]
    failures = [r for r in caplog.records if "sweep_tenant_failed" in r.getMessage()]
    assert len(failures) == 1 and failures[0].levelno == logging.ERROR
    assert str(bad) in failures[0].getMessage() and "kind=health" in failures[0].getMessage()


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_dashboard_lists_are_paged_newest_first():
    admin = uuid.uuid4()
    iid = await _seed(admin)
    base = datetime(2026, 9, 1, tzinfo=UTC)
    async with tenant_session(admin) as s:
        for i in range(5):
            sid = uuid.uuid4()
            s.add(
                SessionModel(
                    id=sid,
                    admin_id=admin,
                    instance_id=iid,
                    channel="widget",
                    mode="ai",
                    last_message_at=base + timedelta(minutes=i),
                )
            )
            s.add(
                EscalationEvent(
                    admin_id=admin,
                    instance_id=iid,
                    session_id=sid,
                    signal="explicit_human_request",
                    gate="outcome",
                    fired_at=base + timedelta(minutes=i),
                )
            )
            s.add(
                Lead(
                    admin_id=admin,
                    instance_id=iid,
                    name=f"Lead {i}",
                    contact_identifier=f"l{i}@x.com",
                    state=LeadState.active,
                    last_activity_at=base + timedelta(minutes=i),
                )
            )
        await s.flush()
    subject = Subject(admin_id=admin, user_id=admin, session_epoch=0)

    page1 = await list_conversations(limit=2, offset=0, subject=subject)
    page2 = await list_conversations(limit=2, offset=2, subject=subject)
    page3 = await list_conversations(limit=2, offset=4, subject=subject)
    assert [len(page1), len(page2), len(page3)] == [2, 2, 1]
    assert page1[0].last_message_at > page1[1].last_message_at > page2[0].last_message_at
    assert {c.session_id for c in page1} & {c.session_id for c in page2} == set()

    esc = await list_escalations(limit=3, offset=0, subject=subject)
    assert len(esc) == 3 and esc[0].fired_at > esc[-1].fired_at
    assert len(await list_escalations(limit=3, offset=3, subject=subject)) == 2

    leads = await list_leads(limit=4, offset=0, subject=subject)
    assert [le.name for le in leads] == ["Lead 4", "Lead 3", "Lead 2", "Lead 1"]
    assert [le.name for le in await list_leads(limit=4, offset=4, subject=subject)] == ["Lead 0"]
    # The defaults still return everything a small tenant has.
    assert len(await list_leads(limit=200, offset=0, subject=subject)) == 5
