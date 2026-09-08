"""Round 6 WP-H (F055): the owner picks which Calendly event type customers book, and
sees the account's event types to pick from."""

from __future__ import annotations

import uuid

import pytest
from app.db.session import tenant_session
from app.models.connection import InstanceConnection
from app.tools import calendly_backend as cb
from sqlalchemy import select

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]

USER = "https://api.calendly.com/users/U-set"
ET_A = "https://api.calendly.com/event_types/A"
ET_B = "https://api.calendly.com/event_types/B"


@pytest.fixture(autouse=True)
def _reset():
    cb.InMemoryCalendlyApi.reset()
    yield
    cb.InMemoryCalendlyApi.reset()


async def _ready(client) -> uuid.UUID:
    from app.auth import access
    from app.auth.tokens import decode_token
    from app.config import get_settings

    email = f"cal-{uuid.uuid4().hex[:8]}@x.com"
    await client.post(
        "/api/v1/auth/signup", json={"email": email, "password": "pw-123456", "captchaToken": "t"}
    )
    _a, token = await access.signup(email, "pw-123456")
    await client.post("/api/v1/auth/verify-email", json={"token": token})
    await client.post("/api/v1/auth/login", json={"email": email, "password": "pw-123456"})
    await client.post(
        "/api/v1/admin/luciel", json={"name": "L", "websiteUrl": "x.com", "businessOneLiner": "b"}
    )
    return uuid.UUID(decode_token(client.cookies.get(get_settings().session_cookie_name))["sub"])


async def _calendly_row(client, admin_id: uuid.UUID) -> str:
    """A connected Calendly row the way the OAuth callback + verifier leave it."""
    from app.connections.secrets import get_secret_store
    from app.models.instance import Instance

    async with tenant_session(admin_id) as s:
        iid = (
            await s.execute(select(Instance.id).where(Instance.admin_id == admin_id))
        ).scalar_one()
        cid = uuid.uuid4()
        ref = f"vm/{admin_id}/{iid}/calendar"
        s.add(
            InstanceConnection(
                connection_id=cid,
                admin_id=admin_id,
                instance_id=iid,
                connection_type="calendar",
                auth_class="oauth_token",
                provider="calendly",
                status="connected",
                secret_ref=ref,
                non_secret_config={"userUri": USER, "eventTypeUri": ET_A, "eventTypeName": "Intro"},
            )
        )
        await s.commit()
    await get_secret_store().put(ref, {"access_token": "cal-token"})
    return str(cid)


def _types():
    return [
        cb.CalendlyEventType(ET_A, "Intro call", True, 15, "https://calendly.com/acme/intro"),
        cb.CalendlyEventType(ET_B, "Site visit", True, 60, "https://calendly.com/acme/visit"),
    ]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_event_types_are_listed_with_the_chosen_one_marked(client):
    admin_id = await _ready(client)
    cid = await _calendly_row(client, admin_id)
    cb.InMemoryCalendlyApi.seed_event_types(USER, _types())
    r = await client.get(f"/api/v1/admin/connections/{cid}/calendly/event-types")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["chosenUri"] == ET_A
    assert [t["name"] for t in body["eventTypes"]] == ["Intro call", "Site visit"]
    assert body["eventTypes"][1]["durationMinutes"] == 60


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_owner_can_switch_the_event_type_and_a_foreign_one_is_refused(client):
    admin_id = await _ready(client)
    cid = await _calendly_row(client, admin_id)
    cb.InMemoryCalendlyApi.seed_event_types(USER, _types())
    r = await client.put(f"/api/v1/admin/connections/{cid}/settings", json={"eventTypeUri": ET_B})
    assert r.status_code == 200, r.text
    assert r.json()["nonSecretConfig"]["eventTypeUri"] == ET_B
    assert r.json()["nonSecretConfig"]["eventTypeName"] == "Site visit"
    r = await client.put(
        f"/api/v1/admin/connections/{cid}/settings",
        json={"eventTypeUri": "https://api.calendly.com/event_types/NOT-MINE"},
    )
    assert r.status_code == 422
    assert "not one your Calendly account offers" in r.text
    r = await client.put(f"/api/v1/admin/connections/{cid}/settings", json={"eventTypeUri": ""})
    assert r.status_code == 422
    r = await client.put(f"/api/v1/admin/connections/{cid}/settings", json={"gadget": 1})
    assert r.status_code == 422


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_settings_on_a_non_calendly_connection_are_refused(client):
    await _ready(client)
    r = await client.post(
        "/api/v1/admin/connections", json={"connectionType": "crm", "provider": "custom_webhook"}
    )
    assert r.status_code in (200, 201), r.text
    rows = (await client.get("/api/v1/admin/connections")).json()
    cid = next(x["connectionId"] for x in rows if x["connectionType"] == "crm")
    r = await client.put(f"/api/v1/admin/connections/{cid}/settings", json={"eventTypeUri": ET_A})
    assert r.status_code == 422
    assert "no settings to change here" in r.text
