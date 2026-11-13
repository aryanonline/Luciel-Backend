"""Lead identity through the real store (round 7 WP-3c items 1, 2, 3 and 7).

* Unification runs on the FIRST turn: a customer writing from a new number who
  says "it's Jane — jane@acme.com" continues Jane's record (prior summaries and
  all) instead of becoming a duplicate; when two rows already exist the older one
  survives (item 1).
* A mentioned address or a reference number is stored as a ``mention`` — never
  the lead's own email/phone, never the CRM key; an anonymous visitor mentioning
  a colleague creates no lead (item 2).
* A push never rewrites the CRM's Email: a lead whose address moved is noted on
  the record, the link keeps the CRM's key, one record throughout (item 2).
* The custom webhook keeps a link per lead and announces ``previousLeadKey`` once
  when the key moves (item 3).
* A text FROM a number an anonymous widget visitor typed resolves to the texter's
  OWN lead: the transport is the proof, the typed claim becomes a mention; a lead a
  transport has already reached keeps its typed second identifier (item 7).
"""

from __future__ import annotations

import uuid

import pytest
from app.api.deps import Subject
from app.api.v1 import dashboard
from app.connections import sandbox
from app.db.session import tenant_session
from app.models.audit import AdminAuditLog
from app.models.connection import InstanceConnection
from app.models.conversation import Session as SessionModel
from app.models.crm_link import CrmLeadLink
from app.models.instance import Account, AccountState, Instance, LucielState
from app.models.lead import Lead, LeadIdentifier
from app.models.tool_auth import InstanceToolAuthorization
from app.runtime import memory
from app.runtime.crm_push import push_captured_lead
from app.runtime.lead_capture import CaptureResult, capture_or_update_lead
from app.schemas_api.leads import LeadPatchRequest
from app.tools import contract as c
from app.tools import crm_backend
from app.tools.registry import make_tool_resolver
from sqlalchemy import select

SMS_FROM = "+14165550143"


@pytest.fixture(autouse=True)
def fresh_mock_crm():
    crm_backend._mock_api = crm_backend.InMemoryCrmApi()
    yield crm_backend._mock_api
    crm_backend._mock_api = None


async def _seed_tenant(admin: uuid.UUID, *, provider: str | None = None) -> uuid.UUID:
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
                name="Identity Co",
                state=LucielState.active,
                embed_key_public_id=f"vm_live_{admin.hex[:10]}",
                channels=[{"id": "widget", "enabled": True}, {"id": "sms", "enabled": True}],
                tools=[{"id": "push_to_crm", "enabled": True}],
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
                    admin_id=admin, instance_id=iid, tool_id="push_to_crm", enabled=True
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
    push: bool = False,
) -> CaptureResult | None:
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
        if captured is not None and push:
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
        return captured


async def _leads(admin: uuid.UUID) -> list[Lead]:
    async with tenant_session(admin) as s:
        rows = (await s.execute(select(Lead).where(Lead.admin_id == admin))).scalars().all()
        return sorted(rows, key=lambda r: r.created_at)


async def _identifiers(
    admin: uuid.UUID,
) -> dict[tuple[str, str], tuple[uuid.UUID, str | None, bool]]:
    """(kind, value) -> (lead_id, source, confirmed)."""
    async with tenant_session(admin) as s:
        rows = (
            (await s.execute(select(LeadIdentifier).where(LeadIdentifier.admin_id == admin)))
            .scalars()
            .all()
        )
        return {(r.kind, r.value): (r.lead_id, r.source, r.confirmed_at is not None) for r in rows}


async def _bound_lead(admin: uuid.UUID, sid: uuid.UUID) -> uuid.UUID | None:
    async with tenant_session(admin) as s:
        return (
            (await s.execute(select(SessionModel).where(SessionModel.id == sid)))
            .scalar_one()
            .resolved_lead_id
        )


async def _audit(admin: uuid.UUID, event_type: str) -> list[dict]:
    async with tenant_session(admin) as s:
        rows = (
            (
                await s.execute(
                    select(AdminAuditLog).where(
                        AdminAuditLog.admin_id == admin, AdminAuditLog.event_type == event_type
                    )
                )
            )
            .scalars()
            .all()
        )
        return [dict(r.event_payload or {}) for r in rows]


def _subject(admin: uuid.UUID) -> Subject:
    return Subject(admin_id=admin, user_id=admin, session_epoch=0)


# --- item 1: unification on the first turn --------------------------------------- #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_first_text_that_names_a_known_email_continues_that_lead_with_its_memory():
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    widget = await _session(admin, iid)
    await _capture(admin, iid, widget, "I'm Jane, my email is jane@acme.com — I want a quote")
    (jane,) = await _leads(admin)
    async with tenant_session(admin) as s:
        await memory.persist_summary(
            s,
            admin_id=admin,
            instance_id=iid,
            session_id=widget,
            resolved_lead_id=jane.id,
            summary="Intent: a quote | Next step: send pricing",
        )

    # A brand-new SMS session from a number nobody has seen: unbound, first turn.
    sms = await _session(admin, iid, channel="sms")
    result = await _capture(
        admin, iid, sms, "it's Jane — jane@acme.com", channel="sms", transport=SMS_FROM
    )
    assert result is not None and result.lead_id == jane.id
    assert [le.id for le in await _leads(admin)] == [jane.id]  # one lead, not two
    assert await _bound_lead(admin, sms) == jane.id
    (lead,) = await _leads(admin)
    assert lead.contact_identifier == "jane@acme.com"
    assert lead.phone == SMS_FROM  # the number the channel proved is on the record
    idents = await _identifiers(admin)
    assert idents[("email", "jane@acme.com")] == (jane.id, "typed", False)
    assert idents[("phone", SMS_FROM)] == (jane.id, "transport", True)
    # …and the SMS conversation starts with her prior summaries loaded.
    async with tenant_session(admin) as s:
        prior = await memory.load_cross_session_summaries(
            s,
            admin_id=admin,
            instance_id=iid,
            channel="sms",
            resolved_lead_id=jane.id,
            exclude_session_id=sms,
        )
    assert prior == ["Intent: a quote | Next step: send pricing"]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_two_existing_rows_unify_on_the_first_turn_and_the_older_survives():
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    widget = await _session(admin, iid)
    await _capture(admin, iid, widget, "hi, it's jane@acme.com — about a quote")
    first_sms = await _session(admin, iid, channel="sms")
    await _capture(
        admin, iid, first_sms, "hello, do you do renos?", channel="sms", transport=SMS_FROM
    )
    older, newer = await _leads(admin)
    assert older.contact_identifier == "jane@acme.com" and newer.contact_identifier == SMS_FROM

    second_sms = await _session(admin, iid, channel="sms")
    result = await _capture(
        admin, iid, second_sms, "it's Jane, jane@acme.com", channel="sms", transport=SMS_FROM
    )
    assert result is not None and result.lead_id == older.id
    assert [le.id for le in await _leads(admin)] == [older.id]
    assert await _bound_lead(admin, first_sms) == older.id  # re-pointed by the merge
    assert await _bound_lead(admin, second_sms) == older.id
    (survivor,) = await _leads(admin)
    assert survivor.phone == SMS_FROM
    idents = await _identifiers(admin)
    assert idents[("phone", SMS_FROM)][0] == older.id
    assert len(await _audit(admin, "lead_merged")) == 1


# --- item 2: mentions are not the lead's identity --------------------------------- #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_mentioned_address_or_reference_number_never_becomes_the_leads_own():
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    sid = await _session(admin, iid)
    await _capture(admin, iid, sid, "I'm jane@acme.com and I need a quote")
    copied = await _capture(admin, iid, sid, "please copy my colleague sarah@acme.com on the quote")
    assert copied is not None and "email" not in copied.changed
    assert copied.lead_key == "jane@acme.com"
    invoiced = await _capture(admin, iid, sid, "invoice 4165550143 is the one I'm asking about")
    assert invoiced is not None and "phone" not in invoiced.changed

    (lead,) = await _leads(admin)
    assert lead.contact_identifier == "jane@acme.com"
    assert lead.email is None and lead.phone is None
    idents = await _identifiers(admin)
    assert idents[("mention", "sarah@acme.com")][0] == lead.id
    assert idents[("mention", "+14165550143")][0] == lead.id
    assert ("email", "sarah@acme.com") not in idents
    assert ("phone", "+14165550143") not in idents

    # An anonymous visitor who only mentions someone else stays anonymous.
    anon = await _session(admin, iid)
    assert await _capture(admin, iid, anon, "copy my colleague sarah@acme.com please") is None
    assert len(await _leads(admin)) == 1


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_push_never_rewrites_the_crm_email_and_notes_the_new_address(fresh_mock_crm):
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin, provider="hubspot")
    sid = await _session(admin, iid)
    await _capture(admin, iid, sid, "I'm jane@acme.com — I need a quote", push=True)
    (rec,) = fresh_mock_crm.records.values()
    assert rec["lead_key"] == "jane@acme.com"
    (lead,) = await _leads(admin)

    # The owner corrects the address (item 6) → the push goes out under the new key.
    await dashboard.edit_lead(
        lead.id, body=LeadPatchRequest(email="new@acme.com"), subject=_subject(admin)
    )
    assert len(fresh_mock_crm.records) == 1  # the SAME record, never a duplicate
    (rec,) = fresh_mock_crm.records.values()
    assert rec["lead_key"] == "jane@acme.com"  # the CRM's Email stands
    assert rec["notes"]["email"] == "new@acme.com"  # …and the new address is noted
    async with tenant_session(admin) as s:
        link = (
            await s.execute(select(CrmLeadLink).where(CrmLeadLink.admin_id == admin))
        ).scalar_one()
        assert link.lead_key == "jane@acme.com"  # the link says what the CRM holds
        assert link.lead_id == lead.id

    # Every later push re-sends the note: the record's CURRENT facts carry it (F130).
    await dashboard.mark_lead_outcome(
        lead.id, body=dashboard.LeadOutcomeRequest(outcome="converted"), subject=_subject(admin)
    )
    (rec,) = fresh_mock_crm.records.values()
    assert rec["notes"]["email"] == "new@acme.com"
    assert rec["notes"]["outcome"] == "converted"
    assert len(fresh_mock_crm.records) == 1


# --- item 3: the webhook keeps a link and announces a moved key -------------------- #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_webhook_keeps_a_link_and_sends_previous_lead_key_once(monkeypatch):
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin, provider="custom_webhook")
    payloads: list[dict] = []

    async def _deliver(**kwargs):  # noqa: ANN003 — mirrors run_byo_webhook's kwargs
        payloads.append(dict(kwargs["payload"]))
        return c.ok({"status": 200})

    monkeypatch.setattr(sandbox, "run_byo_webhook", _deliver)
    sid = await _session(admin, iid)
    await _capture(admin, iid, sid, "Hi, I'm Jane — call me at 604-555-0100 for a quote", push=True)
    assert payloads[-1]["leadKey"] == "+16045550100"
    assert "previousLeadKey" not in payloads[-1]
    assert payloads[-1]["notes"]["intent"].startswith("Hi, I'm Jane")
    (lead,) = await _leads(admin)
    async with tenant_session(admin) as s:
        link = (
            await s.execute(select(CrmLeadLink).where(CrmLeadLink.admin_id == admin))
        ).scalar_one()
        assert (link.provider, link.lead_id, link.lead_key) == (
            "custom_webhook",
            lead.id,
            "+16045550100",
        )

    # The email arrives → the key moves → the endpoint is told what it replaces.
    await _capture(admin, iid, sid, "better yet, email me at jane@acme.com", push=True)
    assert payloads[-1]["leadKey"] == "jane@acme.com"
    assert payloads[-1]["previousLeadKey"] == "+16045550100"
    async with tenant_session(admin) as s:
        link = (
            await s.execute(select(CrmLeadLink).where(CrmLeadLink.admin_id == admin))
        ).scalar_one()
        assert link.lead_key == "jane@acme.com"

    # Once: the next push carries no previous key.
    await _capture(admin, iid, sid, "my budget is around $5000", push=True)
    assert payloads[-1]["leadKey"] == "jane@acme.com"
    assert "previousLeadKey" not in payloads[-1]
    assert len(payloads) == 3


# --- item 7: the transport proves ownership --------------------------------------- #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_text_from_a_number_an_anonymous_visitor_typed_gets_its_own_lead():
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    widget = await _session(admin, iid)
    await _capture(admin, iid, widget, "Call me at 604-555-0100 about a reno quote")
    (visitor,) = await _leads(admin)
    assert visitor.contact_identifier == "+16045550100"
    assert (await _identifiers(admin))[("phone", "+16045550100")] == (visitor.id, "typed", False)

    sms = await _session(admin, iid, channel="sms")
    result = await _capture(
        admin, iid, sms, "Hi, I'd like a quote please", channel="sms", transport="+16045550100"
    )
    assert result is not None and result.lead_id != visitor.id
    texter = next(le for le in await _leads(admin) if le.id == result.lead_id)
    assert texter.contact_identifier == "+16045550100"
    assert await _bound_lead(admin, sms) == texter.id
    visitor_now = next(le for le in await _leads(admin) if le.id == visitor.id)
    assert visitor_now.contact_identifier is None  # the mistyped claim is gone
    idents = await _identifiers(admin)
    assert idents[("phone", "+16045550100")] == (texter.id, "transport", True)
    assert idents[("mention", "+16045550100")][0] == visitor.id  # kept as a mention
    (event,) = await _audit(admin, "lead_identifier_rehomed")
    assert event["kind"] == "phone" and event["reason"] == "transport_proof"
    assert "604" not in str(event) and str(visitor.id) not in str(event)

    # The visitor's own bound session carries on against THEIR record.
    again = await _capture(admin, iid, widget, "my email is v@example.com")
    assert again is not None and again.lead_id == visitor.id
    assert len(await _leads(admin)) == 2
    # And the texter's next text resolves straight to their lead — no contest left.
    second = await _session(admin, iid, channel="sms")
    follow = await _capture(
        admin, iid, second, "still keen", channel="sms", transport="+16045550100"
    )
    assert follow is not None and follow.lead_id == texter.id
    assert len(await _audit(admin, "lead_identifier_rehomed")) == 1


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_lead_a_transport_reached_keeps_its_typed_second_identifier():
    """The F125 guarantee under item 7: a person the channel has proven (their SMS
    number) who typed a second address is honoured — an email from that address
    resolves to them, not to a new row."""
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    sms = await _session(admin, iid, channel="sms")
    await _capture(
        admin,
        iid,
        sms,
        "quote please, email me at bob@personal.com",
        channel="sms",
        transport="+16045550100",
    )
    (bob,) = await _leads(admin)
    mail = await _session(admin, iid, channel="email")
    result = await _capture(
        admin, iid, mail, "following up", channel="email", transport="bob@personal.com"
    )
    assert result is not None and result.lead_id == bob.id
    assert [le.id for le in await _leads(admin)] == [bob.id]
    assert (await _identifiers(admin))[("email", "bob@personal.com")] == (bob.id, "transport", True)
    assert await _audit(admin, "lead_identifier_rehomed") == []
