"""Lead stewardship over the dashboard surface (round 7 WP-3c, items 5, 6 and 9).

* Archive answers with the SAME wire row every other lead route returns — phone and
  the CRM state included (item 9).
* Un-archive is the reverse of archive: active again, audited ``lead_reactivated``,
  idempotent (item 6).
* An owner edit of name / email / phone is audited with the changed field NAMES
  only, keeps the replaced identifier as a ``superseded`` row, refuses a value
  another lead owns, refuses a number that cannot be E.164, and forwards the change
  to the CRM through the standard broker gates (item 6).
* A lead nothing ever pushed is an explicit ``never_attempted`` whose detail is the
  reason under the tenant's CURRENT posture, and the one-time backfill pushes every
  such keyed lead through the standard broker path, bounded and idempotent (item 5).
"""

from __future__ import annotations

import uuid

import pytest
from app.api.deps import Subject
from app.api.errors import ApiError
from app.api.v1 import dashboard
from app.db.session import tenant_session
from app.models.audit import AdminAuditLog
from app.models.connection import InstanceConnection
from app.models.instance import Account, AccountState, Instance, LucielState
from app.models.lead import Lead, LeadIdentifier, LeadState
from app.models.tool_auth import InstanceToolAuthorization
from app.schemas_api.leads import LeadPatchRequest
from app.tools import crm_backend
from sqlalchemy import select

pytestmark = [pytest.mark.asyncio]


@pytest.fixture(autouse=True)
def fresh_mock_crm():
    crm_backend._mock_api = crm_backend.InMemoryCrmApi()
    yield crm_backend._mock_api
    crm_backend._mock_api = None


def _subject(admin: uuid.UUID) -> Subject:
    return Subject(admin_id=admin, user_id=admin, session_epoch=0)


async def _seed_tenant(
    admin: uuid.UUID,
    *,
    provider: str | None = "hubspot",
    crm_status: str = "connected",
    tool_enabled: bool = True,
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
                embed_key_public_id=f"vm_live_{admin.hex[:10]}",
                channels=[{"id": "widget", "enabled": True}],
                tools=[{"id": "push_to_crm", "enabled": tool_enabled}],
                escalation={},
                personality={},
            )
        )
        if provider:
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
        s.add(
            InstanceToolAuthorization(
                admin_id=admin, instance_id=iid, tool_id="push_to_crm", enabled=tool_enabled
            )
        )
        await s.flush()
    if provider:
        await get_secret_store().put(ref, {"access_token": "hs-tok"})
    return iid


async def _seed_lead(
    admin: uuid.UUID,
    iid: uuid.UUID,
    *,
    contact: str | None,
    email: str | None = None,
    phone: str | None = None,
    name: str | None = None,
    state: LeadState = LeadState.active,
) -> uuid.UUID:
    lid = uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            Lead(
                id=lid,
                admin_id=admin,
                instance_id=iid,
                name=name,
                contact_identifier=contact,
                email=email,
                phone=phone,
                intent="a quote",
                state=state,
            )
        )
        await s.flush()
    return lid


async def _lead(admin: uuid.UUID, lid: uuid.UUID) -> Lead:
    async with tenant_session(admin) as s:
        return (await s.execute(select(Lead).where(Lead.id == lid))).scalar_one()


async def _identifiers(admin: uuid.UUID, lid: uuid.UUID) -> set[tuple[str, str]]:
    async with tenant_session(admin) as s:
        rows = (
            (
                await s.execute(
                    select(LeadIdentifier).where(
                        LeadIdentifier.admin_id == admin, LeadIdentifier.lead_id == lid
                    )
                )
            )
            .scalars()
            .all()
        )
        return {(r.kind, r.value) for r in rows}


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


# --- item 9: archive answers with the full wire row ------------------------------- #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_archive_returns_the_same_wire_row_as_every_other_lead_route():
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    lid = await _seed_lead(admin, iid, contact="jane@acme.com", phone="+14165550143")
    out = await dashboard.archive_lead(lid, subject=_subject(admin))
    assert out.state == "archived"
    assert out.phone == "+14165550143"  # used to be dropped by the hand-built row
    assert out.crm_status == "never_attempted"
    assert out.crm_detail == "not_yet_pushed"


# --- item 6: un-archive ------------------------------------------------------------- #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_unarchive_reactivates_audits_and_is_idempotent():
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin, provider=None)
    lid = await _seed_lead(admin, iid, contact="jane@acme.com", state=LeadState.archived)
    out = await dashboard.unarchive_lead(lid, subject=_subject(admin))
    assert out.state == "active"
    again = await dashboard.unarchive_lead(lid, subject=_subject(admin))
    assert again.state == "active"
    events = await _audit(admin, "lead_reactivated")
    assert len(events) == 1  # the no-op second call writes nothing
    assert events[0]["trigger"] == "owner_unarchive"
    assert "jane" not in str(events[0])


# --- item 6: owner edit ------------------------------------------------------------- #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_edit_audits_field_names_keeps_superseded_rows_and_forwards_to_the_crm(
    fresh_mock_crm,
):
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin, provider="hubspot")
    lid = await _seed_lead(
        admin, iid, contact="+16045550100", email="old@acme.com", name="Jane", phone=None
    )
    async with tenant_session(admin) as s:
        s.add(
            LeadIdentifier(
                admin_id=admin, instance_id=iid, lead_id=lid, kind="email", value="old@acme.com"
            )
        )
        await s.flush()

    out = await dashboard.edit_lead(
        lid,
        body=LeadPatchRequest(email="New@Acme.com", phone="416 555 0143"),
        subject=_subject(admin),
    )
    assert out.email == "new@acme.com"  # normalised like capture
    assert out.phone == "+14165550143"
    assert out.name == "Jane"  # omitted → untouched

    lead = await _lead(admin, lid)
    assert lead.email == "new@acme.com" and lead.phone == "+14165550143"
    idents = await _identifiers(admin, lid)
    assert ("superseded", "old@acme.com") in idents  # kept, not dropped
    assert ("email", "old@acme.com") not in idents  # …and no longer resolving
    assert ("email", "new@acme.com") in idents
    assert ("phone", "+14165550143") in idents

    (event,) = await _audit(admin, "lead_edited")
    assert sorted(event["changed_fields"]) == ["email", "phone"]
    assert "acme" not in str(event) and "416" not in str(event)  # names, never values

    # The CRM heard about it through the standard push: one record, keyed on the NEW
    # address (the moved key creates the record here — nothing was pushed before).
    assert len(fresh_mock_crm.records) == 1
    rec = next(iter(fresh_mock_crm.records.values()))
    assert rec["lead_key"] == "new@acme.com"
    assert rec["fields"].get("phone") == "+14165550143"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_edit_refuses_another_leads_identifier_and_an_unreadable_number():
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin, provider=None)
    lid = await _seed_lead(admin, iid, contact="jane@acme.com")
    await _seed_lead(admin, iid, contact="bob@acme.com")
    with pytest.raises(ApiError) as clash:
        await dashboard.edit_lead(
            lid, body=LeadPatchRequest(email="Bob@Acme.com"), subject=_subject(admin)
        )
    assert clash.value.status_code == 409
    with pytest.raises(ApiError) as bad:
        await dashboard.edit_lead(
            lid, body=LeadPatchRequest(phone="12345"), subject=_subject(admin)
        )
    assert bad.value.status_code == 422
    with pytest.raises(ApiError) as bad_email:
        await dashboard.edit_lead(
            lid, body=LeadPatchRequest(email="not-an-address"), subject=_subject(admin)
        )
    assert bad_email.value.status_code == 422
    lead = await _lead(admin, lid)
    assert lead.email is None and lead.phone is None  # nothing half-applied
    assert await _audit(admin, "lead_edited") == []


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_edit_with_an_explicit_null_clears_and_an_omitted_field_is_left_alone():
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin, provider=None)
    lid = await _seed_lead(admin, iid, contact="+16045550100", name="Jane", phone="+14165550143")
    out = await dashboard.edit_lead(lid, body=LeadPatchRequest(name=None), subject=_subject(admin))
    assert out.name is None
    assert out.phone == "+14165550143"
    (event,) = await _audit(admin, "lead_edited")
    assert event["changed_fields"] == ["name"]


# --- item 5: never_attempted + the one-time backfill ------------------------------- #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_never_attempted_carries_the_reason_under_the_current_posture():
    subject_reasons = []
    for provider, status, tool_on, contact, expected in (
        (None, "connected", True, "jane@acme.com", "crm_not_connected"),
        ("hubspot", "dormant", True, "jane@acme.com", "dormant"),
        ("hubspot", "connected", False, "jane@acme.com", "tool_off"),
        ("hubspot", "connected", True, "+16045550100", "no_crm_key"),
        ("custom_webhook", "connected", True, "+16045550100", "not_yet_pushed"),
        ("hubspot", "connected", True, "jane@acme.com", "not_yet_pushed"),
    ):
        admin = uuid.uuid4()
        iid = await _seed_tenant(admin, provider=provider, crm_status=status, tool_enabled=tool_on)
        await _seed_lead(admin, iid, contact=contact)
        (out,) = await dashboard.list_leads(limit=10, offset=0, subject=_subject(admin))
        assert out.crm_status == "never_attempted"
        subject_reasons.append(out.crm_detail)
        assert out.crm_detail == expected, (provider, status, tool_on, contact)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_backfill_pushes_every_keyed_never_attempted_lead_once(fresh_mock_crm):
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin, provider="hubspot")
    keyed = await _seed_lead(admin, iid, contact="jane@acme.com", name="Jane")
    keyed_2 = await _seed_lead(admin, iid, contact="+16045550100", email="bob@acme.com")
    phone_only = await _seed_lead(admin, iid, contact="+16045550199")
    archived = await _seed_lead(admin, iid, contact="zed@acme.com", state=LeadState.archived)

    result = await dashboard.backfill_crm(subject=_subject(admin))
    assert (result.attempted, result.synced, result.failed) == (2, 2, 0)
    assert result.skipped_no_key == 1  # the phone-only lead: HubSpot keys on email
    assert result.remaining == 0 and result.refused_reason is None
    assert {r["lead_key"] for r in fresh_mock_crm.records.values()} == {
        "jane@acme.com",
        "bob@acme.com",
    }
    for lid in (keyed, keyed_2):
        assert (await _lead(admin, lid)).crm_push_status == "synced"
    assert (await _lead(admin, phone_only)).crm_push_status is None
    assert (await _lead(admin, archived)).crm_push_status is None  # archived: untouched

    # Idempotent: the second pass finds nothing to push and creates no duplicate.
    again = await dashboard.backfill_crm(subject=_subject(admin))
    assert (again.attempted, again.synced, again.remaining) == (0, 0, 0)
    assert len(fresh_mock_crm.records) == 2
    runs = await _audit(admin, "crm_backfill_run")
    assert [r["synced"] for r in runs] == [2, 0]
    assert "acme" not in str(runs)

    # The dashboard row now reads synced, and the untouched one still explains itself.
    rows = {
        r.lead_id: r
        for r in await dashboard.list_leads(limit=10, offset=0, subject=_subject(admin))
    }
    assert rows[keyed].crm_status == "synced"
    assert rows[phone_only].crm_status == "never_attempted"
    assert rows[phone_only].crm_detail == "no_crm_key"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_backfill_is_refused_when_nothing_could_be_pushed():
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin, provider=None)
    await _seed_lead(admin, iid, contact="jane@acme.com")
    with pytest.raises(ApiError) as no_crm:
        await dashboard.backfill_crm(subject=_subject(admin))
    assert no_crm.value.status_code == 409
    assert "No CRM" in str(no_crm.value.message)
    assert await _audit(admin, "crm_backfill_run") == []

    admin_off = uuid.uuid4()
    await _seed_tenant(admin_off, provider="hubspot", tool_enabled=False)
    with pytest.raises(ApiError) as off:
        await dashboard.backfill_crm(subject=_subject(admin_off))
    assert off.value.status_code == 409
    assert "switched off" in str(off.value.message)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_single_lead_retry_accepts_a_never_attempted_row(fresh_mock_crm):
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin, provider="hubspot")
    lid = await _seed_lead(admin, iid, contact="jane@acme.com")
    out = await dashboard.retry_crm_push(lid, subject=_subject(admin))
    assert out.crm_status == "synced" and out.crm_detail is None
    assert len(fresh_mock_crm.records) == 1
