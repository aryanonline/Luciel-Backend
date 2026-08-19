"""Disable→enable round trip must NOT sever a channel's backing connection, and the
channels pillar must reflect that connection's LIVE status (owner bug report / fix).

Root cause (documented in ``app.connections.gate.channel_connection_statuses`` and
``app.api.v1.admin.luciel._merge_live_channel_status``): the channels pillar's
``connectionStatus`` was written ONLY by the client's own PUT echo and by
``update_channels``' full-replace — nothing on the backend ever synced it from the
actual ``instance_connections`` row an attach/OAuth-callback updates. A freshly BYO'd
Twilio number (or any other channel-backing connection) was therefore invisible to the
dashboard: GET always echoed back the JSONB's stale/default status, so toggling SMS off
then on looked, from the dashboard's point of view, exactly like a channel that had
never been connected — prompting the owner to "reconnect" a number that the backend had
never touched.

These tests pin two properties:
  1. The backend NEVER touches the ``sms_sender`` connection row across a channel
     disable/enable round trip (this was already true; asserted here so a regression
     is caught).
  2. GET/PUT responses reflect the connection's LIVE status, before and after the
     round trip — so the dashboard has no reason to ask for the number again.

The identical property holds for whatsapp + messenger (``channel_auth``),
instagram (``instagram_auth``), and email (``email_sender``) — every channel
with a backing connection, not just SMS.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]

NUMBER = "+14155550188"


def _number_on_file(conn) -> str | None:
    """The owner's number survives toggles wherever it lives: as the routable
    claim (destination) or, in mock mode where the operability probe never
    confirms, parked under pending_destination (c21 anti-squatting)."""
    cfg = conn.non_secret_config or {}
    return cfg.get("destination") or cfg.get("pending_destination")



async def _signup_verify_login(client) -> uuid.UUID:
    from app.auth import access
    from app.auth.tokens import decode_token
    from app.config import get_settings

    email = f"toggle-{uuid.uuid4().hex[:8]}@x.com"
    await client.post(
        "/api/v1/auth/signup", json={"email": email, "password": "pw-123456", "captchaToken": "t"}
    )
    _acct, token = await access.signup(email, "pw-123456")
    await client.post("/api/v1/auth/verify-email", json={"token": token})
    await client.post("/api/v1/auth/login", json={"email": email, "password": "pw-123456"})
    return uuid.UUID(decode_token(client.cookies.get(get_settings().session_cookie_name))["sub"])


async def _create(client) -> None:
    await client.post(
        "/api/v1/admin/luciel",
        json={"name": "L", "websiteUrl": "x.com", "businessOneLiner": "b"},
    )


async def _instance_id(admin_id: uuid.UUID) -> uuid.UUID:
    from app.db.session import tenant_session
    from app.models.instance import Instance

    async with tenant_session(admin_id) as s:
        return (
            await s.execute(select(Instance.id).where(Instance.admin_id == admin_id))
        ).scalar_one()


def _channel(response, channel_id: str) -> dict:
    return next(c for c in response.json()["channels"] if c["id"] == channel_id)


def _toggle(channels: list[dict], channel_id: str, *, enabled: bool) -> list[dict]:
    return [{**c, "enabled": enabled} if c["id"] == channel_id else c for c in channels]


async def _connection_row(admin_id: uuid.UUID, instance_id: uuid.UUID, connection_type: str):
    from app.db.session import tenant_session
    from app.models.connection import InstanceConnection

    async with tenant_session(admin_id) as s:
        return (
            await s.execute(
                select(InstanceConnection).where(
                    InstanceConnection.admin_id == admin_id,
                    InstanceConnection.instance_id == instance_id,
                    InstanceConnection.connection_type == connection_type,
                )
            )
        ).scalar_one_or_none()


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_sms_disable_enable_round_trip_preserves_connection_and_number(client):
    """The owner-reported bug: BYO Twilio + number attached, toggle off/on must NOT
    ask to reconnect the number, and the connection row must be untouched throughout."""
    admin_id = await _signup_verify_login(client)
    await _create(client)
    instance_id = await _instance_id(admin_id)

    r1 = await client.put(
        "/api/v1/admin/luciel/channels",
        json={"channels": [{"id": "widget", "enabled": True}, {"id": "sms", "enabled": True}]},
    )
    assert r1.status_code == 200

    attach = await client.post(
        "/api/v1/admin/connections",
        json={"connectionType": "sms_sender", "provider": "twilio", "phoneNumber": NUMBER},
    )
    assert attach.status_code == 200

    conn_before = await _connection_row(admin_id, instance_id, "sms_sender")
    assert conn_before is not None
    assert _number_on_file(conn_before) == NUMBER
    connection_id_before = conn_before.connection_id
    status_before = conn_before.status

    g = await client.get("/api/v1/admin/luciel")
    sms = _channel(g, "sms")
    # Live status must be visible immediately after attach — not "unconfigured".
    assert sms["connectionStatus"] != "unconfigured"
    live_status_seen = sms["connectionStatus"]

    # Disable SMS exactly as the frontend toggle does: full-replace PUT, enabled=false.
    off_channels = _toggle(g.json()["channels"], "sms", enabled=False)
    r_off = await client.put("/api/v1/admin/luciel/channels", json={"channels": off_channels})
    assert r_off.status_code == 200
    sms_off = _channel(r_off, "sms")
    assert sms_off["enabled"] is False
    # Disabling must NOT wipe the connection's live status from view.
    assert sms_off["connectionStatus"] == live_status_seen

    # The connection row itself must be completely untouched by the disable.
    conn_after_off = await _connection_row(admin_id, instance_id, "sms_sender")
    assert conn_after_off.connection_id == connection_id_before
    assert conn_after_off.status == status_before
    assert _number_on_file(conn_after_off) == NUMBER

    # Re-enable: must restore SILENTLY — no "add your number" state, connection intact.
    on_channels = _toggle(off_channels, "sms", enabled=True)
    r_on = await client.put("/api/v1/admin/luciel/channels", json={"channels": on_channels})
    assert r_on.status_code == 200
    sms_on = _channel(r_on, "sms")
    assert sms_on["enabled"] is True
    assert sms_on["connectionStatus"] == live_status_seen
    assert sms_on["connectionStatus"] != "unconfigured"

    conn_after_on = await _connection_row(admin_id, instance_id, "sms_sender")
    assert conn_after_on.connection_id == connection_id_before
    assert conn_after_on.status == status_before
    assert _number_on_file(conn_after_on) == NUMBER

    # A GET after the round trip must agree with the PUT response (single source of truth).
    g_final = await client.get("/api/v1/admin/luciel")
    assert _channel(g_final, "sms")["connectionStatus"] == live_status_seen


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_sms_operable_number_shows_pending_carrier_registration_after_round_trip(client):
    """An OPERABLE BYO number must read pending_carrier_registration (never dropped to
    unconfigured/error) across a disable/enable round trip."""
    from app.channels import phone_lifecycle
    from app.channels.number_operability import NullOperabilityProbe
    from app.db.session import tenant_session

    admin_id = await _signup_verify_login(client)
    await _create(client)
    instance_id = await _instance_id(admin_id)

    await client.put(
        "/api/v1/admin/luciel/channels",
        json={"channels": [{"id": "widget", "enabled": True}, {"id": "sms", "enabled": True}]},
    )

    async with tenant_session(admin_id) as s:
        probe = NullOperabilityProbe(operable_numbers={NUMBER})
        await phone_lifecycle.attach_sms_number(
            s, admin_id=admin_id, instance_id=instance_id, e164_number=NUMBER, probe=probe
        )
        await s.commit()

    g = await client.get("/api/v1/admin/luciel")
    assert _channel(g, "sms")["connectionStatus"] == "pending_carrier_registration"

    off = _toggle(g.json()["channels"], "sms", enabled=False)
    r_off = await client.put("/api/v1/admin/luciel/channels", json={"channels": off})
    assert _channel(r_off, "sms")["connectionStatus"] == "pending_carrier_registration"

    on = _toggle(off, "sms", enabled=True)
    r_on = await client.put("/api/v1/admin/luciel/channels", json={"channels": on})
    assert _channel(r_on, "sms")["connectionStatus"] == "pending_carrier_registration"
    assert _channel(r_on, "sms")["enabled"] is True


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_widget_channel_has_no_live_status_overlay(client):
    """Widget has no backing connection — its (absent) connectionStatus must be left
    exactly as stored, never coerced to a connection-derived value."""
    admin_id = await _signup_verify_login(client)
    await _create(client)
    del admin_id

    g = await client.get("/api/v1/admin/luciel")
    widget = _channel(g, "widget")
    assert widget.get("connectionStatus") is None


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_email_channel_reflects_live_connection_status_across_toggle(client):
    """Same property as SMS, for the email_sender-backed ``email`` channel: a
    provisioned address must not read as reconnect-needed after a disable/enable."""
    from app.connections import service as conn_service
    from app.db.session import tenant_session

    admin_id = await _signup_verify_login(client)
    await _create(client)
    instance_id = await _instance_id(admin_id)

    await client.put(
        "/api/v1/admin/luciel/channels",
        json={"channels": [{"id": "widget", "enabled": True}, {"id": "email", "enabled": True}]},
    )

    async with tenant_session(admin_id) as s:
        await conn_service.provision_email(
            s,
            admin_id=admin_id,
            instance_id=instance_id,
            mode="vm_subdomain",
            email_address=None,
            instance_name="L",
            user_id=None,
        )
        await s.commit()

    g = await client.get("/api/v1/admin/luciel")
    email_ch = _channel(g, "email")
    assert email_ch["connectionStatus"] == "connected"

    off = _toggle(g.json()["channels"], "email", enabled=False)
    r_off = await client.put("/api/v1/admin/luciel/channels", json={"channels": off})
    assert _channel(r_off, "email")["connectionStatus"] == "connected"

    on = _toggle(off, "email", enabled=True)
    r_on = await client.put("/api/v1/admin/luciel/channels", json={"channels": on})
    assert _channel(r_on, "email")["connectionStatus"] == "connected"
    assert _channel(r_on, "email")["enabled"] is True

    conn = await _connection_row(admin_id, instance_id, "email_sender")
    assert conn is not None
    assert conn.non_secret_config["mode"] == "vm_subdomain"
