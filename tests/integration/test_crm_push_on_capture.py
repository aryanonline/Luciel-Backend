"""Deterministic CRM push-on-capture (Phase 6.5).

The Journey's "keeps each lead's record current" must not depend on the model
remembering to call push_to_crm — a turn whose capture changed a fact pushes it
through the SAME broker gates a model call would face. Pinned here, against the
production upsert/link discipline (the in-memory CRM is transport only):

  - create → update across turns maintains ONE record, never a duplicate;
  - a turn that changes nothing pushes nothing;
  - a disabled tool pushes nothing (default-deny authorization);
  - a dormant/disconnected connection pushes nothing;
  - a phone-keyed lead stays safe in the dashboard and is NOT pushed into an
    email-keyed CRM.
"""

from __future__ import annotations

import uuid

import pytest
from app.db.session import tenant_session
from app.models.connection import InstanceConnection
from app.models.conversation import Session as SessionModel
from app.models.instance import Account, AccountState, Instance, LucielState
from app.models.tool_auth import InstanceToolAuthorization
from app.runtime.crm_push import push_captured_lead
from app.runtime.lead_capture import capture_or_update_lead
from app.tools import crm_backend
from app.tools.registry import make_tool_resolver


@pytest.fixture(autouse=True)
def fresh_mock_crm():
    """Each test gets its own in-memory CRM store (it is a process global)."""
    crm_backend._mock_api = crm_backend.InMemoryCrmApi()
    yield crm_backend._mock_api
    crm_backend._mock_api = None


async def _seed_tenant(
    admin: uuid.UUID,
    *,
    tool_enabled: bool = True,
    crm_status: str = "connected",
    provider: str = "salesforce",
) -> tuple[uuid.UUID, uuid.UUID]:
    from app.connections.secrets import get_secret_store, secret_ref_for

    iid = uuid.uuid4()
    sid = uuid.uuid4()
    ref = secret_ref_for(admin, iid, "crm")
    await get_secret_store().put(
        ref, {"access_token": "sf-tok", "instance_url": "https://acme.my.salesforce.com"}
    )
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
                name="Push Co",
                state=LucielState.active,
                embed_key_public_id=f"vm_live_{admin.hex[:8]}",
                channels=[{"id": "widget", "enabled": True}],
                tools=[{"id": "push_to_crm", "enabled": tool_enabled}],
                escalation={},
                personality={},
            )
        )
        s.add(
            InstanceConnection(
                connection_id=uuid.uuid4(),
                admin_id=admin,
                instance_id=iid,
                connection_type="crm",
                provider=provider,
                auth_class="oauth_token",
                status=crm_status,
                secret_ref=ref,
                non_secret_config={},
            )
        )
        # The broker's gate 1 reads the authorization TABLE, not the tools JSON.
        s.add(
            InstanceToolAuthorization(
                admin_id=admin,
                instance_id=iid,
                tool_id="push_to_crm",
                enabled=tool_enabled,
            )
        )
        s.add(
            SessionModel(
                id=sid,
                admin_id=admin,
                instance_id=iid,
                channel="widget",
                mode="ai",
            )
        )
        await s.flush()
    return iid, sid


async def _capture_and_push(
    admin: uuid.UUID, iid: uuid.UUID, sid: uuid.UUID, inbound_text: str
) -> None:
    from sqlalchemy import select

    async with tenant_session(admin) as s:
        sess_obj = (
            await s.execute(select(SessionModel).where(SessionModel.id == sid))
        ).scalar_one()
        captured = await capture_or_update_lead(
            s,
            admin_id=admin,
            instance_id=iid,
            sess=sess_obj,
            inbound_text=inbound_text,
            channel="widget",
            transport_identifier=None,
        )
        if captured is not None:
            await push_captured_lead(
                s,
                admin_id=admin,
                instance_id=iid,
                captured=captured,
                lead_key=captured.lead_key,
                snapshot_tool_ids=None,
                tool_resolver=make_tool_resolver(),
                session_id=sid,
            )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_capture_creates_then_updates_one_crm_record(fresh_mock_crm):
    admin = uuid.uuid4()
    iid, sid = await _seed_tenant(admin)

    await _capture_and_push(admin, iid, sid, "I'm jane@acme.com — I need a quote for a reno.")
    assert len(fresh_mock_crm.records) == 1
    record_id, rec = next(iter(fresh_mock_crm.records.items()))
    assert rec["lead_key"] == "jane@acme.com"
    assert "intent" in rec["fields"]

    # A NEW fact on a later turn updates the SAME record — never a duplicate.
    await _capture_and_push(admin, iid, sid, "Budget is around $12k, I want to book this month.")
    assert len(fresh_mock_crm.records) == 1
    assert "12" in (fresh_mock_crm.records[record_id]["fields"].get("intent") or "")

    # A turn that changes nothing pushes nothing (fields stay as they were).
    before = dict(fresh_mock_crm.records[record_id]["fields"])
    await _capture_and_push(admin, iid, sid, "Ok thanks!")
    assert fresh_mock_crm.records[record_id]["fields"] == before


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_disabled_tool_pushes_nothing(fresh_mock_crm):
    admin = uuid.uuid4()
    iid, sid = await _seed_tenant(admin, tool_enabled=False)
    await _capture_and_push(admin, iid, sid, "I'm jane@acme.com — pricing please.")
    # The lead is safe in the dashboard store; the CRM saw nothing.
    assert fresh_mock_crm.records == {}


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_dormant_connection_pushes_nothing(fresh_mock_crm):
    admin = uuid.uuid4()
    iid, sid = await _seed_tenant(admin, crm_status="dormant")
    await _capture_and_push(admin, iid, sid, "I'm jane@acme.com — pricing please.")
    assert fresh_mock_crm.records == {}


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_self_introduced_name_reaches_the_lead_and_the_crm_push(fresh_mock_crm):
    """#5d (live-caught 2026-08-23): 'I'm Jane Miller' left the Lead named None and
    the Salesforce push fell back to LastName 'Unknown'. The capture now fills the
    name (fill-when-absent only) and forwards it as a changed fact."""
    from app.models.lead import Lead
    from sqlalchemy import select

    admin = uuid.uuid4()
    iid, sid = await _seed_tenant(admin)

    await _capture_and_push(
        admin, iid, sid, "Hi, I'm Jane Miller — email me at jane@acme.com about a quote."
    )
    assert len(fresh_mock_crm.records) == 1
    record_id, rec = next(iter(fresh_mock_crm.records.items()))
    assert rec["fields"].get("name") == "Jane Miller"
    async with tenant_session(admin) as s:
        lead = (await s.execute(select(Lead).where(Lead.admin_id == admin))).scalar_one()
        assert lead.name == "Jane Miller"

    # A later differing self-introduction never overwrites the recorded name —
    # the extractor is conservative, not authoritative.
    await _capture_and_push(admin, iid, sid, "Actually I'm Bob — also, budget is $9k.")
    async with tenant_session(admin) as s:
        lead = (await s.execute(select(Lead).where(Lead.admin_id == admin))).scalar_one()
        assert lead.name == "Jane Miller"
    assert fresh_mock_crm.records[record_id]["fields"].get("name") == "Jane Miller"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_phone_keyed_lead_is_not_pushed_into_an_email_keyed_crm(fresh_mock_crm):
    admin = uuid.uuid4()
    iid, sid = await _seed_tenant(admin)
    await _capture_and_push(admin, iid, sid, "Call me at 604-555-0100 about pricing.")
    assert fresh_mock_crm.records == {}


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_email_volunteered_later_finally_reaches_the_crm(fresh_mock_crm):
    """Round 5A item 5 (live-audited 2026-08-24): an SMS/voice lead who later
    volunteered an email had it silently DISCARDED — never stored, never pushed.
    Now the address is the record's CRM key from the moment it is known, and the
    first push carries the record's standing facts, not just that turn's delta."""
    from app.models.lead import Lead
    from sqlalchemy import select

    admin = uuid.uuid4()
    iid, sid = await _seed_tenant(admin)

    # Phone-keyed capture: name + intent land in the dashboard, nothing in CRM.
    await _capture_and_push(
        admin, iid, sid, "Hi, I'm Jane Miller — call me at 604-555-0100 for a reno quote."
    )
    assert fresh_mock_crm.records == {}

    # The email arrives → the CRM record exists, keyed on it, carrying the
    # already-known facts.
    await _capture_and_push(admin, iid, sid, "Better yet, email me at Jane.M@Acme.com")
    assert len(fresh_mock_crm.records) == 1
    rec = next(iter(fresh_mock_crm.records.values()))
    assert rec["lead_key"] == "jane.m@acme.com"  # normalized lower-case
    assert rec["fields"].get("name") == "Jane Miller"
    async with tenant_session(admin) as s:
        lead = (await s.execute(select(Lead).where(Lead.admin_id == admin))).scalar_one()
        assert lead.email == "jane.m@acme.com"
        # The transport key is untouched — inbound routing still knows them.
        assert lead.contact_identifier != lead.email

    # Fill-when-absent: a second, different address never rekeys.
    await _capture_and_push(admin, iid, sid, "or try other@else.com maybe")
    async with tenant_session(admin) as s:
        lead = (await s.execute(select(Lead).where(Lead.admin_id == admin))).scalar_one()
        assert lead.email == "jane.m@acme.com"
    assert len(fresh_mock_crm.records) == 1
