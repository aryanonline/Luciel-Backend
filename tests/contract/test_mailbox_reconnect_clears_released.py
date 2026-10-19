"""A successful mailbox reconnect consumes the released copy (round 7 M10).

Live defect: a disconnect moves the mailbox address from the routing claim
(``destination``) to ``released_destination`` (round 7 WP-4a item 12). The OAuth
callback of a later reconnect re-established the claim (Graph ``/me`` verified the
same mailbox and the bind wrote ``destination`` again) but never removed the
released marker — so the row carried a live claim AND a "released" copy, and a later
disconnect / restore read stale state beside the live one.
"""

from __future__ import annotations

import uuid
from urllib.parse import parse_qs, urlparse

import pytest
from app.channels import routing
from app.connections.service import RELEASED_DESTINATION_KEY
from app.db.session import tenant_session
from app.models.audit import AdminAuditLog
from sqlalchemy import select

from tests.contract.test_outlook_mailbox_connect import (
    MAILBOX,
    _configure_microsoft,
    _fake_microsoft_transport,
    _ready_account_with_luciel,
    _row,
)

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]


async def _connect_outlook(client) -> str:
    r = await client.post(
        "/api/v1/admin/connections",
        json={"connectionType": "email_sender", "provider": "outlook"},
    )
    assert r.status_code == 200, r.text
    state = parse_qs(urlparse(r.json()["authorizeUrl"]).query)["state"][0]
    listed = (await client.get("/api/v1/admin/connections")).json()
    connection_id = next(c["connectionId"] for c in listed if c["connectionType"] == "email_sender")
    cb = await client.post(
        f"/api/v1/admin/connections/{connection_id}/oauth-callback",
        json={"code": f"auth-{uuid.uuid4().hex[:6]}", "state": state},
    )
    assert cb.status_code == 200, cb.text
    return connection_id


async def _released_on_disconnect(admin_id: uuid.UUID) -> list[object]:
    async with tenant_session(admin_id) as s:
        rows = (
            await s.execute(
                select(AdminAuditLog)
                .where(
                    AdminAuditLog.admin_id == admin_id,
                    AdminAuditLog.event_type == "connection_status_changed",
                )
                .order_by(AdminAuditLog.seq.asc())
            )
        ).scalars()
        return [
            (e.event_payload or {}).get("released_destination")
            for e in rows
            if (e.event_payload or {}).get("reason") == "disconnect"
        ]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_outlook_reconnect_clears_the_released_marker(client, monkeypatch):
    admin_id = await _ready_account_with_luciel(client)
    _configure_microsoft(monkeypatch)
    _fake_microsoft_transport(monkeypatch)
    connection_id = await _connect_outlook(client)

    assert (await client.post(f"/api/v1/admin/connections/{connection_id}/disconnect")).is_success
    cfg = (await _row(admin_id)).non_secret_config or {}
    assert "destination" not in cfg
    assert cfg[RELEASED_DESTINATION_KEY] == MAILBOX

    # Reconnect the same mailbox: the claim is re-established through verify + bind...
    assert await _connect_outlook(client) == connection_id
    row = await _row(admin_id)
    cfg = row.non_secret_config or {}
    assert row.status == "connected"
    assert cfg["destination"] == MAILBOX
    # ...and the released copy is gone — the claim is live, nothing is "released".
    assert RELEASED_DESTINATION_KEY not in cfg
    async with tenant_session(admin_id) as s:
        assert await routing.owner_of_destination(s, "email_sender", MAILBOX) == admin_id

    # A later disconnect reads the LIVE claim (not a stale marker) and releases it.
    assert (await client.post(f"/api/v1/admin/connections/{connection_id}/disconnect")).is_success
    assert await _released_on_disconnect(admin_id) == [MAILBOX, MAILBOX]
    cfg = (await _row(admin_id)).non_secret_config or {}
    assert cfg[RELEASED_DESTINATION_KEY] == MAILBOX
    assert "destination" not in cfg


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_sms_re_verify_promotion_clears_a_released_marker():
    """The re-verify path is a claim point too: a pending number that probes operable
    becomes the routable claim, and a released copy left on the row (a row written
    before attach consumed the marker) must not survive beside the live claim."""
    from app.channels.number_operability import NullOperabilityProbe
    from app.channels.phone_lifecycle import attach_sms_number, reverify_sms_number

    from tests.integration.test_number_operability import NUM_A, _seed

    admin = uuid.uuid4()
    iid = await _seed(admin)
    async with tenant_session(admin) as s:
        conn = await attach_sms_number(
            s,
            admin_id=admin,
            instance_id=iid,
            e164_number=NUM_A,
            probe=NullOperabilityProbe(operable_numbers=set()),
        )
        # The stale state: a pending (unproven) number with a released copy beside it.
        conn.non_secret_config = {
            **(conn.non_secret_config or {}),
            RELEASED_DESTINATION_KEY: NUM_A,
        }
        await s.flush()
        assert "destination" not in conn.non_secret_config

        promoted = await reverify_sms_number(
            s, admin_id=admin, instance_id=iid, probe=NullOperabilityProbe(operable_numbers={NUM_A})
        )
        assert promoted is not None
        cfg = promoted.non_secret_config or {}
        assert cfg["destination"] == NUM_A
        assert "pending_destination" not in cfg
        assert RELEASED_DESTINATION_KEY not in cfg
        assert await routing.owner_of_destination(s, "sms_sender", NUM_A) == admin
