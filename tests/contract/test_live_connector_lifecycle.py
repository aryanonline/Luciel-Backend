"""Round 7 WP-4a item 1: the record-source live connector is connected under the id the
connect flow writes, proven at connect time, and dispatched under that same id.

Round 6 WP-H built ``LiveRecordBackend`` and a connect-time proof but keyed both on a
provider id (``api``) the registry never offered: the registry registered
``live_connector``, so every live connector connected green with no proof and every
``lookup_record`` fell through to "unavailable". These tests drive the REAL id end to
end — connect form → authenticated proof → connected row → tool dispatch — and pin the
proof's honest boundary (2xx must be records; 401/403 refuses; other 4xx is reachable;
5xx/transport fails the connect).
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = [pytest.mark.contract]

URL = "https://records.acme.example/lookup"


async def _ready_account_with_luciel(client) -> uuid.UUID:
    from app.auth import access
    from app.auth.tokens import decode_token
    from app.config import get_settings

    email = f"lc-{uuid.uuid4().hex[:8]}@x.com"
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


@pytest.fixture(autouse=True)
def _reset_record_transport():
    from app.tools.record_backend import MockRecordTransport

    MockRecordTransport.reset()
    yield
    MockRecordTransport.reset()


async def _start_live_connector(client) -> str:
    r = await client.post(
        "/api/v1/admin/connections",
        json={"connectionType": "record_source", "provider": "live_connector"},
    )
    assert r.status_code == 200, r.text
    assert r.json().get("requiresClientForm") is True
    rows = (await client.get("/api/v1/admin/connections")).json()
    return next(c for c in rows if c["connectionType"] == "record_source")["connectionId"]


async def _submit(client, cid: str):
    return await client.post(
        f"/api/v1/admin/connections/{cid}/credentials",
        json={"fields": {"url": URL, "api_key": "k-1"}},
    )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_connect_flow_proves_the_endpoint_and_the_tool_dispatches_to_it(client):
    from app.db.session import tenant_session
    from app.models.instance import Instance
    from app.models.tool_auth import InstanceToolAuthorization
    from app.tools.broker import ToolBroker
    from app.tools.contract import Outcome
    from app.tools.record_backend import MockRecordTransport
    from app.tools.registry import build_tool
    from sqlalchemy import select

    admin_id = await _ready_account_with_luciel(client)
    cid = await _start_live_connector(client)
    MockRecordTransport.seed(URL, [{"order": "A-100", "status": "shipped"}])

    r = await _submit(client, cid)
    assert r.status_code == 200, r.text
    row = r.json()
    assert row["provider"] == "live_connector"
    assert row["status"] == "connected"
    # The API key went to the SecretStore, never onto the row.
    assert "api_key" not in (row["nonSecretConfig"] or {})
    assert row["nonSecretConfig"]["url"] == URL

    # The SAME id the connect flow wrote is what lookup_record dispatches on.
    async with tenant_session(admin_id) as s:
        iid = (
            await s.execute(select(Instance.id).where(Instance.admin_id == admin_id))
        ).scalar_one()
        s.add(
            InstanceToolAuthorization(
                id=uuid.uuid4(),
                admin_id=admin_id,
                instance_id=iid,
                tool_id="lookup_record",
                enabled=True,
            )
        )
    tool = build_tool("lookup_record")
    assert tool is not None
    async with tenant_session(admin_id) as s:
        res = await ToolBroker(s).dispatch(
            tool, {"query": "A-100"}, admin_id=admin_id, instance_id=iid
        )
    assert res.outcome is Outcome.ok, res.reason
    assert res.payload["record"]["status"] == "shipped"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_endpoint_that_refuses_the_key_fails_the_connect_honestly(client):
    from app.tools.record_backend import MockRecordTransport

    await _ready_account_with_luciel(client)
    cid = await _start_live_connector(client)
    MockRecordTransport.seed(URL, 401)

    r = await _submit(client, cid)
    assert r.status_code == 422, r.text
    assert "refused the API key" in r.json()["message"]
    row = next(
        c
        for c in (await client.get("/api/v1/admin/connections")).json()
        if c["connectionType"] == "record_source"
    )
    assert row["status"] == "unconfigured", "a refused key must never mint a Connected row"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_reachable_endpoint_with_no_such_record_connects(client):
    """A 404 for the synthetic probe key is a correct answer: the endpoint is up and the
    key was accepted. Only 401/403 (refused) and 5xx/transport (down) fail the connect."""
    from app.tools.record_backend import MockRecordTransport

    await _ready_account_with_luciel(client)
    cid = await _start_live_connector(client)
    MockRecordTransport.seed(URL, 404)

    r = await _submit(client, cid)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "connected"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
@pytest.mark.parametrize("seed", [503, "<html>not a records endpoint</html>"])
async def test_a_down_or_non_records_endpoint_fails_the_connect(client, seed):
    from app.tools.record_backend import MockRecordTransport

    await _ready_account_with_luciel(client)
    cid = await _start_live_connector(client)
    MockRecordTransport.seed(URL, seed)

    r = await _submit(client, cid)
    assert r.status_code == 422, r.text
    row = next(
        c
        for c in (await client.get("/api/v1/admin/connections")).json()
        if c["connectionType"] == "record_source"
    )
    assert row["status"] == "unconfigured"
