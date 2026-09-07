"""Leads and CRM stewardship through the real store (2026-09-05 audit WP6).

* A phone typed into the widget and a later text FROM that number are one lead
  (F111/F127), and the identifier table remembers it.
* Unifying two rows keeps the absorbed identity resolvable (F125).
* A phone-only lead reaches the tenant's custom webhook keyed on its number (F126).
* A push the CRM refuses is stamped on the lead, emailed to the owner ONCE, and
  retryable from the dashboard (F134); an owner-disabled tool is not a failure.
* A key the conversation's lead does not own is refused outright (F095).
* Erasure takes the identifier rows; a CRM swap forgets the old record ids (F120).
"""

from __future__ import annotations

import uuid

import pytest
from app.api.deps import Subject
from app.api.v1.dashboard import retry_crm_push
from app.connections import sandbox, service
from app.db.session import tenant_session
from app.lifecycle import lead_erasure
from app.models.audit import ActorType, AdminAuditLog
from app.models.connection import InstanceConnection
from app.models.conversation import Session as SessionModel
from app.models.crm_link import CrmLeadLink
from app.models.instance import Account, AccountState, Instance, LucielState
from app.models.lead import Lead, LeadIdentifier
from app.models.tool_auth import InstanceToolAuthorization
from app.runtime.crm_push import push_captured_lead
from app.runtime.lead_capture import capture_or_update_lead
from app.tools import contract as c
from app.tools import crm_backend
from app.tools.broker import ToolBroker
from app.tools.contract import Outcome
from app.tools.registry import build_tool, make_tool_resolver
from sqlalchemy import select

pytestmark = [pytest.mark.asyncio]


@pytest.fixture(autouse=True)
def fresh_mock_crm():
    crm_backend._mock_api = crm_backend.InMemoryCrmApi()
    yield crm_backend._mock_api
    crm_backend._mock_api = None


async def _seed_tenant(
    admin: uuid.UUID, *, provider: str | None = "hubspot", tool_enabled: bool = True
) -> uuid.UUID:
    from app.connections.secrets import get_secret_store, secret_ref_for

    iid = uuid.uuid4()
    ref = secret_ref_for(admin, iid, "crm")
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
                name="Stew Co",
                state=LucielState.active,
                embed_key_public_id=f"vm_live_{admin.hex[:8]}",
                channels=[{"id": "widget", "enabled": True}, {"id": "sms", "enabled": True}],
                tools=[{"id": "push_to_crm", "enabled": tool_enabled}],
                escalation={},
                personality={},
            )
        )
        if provider:
            config = (
                {"url": "https://hooks.example.com/leads", "egressAllowlist": ["hooks.example.com"]}
                if provider == "custom_webhook"
                else {}
            )
            s.add(
                InstanceConnection(
                    connection_id=uuid.uuid4(),
                    admin_id=admin,
                    instance_id=iid,
                    connection_type="crm",
                    provider=provider,
                    auth_class="long_lived_token"
                    if provider == "custom_webhook"
                    else "oauth_token",
                    status="connected",
                    secret_ref=ref,
                    non_secret_config=config,
                )
            )
        s.add(
            InstanceToolAuthorization(
                admin_id=admin, instance_id=iid, tool_id="push_to_crm", enabled=tool_enabled
            )
        )
        await s.flush()
    if provider == "custom_webhook":
        await get_secret_store().put(ref, {"auth_header": "Bearer tenant-token"})
    elif provider:
        await get_secret_store().put(ref, {"access_token": "hs-tok"})
    return iid


async def _session(admin: uuid.UUID, iid: uuid.UUID, channel: str = "widget") -> uuid.UUID:
    sid = uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(SessionModel(id=sid, admin_id=admin, instance_id=iid, channel=channel, mode="ai"))
        await s.flush()
    return sid


async def _capture(
    admin: uuid.UUID,
    iid: uuid.UUID,
    sid: uuid.UUID,
    text: str,
    *,
    channel: str = "widget",
    transport: str | None = None,
    push: bool = True,
) -> str | None:
    async with tenant_session(admin) as s:
        sess = (await s.execute(select(SessionModel).where(SessionModel.id == sid))).scalar_one()
        captured = await capture_or_update_lead(
            s,
            admin_id=admin,
            instance_id=iid,
            sess=sess,
            inbound_text=text,
            channel=channel,
            transport_identifier=transport,
        )
        if captured is None or not push:
            return None
        receipt = await push_captured_lead(
            s,
            admin_id=admin,
            instance_id=iid,
            captured=captured,
            lead_key=captured.lead_key,
            snapshot_tool_ids=None,
            tool_resolver=make_tool_resolver(),
            session_id=sid,
        )
        return receipt.outcome if receipt is not None else None


async def _leads(admin: uuid.UUID) -> list[Lead]:
    async with tenant_session(admin) as s:
        return list((await s.execute(select(Lead).where(Lead.admin_id == admin))).scalars().all())


async def _identifiers(admin: uuid.UUID) -> dict[str, uuid.UUID]:
    async with tenant_session(admin) as s:
        rows = (
            (await s.execute(select(LeadIdentifier).where(LeadIdentifier.admin_id == admin)))
            .scalars()
            .all()
        )
        return {r.value: r.lead_id for r in rows}


async def _audit_count(admin: uuid.UUID, event_type: str) -> int:
    async with tenant_session(admin) as s:
        rows = (
            await s.execute(
                select(AdminAuditLog).where(
                    AdminAuditLog.admin_id == admin, AdminAuditLog.event_type == event_type
                )
            )
        ).scalars()
        return len(list(rows))


# --- F111 / F127 --------------------------------------------------------------- #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_typed_phone_and_a_later_text_from_it_are_one_lead():
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin, provider=None)
    widget = await _session(admin, iid)
    await _capture(admin, iid, widget, "Call me at 604-555-0100 about a reno quote", push=False)
    (lead,) = await _leads(admin)
    assert lead.contact_identifier == "+16045550100"  # E.164 from the first turn
    assert (await _identifiers(admin)) == {"+16045550100": lead.id}

    sms = await _session(admin, iid, channel="sms")
    await _capture(
        admin,
        iid,
        sms,
        "hi it's me, still keen",
        channel="sms",
        transport="+16045550100",
        push=False,
    )
    leads = await _leads(admin)
    assert [le.id for le in leads] == [lead.id]  # recognised, not duplicated
    async with tenant_session(admin) as s:
        bound = (await s.execute(select(SessionModel).where(SessionModel.id == sms))).scalar_one()
        assert bound.resolved_lead_id == lead.id


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_email_keyed_visitor_who_types_a_number_keeps_it_on_the_record():
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin, provider=None)
    sid = await _session(admin, iid)
    await _capture(admin, iid, sid, "I'm jane@acme.com and I want a quote", push=False)
    await _capture(admin, iid, sid, "you can also text me on 416 555 0143", push=False)
    (lead,) = await _leads(admin)
    assert lead.contact_identifier == "jane@acme.com"
    assert lead.phone == "+14165550143"
    assert (await _identifiers(admin)) == {"jane@acme.com": lead.id, "+14165550143": lead.id}


# --- F125 ------------------------------------------------------------------------ #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_unification_keeps_the_absorbed_identity_resolvable():
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin, provider=None)
    # The OLDER row: an SMS lead who already has their own email on file.
    sms = await _session(admin, iid, channel="sms")
    await _capture(
        admin,
        iid,
        sms,
        "quote please, email me at bob@personal.com",
        channel="sms",
        transport="+16045550100",
        push=False,
    )
    (older,) = await _leads(admin)
    # The NEWER row: the same person on the widget under a work address.
    widget = await _session(admin, iid)
    await _capture(admin, iid, widget, "hi, it's bob@acme.com — about that quote", push=False)
    assert len(await _leads(admin)) == 2
    # They prove they are one person by typing the number → unified into the older row.
    await _capture(admin, iid, widget, "my number is 604-555-0100", push=False)
    leads = await _leads(admin)
    assert [le.id for le in leads] == [older.id]
    assert leads[0].email == "bob@personal.com"  # the survivor's own email stands
    idents = await _identifiers(admin)
    assert idents == {
        "+16045550100": older.id,
        "bob@personal.com": older.id,
        "bob@acme.com": older.id,  # the absorbed identity now names the survivor
    }
    # …so a later email from the WORK address resolves to the survivor, not a new row.
    mail = await _session(admin, iid, channel="email")
    await _capture(
        admin, iid, mail, "following up", channel="email", transport="bob@acme.com", push=False
    )
    assert [le.id for le in await _leads(admin)] == [older.id]


# --- F126 ------------------------------------------------------------------------ #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_phone_only_lead_reaches_a_custom_webhook_keyed_on_its_number(monkeypatch):
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin, provider="custom_webhook")
    seen: dict[str, object] = {}

    async def _deliver(**kwargs):  # noqa: ANN003 — mirrors run_byo_webhook's kwargs
        seen.update(kwargs)
        return c.ok({"status": 200})

    monkeypatch.setattr(sandbox, "run_byo_webhook", _deliver)
    sid = await _session(admin, iid)
    outcome = await _capture(admin, iid, sid, "Call me at 604-555-0100 about pricing.")
    assert outcome == "ok"
    payload = seen["payload"]
    assert isinstance(payload, dict) and payload["leadKey"] == "+16045550100"
    (lead,) = await _leads(admin)
    assert lead.crm_push_status == "synced" and lead.crm_pushed_at is not None


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_phone_only_lead_still_never_enters_an_email_keyed_crm(fresh_mock_crm):
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin, provider="hubspot")
    sid = await _session(admin, iid)
    assert await _capture(admin, iid, sid, "Call me at 604-555-0100 about pricing.") is None
    assert fresh_mock_crm.records == {}
    (lead,) = await _leads(admin)
    assert lead.crm_push_status is None  # nothing attempted, nothing claimed


# --- F134 ------------------------------------------------------------------------ #
class _RefusingApi:
    """A CRM whose create/update fail the way a live 400 does."""

    async def find_by_key(self, token, lead_key):
        return None

    async def get(self, token, record_id):
        return None

    async def create(self, token, lead_key, fields, notes=None):
        raise crm_backend.CrmUnavailable("hubspot_create_http_400")

    async def update(self, token, record_id, lead_key, fields, notes=None):
        raise crm_backend.CrmUnavailable("hubspot_update_http_400")


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_refused_push_is_stamped_emailed_once_and_retryable(monkeypatch, fresh_mock_crm):
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin, provider="hubspot")
    sid = await _session(admin, iid)
    monkeypatch.setattr(crm_backend, "build_crm_api", lambda: _RefusingApi())

    assert await _capture(admin, iid, sid, "I'm jane@acme.com — I need a quote") == "failed"
    (lead,) = await _leads(admin)
    assert lead.crm_push_status == "failed"
    assert lead.crm_push_detail == "crm_error: hubspot_create_http_400"
    assert lead.crm_failure_notified_at is not None
    assert await _audit_count(admin, "crm_push_failure_emailed") == 1

    # A second failure on the next fact: still failed, the owner is NOT emailed again.
    assert await _capture(admin, iid, sid, "budget is about $9k") == "failed"
    assert await _audit_count(admin, "crm_push_failure_emailed") == 1

    # The CRM recovers; the owner retries from the dashboard → synced, one record.
    monkeypatch.setattr(crm_backend, "build_crm_api", lambda: fresh_mock_crm)
    out = await retry_crm_push(
        lead.id, subject=Subject(admin_id=admin, user_id=admin, session_epoch=0)
    )
    assert out.crm_status == "synced" and out.crm_detail is None
    assert len(fresh_mock_crm.records) == 1
    rec = next(iter(fresh_mock_crm.records.values()))
    assert rec["lead_key"] == "jane@acme.com"
    assert "intent" in rec["fields"] and rec["fields"]["outcome"] == "in_progress"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_owner_disabled_tool_is_a_refusal_not_a_failure(fresh_mock_crm):
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin, provider="hubspot", tool_enabled=False)
    sid = await _session(admin, iid)
    assert await _capture(admin, iid, sid, "I'm jane@acme.com — I need a quote") == "failed"
    (lead,) = await _leads(admin)
    assert lead.crm_push_status is None  # the owner chose this; nothing is 'broken'
    assert await _audit_count(admin, "crm_push_failure_emailed") == 0
    with pytest.raises(Exception, match="switched off"):
        await retry_crm_push(
            lead.id, subject=Subject(admin_id=admin, user_id=admin, session_epoch=0)
        )


# --- F095 ------------------------------------------------------------------------ #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_key_the_conversations_lead_does_not_own_is_refused(fresh_mock_crm):
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin, provider="hubspot")
    sid = await _session(admin, iid)
    await _capture(admin, iid, sid, "I'm jane@acme.com — I need a quote", push=False)
    (lead,) = await _leads(admin)
    tool = build_tool("push_to_crm")
    assert tool is not None
    async with tenant_session(admin) as s:
        res = await ToolBroker(s).dispatch(
            tool,
            {"lead_key": "stranger@elsewhere.com", "fields": {"intent": "hijack"}},
            admin_id=admin,
            instance_id=iid,
            resolved_lead_id=lead.id,
            session_id=sid,
        )
    assert res.outcome is Outcome.failed
    assert res.reason == "crm_error: crm_lead_key_not_owned"
    assert fresh_mock_crm.records == {}


# --- erasure + swap -------------------------------------------------------------- #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_erasure_takes_the_identifier_rows_with_the_lead():
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin, provider=None)
    sid = await _session(admin, iid)
    await _capture(admin, iid, sid, "I'm jane@acme.com, text me on 604-555-0100", push=False)
    assert len(await _identifiers(admin)) == 2
    async with tenant_session(admin) as s:
        lead = (await s.execute(select(Lead).where(Lead.admin_id == admin))).scalar_one()
        counts = await lead_erasure.erase_lead_data(
            s,
            admin_id=admin,
            lead=lead,
            event_type="lead_erasure",
            actor_type=ActorType.admin_user,
            actor_user_id=admin,
        )
    assert counts.identifiers_deleted == 2
    assert await _identifiers(admin) == {}
    assert await _leads(admin) == []


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_crm_swap_forgets_the_old_record_ids_like_a_disconnect():
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin, provider=None)
    async with tenant_session(admin) as s:
        conn, _ = await service.start_connection(
            s,
            admin_id=admin,
            instance_id=iid,
            connection_type="crm",
            provider="hubspot",
            user_id=None,
        )
        await service.confirm_reconnect(
            s, admin_id=admin, connection_id=conn.connection_id, user_id=None, probe_ok=True
        )
        s.add(
            CrmLeadLink(
                admin_id=admin,
                instance_id=iid,
                provider="hubspot",
                lead_id=None,
                lead_key="jane@acme.com",
                crm_record_id="hs-1",
            )
        )
        await s.flush()
        await service.swap(
            s, admin_id=admin, connection_id=conn.connection_id, provider="salesforce", user_id=None
        )
        ok, _ = await service.confirm_reconnect(
            s, admin_id=admin, connection_id=conn.connection_id, user_id=None, probe_ok=True
        )
        assert ok and conn.provider == "salesforce"
        links = (
            await s.execute(select(CrmLeadLink).where(CrmLeadLink.admin_id == admin))
        ).scalars()
        assert list(links) == []
        cleared = (
            await s.execute(
                select(AdminAuditLog).where(
                    AdminAuditLog.admin_id == admin, AdminAuditLog.event_type == "crm_links_cleared"
                )
            )
        ).scalars()
        payloads = [row.event_payload for row in cleared]
        assert payloads and payloads[-1]["reason"] == "swap" and payloads[-1]["count"] == 1


# --- Round 6 WP-C: a dead grant flips the row the moment a push hits it ------- #
class _RejectingApi:
    """A CRM whose grant is dead: the provider answers 401 to everything."""

    async def find_by_key(self, token, lead_key):
        raise crm_backend.CrmUnavailable("hubspot_search_http_401")

    async def get(self, token, record_id):
        raise crm_backend.CrmUnavailable("hubspot_get_http_401")

    async def create(self, token, lead_key, fields, notes=None):
        raise crm_backend.CrmUnavailable("hubspot_create_http_401")

    async def update(self, token, record_id, lead_key, fields, notes=None):
        raise crm_backard_never()  # pragma: no cover — find_by_key fails first


def crm_backard_never():  # pragma: no cover
    return crm_backend.CrmUnavailable("hubspot_update_http_401")


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_rejected_credential_flips_the_crm_row_and_marks_the_lead(
    monkeypatch, fresh_mock_crm
):
    """Round 6 WP-C (F081): the row used to read Connected until the next sweep while
    every push returned 401. Now the push that hits the dead grant flips it — audit
    row, one attention email — and the lead still records the honest failure."""
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin, provider="hubspot")
    sid = await _session(admin, iid)
    monkeypatch.setattr(crm_backend, "build_crm_api", lambda: _RejectingApi())

    assert await _capture(admin, iid, sid, "I'm jane@acme.com — I need a quote") == "failed"
    (lead,) = await _leads(admin)
    assert lead.crm_push_status == "failed"
    assert (lead.crm_push_detail or "").endswith("hubspot_search_http_401")
    async with tenant_session(admin) as s:
        row = (
            await s.execute(
                select(InstanceConnection).where(
                    InstanceConnection.admin_id == admin,
                    InstanceConnection.connection_type == "crm",
                )
            )
        ).scalar_one()
        assert row.status == "error"
        assert "rejected the stored credential" in (row.status_detail or "")
        assert "reconnect" in (row.status_detail or "")
    assert await _audit_count(admin, "connection_status_changed") == 1

    # A second failing push on a row that is already error transitions nothing more.
    assert await _capture(admin, iid, sid, "budget is about $9k") == "failed"
    assert await _audit_count(admin, "connection_status_changed") == 1
