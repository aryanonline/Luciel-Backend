"""Round 6 WP-D (item 10): everything the owner connected or acknowledged can be
taken back from the API, honestly and audited — a number without the account, an
attestation, the CSV records, the voice and SMS acknowledgements — and a channel
switched off takes its send tool with it server-side (F163). Closing the account
while a Luciel is live needs an explicit confirmation (F156)."""

from __future__ import annotations

import io
import uuid

import pytest
from app.channels.tenant_twilio import MOCK_TENANT_NUMBERS

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]

# A number the mock Twilio account holds, so the operability probe confirms it.
NUMBER = MOCK_TENANT_NUMBERS[0].phone_number


async def _ready(client) -> uuid.UUID:
    from app.auth import access
    from app.auth.tokens import decode_token
    from app.config import get_settings

    email = f"maint-{uuid.uuid4().hex[:8]}@x.com"
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


async def _sms_row(client) -> dict:
    rows = (await client.get("/api/v1/admin/connections")).json()
    return next(r for r in rows if r["connectionType"] == "sms_sender")


async def _connect_twilio_with_number(client, admin_id: uuid.UUID) -> str:
    """Twilio account via the API, then the number attached with an OPERABLE mock probe —
    the mock probe only confirms numbers it is told about."""
    from app.channels import phone_lifecycle
    from app.channels.number_operability import NullOperabilityProbe
    from app.db.session import tenant_session
    from app.models.instance import Instance
    from sqlalchemy import select

    r = await client.post(
        "/api/v1/admin/connections", json={"connectionType": "sms_sender", "provider": "twilio"}
    )
    assert r.status_code in (200, 201), r.text
    cid = (await _sms_row(client))["connectionId"]
    r = await client.post(
        f"/api/v1/admin/connections/{cid}/credentials",
        json={"fields": {"accountSid": "AC" + "0" * 32, "authToken": "tok"}},
    )
    assert r.status_code == 200, r.text
    async with tenant_session(admin_id) as s:
        instance_id = (
            await s.execute(select(Instance.id).where(Instance.admin_id == admin_id))
        ).scalar_one()
        await phone_lifecycle.attach_sms_number(
            s,
            admin_id=admin_id,
            instance_id=instance_id,
            e164_number=NUMBER,
            probe=NullOperabilityProbe(operable_numbers={NUMBER}),
        )
        await s.commit()
    return str(cid)


async def _channels(client) -> dict[str, dict]:
    return {c["id"]: c for c in (await client.get("/api/v1/admin/luciel")).json()["channels"]}


async def _tools(client) -> dict[str, dict]:
    return {t["id"]: t for t in (await client.get("/api/v1/admin/luciel")).json()["tools"]}


async def _put_channel(client, channel: str, enabled: bool):
    channels = list((await client.get("/api/v1/admin/luciel")).json()["channels"])
    body = [
        {"id": c["id"], "enabled": enabled if c["id"] == channel else c["enabled"]}
        for c in channels
    ]
    return await client.put("/api/v1/admin/luciel/channels", json={"channels": body})


async def _put_tool(client, tool: str, enabled: bool):
    tools = list((await client.get("/api/v1/admin/luciel")).json()["tools"])
    body = [{"id": t["id"], "enabled": enabled if t["id"] == tool else t["enabled"]} for t in tools]
    return await client.put("/api/v1/admin/luciel/tools", json={"tools": body})


# --- the number ---------------------------------------------------------------------


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_number_can_be_removed_without_losing_the_twilio_account(client):
    admin_id = await _ready(client)
    await _connect_twilio_with_number(client, admin_id)
    before = await _sms_row(client)
    assert before["status"] == "pending_carrier_registration"
    assert before["nonSecretConfig"]["destination"] == NUMBER

    r = await client.delete("/api/v1/admin/connections/sms/number")
    assert r.status_code == 200, r.text
    row = r.json()
    assert row["status"] == "unconfigured"
    assert "destination" not in row["nonSecretConfig"]
    assert "pending_destination" not in row["nonSecretConfig"]
    # The account stays: the Account SID is the non-secret half of the credential.
    assert row["nonSecretConfig"]["accountSid"] == "AC" + "0" * 32
    # Nothing to remove twice.
    again = await client.delete("/api/v1/admin/connections/sms/number")
    assert again.status_code == 422
    # The channel rows read the honest "add your number" state.
    assert (await _channels(client))["sms"]["connectionStatus"] == "unconfigured"
    # A fresh designate works again on the same account.
    await _connect_twilio_with_number(client, admin_id)
    assert (await _sms_row(client))["nonSecretConfig"]["destination"] == NUMBER


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_remove_number_without_an_sms_row_is_404(client):
    await _ready(client)
    assert (await client.delete("/api/v1/admin/connections/sms/number")).status_code == 404


# --- the attestation --------------------------------------------------------------------


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_attestation_can_be_withdrawn_and_texting_goes_back_behind_the_gate(client):
    admin_id = await _ready(client)
    await _connect_twilio_with_number(client, admin_id)
    r = await client.post("/api/v1/admin/connections/sms/attest-registration")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "connected"

    r = await client.delete("/api/v1/admin/connections/sms/attest-registration")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending_carrier_registration"
    assert r.json()["nonSecretConfig"]["destination"] == NUMBER
    # Idempotent: withdrawing twice is not an error and changes nothing.
    r = await client.delete("/api/v1/admin/connections/sms/attest-registration")
    assert r.status_code == 200 and r.json()["status"] == "pending_carrier_registration"
    # Attesting again works.
    assert (await client.post("/api/v1/admin/connections/sms/attest-registration")).json()[
        "status"
    ] == "connected"


# --- the CSV records ---------------------------------------------------------------------


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_clearing_the_csv_hands_the_record_source_back_and_switches_lookup_off(client):
    await _ready(client)
    csv = io.BytesIO(b"name,email\nDana,dana@lead.example\nSam,sam@lead.example\n")
    r = await client.post(
        "/api/v1/admin/connections/record-source/csv",
        files={"file": ("records.csv", csv, "text/csv")},
    )
    assert r.status_code == 200, r.text
    assert r.json()["records"] == 2
    r = await _put_tool(client, "lookup_record", True)
    assert r.status_code == 200, r.text

    r = await client.delete("/api/v1/admin/connections/record-source/csv")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["connection"]["status"] == "not_connected"
    assert out["connection"]["statusDetail"] == "No records on file"
    assert "lookup_record" in out["disabledTools"]
    tool = (await _tools(client))["lookup_record"]
    assert tool["enabled"] is False
    # Uploading again re-arms the source.
    csv = io.BytesIO(b"name\nDana\n")
    r = await client.post(
        "/api/v1/admin/connections/record-source/csv",
        files={"file": ("records.csv", csv, "text/csv")},
    )
    assert r.status_code == 200 and r.json()["records"] == 1


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_clearing_records_that_were_never_uploaded_is_404(client):
    await _ready(client)
    assert (await client.delete("/api/v1/admin/connections/record-source/csv")).status_code == 404


# --- the acknowledgements -------------------------------------------------------------


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_voice_consent_can_be_withdrawn_and_voice_needs_it_again(client):
    await _ready(client)
    assert (await client.post("/api/v1/admin/luciel/voice-consent")).status_code == 200
    r = await _put_channel(client, "voice", True)
    assert r.status_code == 200, r.text
    assert (await _channels(client))["voice"]["enabled"] is True

    r = await client.delete("/api/v1/admin/luciel/voice-consent")
    assert r.status_code == 200, r.text
    voice = {c["id"]: c for c in r.json()["channels"]}["voice"]
    assert voice["enabled"] is False
    assert voice.get("voiceConsentAcknowledgedAt") is None
    # Turning voice on again is refused until the consent is acknowledged again.
    assert (await _put_channel(client, "voice", True)).status_code == 409
    assert (await client.post("/api/v1/admin/luciel/voice-consent")).status_code == 200
    assert (await _put_channel(client, "voice", True)).status_code == 200


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_sms_ack_withdrawal_switches_sms_and_send_sms_off_together(client):
    await _ready(client)
    r = await _put_channel(client, "sms", True)  # the enabling write IS the acknowledgement
    assert r.status_code == 200, r.text
    assert (await _channels(client))["sms"].get("smsComplianceAcknowledgedAt")
    assert (await _put_tool(client, "send_sms", True)).status_code == 200

    r = await client.delete("/api/v1/admin/luciel/sms-compliance-ack")
    assert r.status_code == 200, r.text
    out = r.json()
    sms = {c["id"]: c for c in out["channels"]}["sms"]
    assert sms["enabled"] is False
    assert sms.get("smsComplianceAcknowledgedAt") is None
    send_sms = {t["id"]: t for t in out["tools"]}["send_sms"]
    assert send_sms["enabled"] is False
    assert send_sms["disabledReason"] == "sms_channel_disabled"
    # Enabling SMS again stamps a fresh acknowledgement (the modal's write).
    r = await _put_channel(client, "sms", True)
    assert r.status_code == 200
    assert (await _channels(client))["sms"].get("smsComplianceAcknowledgedAt")


# --- F163: the cascade is the server's ---------------------------------------------------


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_turning_a_channel_off_takes_its_send_tool_with_it_server_side(client):
    await _ready(client)
    assert (await _put_channel(client, "email", True)).status_code == 200
    assert (await _put_tool(client, "send_email", True)).status_code == 200
    assert (await _tools(client))["send_email"]["enabled"] is True

    r = await _put_channel(client, "email", False)
    assert r.status_code == 200, r.text
    tool = {t["id"]: t for t in r.json()["tools"]}["send_email"]
    assert tool["enabled"] is False
    assert tool["disabledReason"] == "email_channel_disabled"
    # Turning the channel back on does not silently re-enable the tool: the owner
    # re-enables it, which clears the platform-owned reason.
    assert (await _put_channel(client, "email", True)).status_code == 200
    assert (await _tools(client))["send_email"]["enabled"] is False
    assert (await _put_tool(client, "send_email", True)).status_code == 200
    assert (await _tools(client))["send_email"]["enabled"] is True


# --- knowledge crawl dedupe ---------------------------------------------------------------


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_crawling_the_same_site_twice_is_one_connection(client):
    await _ready(client)
    body = {"provider": "website_crawl", "crawlUrls": ["https://acme.example/"]}
    first = await client.post("/api/v1/admin/knowledge/sync-connections", json=body)
    assert first.status_code == 201, first.text
    second = await client.post("/api/v1/admin/knowledge/sync-connections", json=body)
    assert second.status_code == 200, second.text
    assert second.json()["connectionId"] == first.json()["connectionId"]
    other = await client.post(
        "/api/v1/admin/knowledge/sync-connections",
        json={"provider": "website_crawl", "crawlUrls": ["https://acme.example/pricing"]},
    )
    assert other.status_code == 201
    assert other.json()["connectionId"] != first.json()["connectionId"]
    rows = (await client.get("/api/v1/admin/knowledge/sync-connections")).json()
    assert len(rows) == 2


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_site_removed_and_crawled_again_is_live_again(client):
    """ "Remove crawl" leaves the connection row not_connected (a disconnect, not a
    revocation). Crawling the same site again used to hand that dead row back as-is
    (200): its pages synced, but the row never read connected, so the pillar offered no
    Re-sync - found by the first live walk on dev, 2026-10-19."""
    await _ready(client)
    body = {"provider": "website_crawl", "crawlUrls": ["https://acme.example/"]}
    first = await client.post("/api/v1/admin/knowledge/sync-connections", json=body)
    assert first.status_code == 201, first.text
    cid = first.json()["connectionId"]
    gone = await client.post(f"/api/v1/admin/connections/{cid}/disconnect")
    assert gone.status_code in (200, 204), gone.text
    listed = {c["connectionId"]: c for c in (await client.get("/api/v1/admin/connections")).json()}
    assert listed[cid]["status"] != "connected"

    again = await client.post("/api/v1/admin/knowledge/sync-connections", json=body)
    assert again.status_code == 200, again.text
    assert again.json()["connectionId"] == cid
    assert again.json()["status"] == "connected"
    listed = {c["connectionId"]: c for c in (await client.get("/api/v1/admin/connections")).json()}
    assert listed[cid]["status"] == "connected"


# --- F156: closing needs the Luciel's deletion confirmed ------------------------------------


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_closing_with_a_live_luciel_needs_confirmation(client):
    await _ready(client)
    r = await client.post("/api/v1/admin/account/close")
    assert r.status_code == 409, r.text
    assert "Delete it first" in r.text
    r = await client.post("/api/v1/admin/account/close", json={"confirmDeleteLuciel": False})
    assert r.status_code == 409
    assert (await client.get("/api/v1/admin/luciel")).json()["state"] == "active"
