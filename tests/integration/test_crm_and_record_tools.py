"""push_to_crm + lookup_record finally have an effect layer (§3.3.6, owner scope).

Pinned here:
  * HubSpot upsert MAINTAINS the same lead's record: two pushes → ONE record,
    second reports action=updated (owner-reconfirmed semantics — "not just a
    one-time push").
  * The crm_lead_links row survives an email change: the link, not the search,
    is what makes "the same lead" the same record.
  * Link healing: a record deleted inside the CRM re-creates and re-links
    instead of failing forever.
  * lookup_record reads the uploaded CSV rows LIVE at call time with the honest
    outcome vocabulary (ok / empty / ambiguous) — never a knowledge snapshot.
  * The audit trail records create-vs-update with no lead PII.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest
from app.db.session import tenant_session
from app.models.audit import AdminAuditLog
from app.models.connection import InstanceConnection
from app.models.crm_link import CrmLeadLink
from app.models.instance import Account, AccountState, Instance, LucielState
from app.models.record_source import RecordSourceRecord
from app.models.tool_auth import InstanceToolAuthorization
from app.tools import crm_backend
from app.tools.broker import ToolBroker
from app.tools.contract import Outcome
from app.tools.registry import build_tool
from sqlalchemy import select

pytestmark = [pytest.mark.asyncio]

LIVE_RECORD_URL = "https://records.acme.example/lookup"

LEAD_KEY = "jordan@acme.test"


@dataclass
class _Seeded:
    admin: uuid.UUID
    iid: uuid.UUID
    # The conversation's resolved lead, which OWNS ``LEAD_KEY`` (round 7 WP-3c item 8:
    # a push with no resolved lead is refused, so every push here carries one).
    lead_id: uuid.UUID | None = None


@pytest.fixture(autouse=True)
def _fresh_mock_crm():
    """The mock CRM store is process-global (so one test's second push sees its
    first push); reset it BETWEEN tests so tests stay independent."""
    crm_backend._mock_api = None
    yield
    crm_backend._mock_api = None


async def _seed(
    *,
    crm_provider: str | None = "hubspot",
    record_provider: str | None = None,
    authorize: tuple[str, ...] = ("push_to_crm",),
    token: str = "hs-token",
) -> _Seeded:
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
                state=AccountState.verified,
                email_verified=True,
            )
        )
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
        for tool_id in authorize:
            s.add(
                InstanceToolAuthorization(
                    id=uuid.uuid4(), admin_id=admin, instance_id=iid, tool_id=tool_id, enabled=True
                )
            )
        if crm_provider:
            ref = f"vm/{admin}/{iid}/crm"
            s.add(
                InstanceConnection(
                    connection_id=uuid.uuid4(),
                    admin_id=admin,
                    instance_id=iid,
                    connection_type="crm",
                    auth_class="oauth_token",
                    provider=crm_provider,
                    status="connected",
                    secret_ref=ref,
                )
            )
        if record_provider:
            s.add(
                InstanceConnection(
                    connection_id=uuid.uuid4(),
                    admin_id=admin,
                    instance_id=iid,
                    connection_type="record_source",
                    auth_class="provisioned_resource",
                    provider=record_provider,
                    status="connected",
                    # The live connector (WP-H) reads its endpoint from the row.
                    non_secret_config=(
                        {"url": LIVE_RECORD_URL, "egressAllowlist": ["records.acme.example"]}
                        if record_provider == "api"
                        else None
                    ),
                )
            )
    lead_id: uuid.UUID | None = None
    if crm_provider:
        from app.connections.secrets import get_secret_store
        from app.models.lead import Lead, LeadState

        await get_secret_store().put(f"vm/{admin}/{iid}/crm", {"access_token": token})
        lead_id = uuid.uuid4()
        async with tenant_session(admin) as s:
            s.add(
                Lead(
                    id=lead_id,
                    admin_id=admin,
                    instance_id=iid,
                    contact_identifier=LEAD_KEY,
                    state=LeadState.active,
                )
            )
    return _Seeded(admin=admin, iid=iid, lead_id=lead_id)


async def _push(
    seeded: _Seeded, fields: dict[str, object], *, resolved: bool = True, lead_key: str = LEAD_KEY
):
    tool = build_tool("push_to_crm")
    assert tool is not None
    async with tenant_session(seeded.admin) as s:
        broker = ToolBroker(s)
        return await broker.dispatch(
            tool,
            {"lead_key": lead_key, "fields": fields},
            admin_id=seeded.admin,
            instance_id=seeded.iid,
            resolved_lead_id=seeded.lead_id if resolved else None,
        )


async def test_two_pushes_one_record_second_is_an_update():
    seeded = await _seed()
    first = await _push(seeded, {"budget": "5k"})
    assert first.outcome is Outcome.ok, first.reason
    assert first.payload["action"] == "created"
    record_id = first.payload["crmRecordId"]

    second = await _push(seeded, {"budget": "50k", "phone": "+16045550100"})
    assert second.outcome is Outcome.ok, second.reason
    assert second.payload["action"] == "updated"
    assert second.payload["crmRecordId"] == record_id  # SAME record, updated in place

    api = crm_backend.build_crm_api()
    assert isinstance(api, crm_backend.InMemoryCrmApi)
    assert len(api.records) == 1  # never a duplicate
    assert api.records[record_id]["fields"]["budget"] == "50k"


async def test_the_link_row_pins_the_record_and_survives_healing():
    seeded = await _seed()
    first = await _push(seeded, {"budget": "5k"})
    record_id = first.payload["crmRecordId"]

    async with tenant_session(seeded.admin) as s:
        link = (
            await s.execute(select(CrmLeadLink).where(CrmLeadLink.admin_id == seeded.admin))
        ).scalar_one()
        assert link.lead_key == LEAD_KEY
        assert link.crm_record_id == record_id

    # The record is deleted INSIDE the CRM (out of band). The next push must
    # heal: re-create + re-link, never fail forever on a dead id.
    api = crm_backend.build_crm_api()
    assert isinstance(api, crm_backend.InMemoryCrmApi)
    api.forget(record_id)

    healed = await _push(seeded, {"budget": "75k"})
    assert healed.outcome is Outcome.ok, healed.reason
    assert healed.payload["crmRecordId"] != record_id
    async with tenant_session(seeded.admin) as s:
        link = (
            await s.execute(select(CrmLeadLink).where(CrmLeadLink.admin_id == seeded.admin))
        ).scalar_one()  # still exactly one link row
        assert link.crm_record_id == healed.payload["crmRecordId"]


async def test_upserts_are_audited_without_lead_pii():
    seeded = await _seed()
    await _push(seeded, {"budget": "5k"})
    await _push(seeded, {"budget": "50k"})
    async with tenant_session(seeded.admin) as s:
        events = (
            (
                await s.execute(
                    select(AdminAuditLog).where(
                        AdminAuditLog.admin_id == seeded.admin,
                        AdminAuditLog.event_type == "crm_record_upserted",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert [e.event_payload["action"] for e in events] == ["created", "updated"]
    for e in events:
        assert LEAD_KEY not in str(e.event_payload)  # never the lead's email


async def test_a_push_with_no_resolved_lead_is_refused_and_creates_nothing():
    """Round 7 WP-3c item 8: the key-ownership guard used to run only when a lead was
    resolved, so an anonymous turn could create a CRM contact under any key the
    model chose. No resolved lead → failed / no_resolved_lead, nothing written."""
    seeded = await _seed()
    res = await _push(seeded, {"budget": "5k"}, resolved=False)
    assert res.outcome is Outcome.failed
    assert res.reason == "crm_error: no_resolved_lead"
    api = crm_backend.build_crm_api()
    assert isinstance(api, crm_backend.InMemoryCrmApi)
    assert api.records == {}
    async with tenant_session(seeded.admin) as s:
        assert (
            await s.execute(select(CrmLeadLink).where(CrmLeadLink.admin_id == seeded.admin))
        ).scalar_one_or_none() is None


async def test_two_mock_tenants_never_share_a_crm_record_store():
    """Round 7 WP-3c item 15: the in-memory CRM is keyed by (token, record) like the
    calendar mock. Tenant B's push of the SAME email must create B's own record under
    B's token — never find and PATCH tenant A's record, which the live APIs (one
    token, one portal) can never do either."""
    a = await _seed(token="tok-a")
    b = await _seed(token="tok-b")
    first = await _push(a, {"budget": "5k"})
    assert first.outcome is Outcome.ok, first.reason
    assert first.payload["action"] == "created"
    second = await _push(b, {"budget": "9k"})
    assert second.outcome is Outcome.ok, second.reason
    assert second.payload["action"] == "created"  # B did not "update" A's contact
    assert second.payload["crmRecordId"] != first.payload["crmRecordId"]

    api = crm_backend.build_crm_api()
    assert isinstance(api, crm_backend.InMemoryCrmApi)
    assert set(api.records_for("tok-a")) == {first.payload["crmRecordId"]}
    assert set(api.records_for("tok-b")) == {second.payload["crmRecordId"]}
    assert api.records[first.payload["crmRecordId"]]["fields"]["budget"] == "5k"  # untouched
    # And each tenant's later push still updates ITS OWN record in place.
    again = await _push(b, {"budget": "12k"})
    assert again.payload["action"] == "updated"
    assert again.payload["crmRecordId"] == second.payload["crmRecordId"]
    assert len(api.records) == 2


async def test_salesforce_crm_pushes_with_its_own_link_namespace():
    """Salesforce lead-push is REAL since Phase 6.5 (owner decision 2026-08-23):
    the same upsert/link discipline as HubSpot, with crm_lead_links rows under
    provider="salesforce" so the two CRMs can never claim each other's records."""
    seeded = await _seed(crm_provider="salesforce")
    res = await _push(seeded, {"budget": "5k"})
    assert res.outcome is Outcome.ok
    assert res.payload["action"] == "created"
    again = await _push(seeded, {"budget": "50k"})
    assert again.outcome is Outcome.ok
    assert again.payload["action"] == "updated"
    assert again.payload["crmRecordId"] == res.payload["crmRecordId"]  # never a duplicate
    async with tenant_session(seeded.admin) as s:
        link = (
            await s.execute(select(CrmLeadLink).where(CrmLeadLink.admin_id == seeded.admin))
        ).scalar_one()
        assert link.provider == "salesforce"


async def _seed_records(seeded: _Seeded, rows: list[dict[str, str]]) -> None:
    async with tenant_session(seeded.admin) as s:
        for i, fields in enumerate(rows):
            s.add(
                RecordSourceRecord(
                    admin_id=seeded.admin, instance_id=seeded.iid, ordinal=i, fields=fields
                )
            )


async def _lookup(seeded: _Seeded, query: str):
    tool = build_tool("lookup_record")
    assert tool is not None
    async with tenant_session(seeded.admin) as s:
        broker = ToolBroker(s)
        return await broker.dispatch(
            tool, {"query": query}, admin_id=seeded.admin, instance_id=seeded.iid
        )


async def test_lookup_record_reads_the_uploaded_rows_live():
    seeded = await _seed(crm_provider=None, record_provider="csv", authorize=("lookup_record",))
    await _seed_records(
        seeded,
        [
            {"package": "Starter", "price": "$899", "team": "solo"},
            {"package": "Growth", "price": "$2900", "team": "10-person"},
        ],
    )
    res = await _lookup(seeded, "starter")
    assert res.outcome is Outcome.ok, res.reason
    assert res.payload["record"]["price"] == "$899"

    # empty is a TRUE statement about the admin's data — never a fabrication.
    assert (await _lookup(seeded, "enterprise")).outcome is Outcome.empty

    # several matches → ambiguous with candidates for the customer to pick from.
    ambiguous = await _lookup(seeded, "package")
    assert ambiguous.outcome is Outcome.ambiguous
    assert len(ambiguous.payload["candidates"]) == 2


async def test_lookup_record_on_a_live_api_connector_reads_the_owners_endpoint():
    """Round 6 WP-H (F062): the live connector is real — the owner's endpoint answers
    the query through the egress seam, and the outcomes follow the same vocabulary."""
    from app.tools.record_backend import MockRecordTransport

    MockRecordTransport.reset()
    seeded = await _seed(crm_provider=None, record_provider="api", authorize=("lookup_record",))
    MockRecordTransport.seed(LIVE_RECORD_URL, [{"order": "A-100", "status": "shipped"}])
    res = await _lookup(seeded, "A-100")
    assert res.outcome is Outcome.ok, res.reason
    assert res.payload["record"]["status"] == "shipped"
    MockRecordTransport.seed(LIVE_RECORD_URL, [])
    assert (await _lookup(seeded, "nothing")).outcome is Outcome.empty
    MockRecordTransport.seed(LIVE_RECORD_URL, 503)
    failed = await _lookup(seeded, "anything")
    assert failed.outcome is Outcome.failed
    assert "record_source" in (failed.reason or "")
    MockRecordTransport.reset()
