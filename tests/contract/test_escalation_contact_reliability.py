"""Round 5B item 13 — the hot-lead black hole, closed end to end.

SES accepting a message proves nothing: a typo'd-but-deliverable-looking
escalation contact swallowed every hand-off notification with a green audit
trail, because acceptance suppressed the owner fallback and the retry queue
alike, and no bounce was recorded anywhere. These pin the wire halves: the SES
events webhook records bounces under the right tenant, a suppressed contact is
skipped with the owner fallback actually firing, saving a contact starts the
confirmation loop, and the public confirm route consumes its single-use token.
"""

from __future__ import annotations

import uuid

import pytest
from app.db.session import tenant_session
from app.models.email_incident import EmailDeliveryIncident, is_suppressed
from sqlalchemy import select

pytestmark = [pytest.mark.contract]


async def _ready_account_with_luciel(client) -> uuid.UUID:
    from app.auth import access
    from app.auth.tokens import decode_token
    from app.config import get_settings

    email = f"esc-{uuid.uuid4().hex[:8]}@x.com"
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


def _bounce_event(admin_id: uuid.UUID, address: str, bounce_type: str = "Permanent") -> dict:
    import json

    return {
        "Type": "Notification",
        "Message": json.dumps(
            {
                "eventType": "Bounce",
                "bounce": {
                    "bounceType": bounce_type,
                    "bounceSubType": "General",
                    "bouncedRecipients": [{"emailAddress": address}],
                },
                "mail": {
                    "tags": {
                        "vm_admin_id": [str(admin_id)],
                        "vm_purpose": ["escalation_notification"],
                    }
                },
            }
        ),
    }


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_bounce_event_records_an_incident_under_the_right_tenant(client):
    admin = await _ready_account_with_luciel(client)
    r = await client.post(
        "/api/v1/webhooks/ses/events", json=_bounce_event(admin, "Contact@Shop.com")
    )
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    async with tenant_session(admin) as s:
        row = (
            await s.execute(
                select(EmailDeliveryIncident).where(EmailDeliveryIncident.admin_id == admin)
            )
        ).scalar_one()
    assert row.email_lower == "contact@shop.com"
    assert row.hard_bounced_at is not None
    assert is_suppressed(row)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_transient_bounce_counts_but_does_not_suppress(client):
    admin = await _ready_account_with_luciel(client)
    await client.post(
        "/api/v1/webhooks/ses/events", json=_bounce_event(admin, "c@shop.com", "Transient")
    )
    async with tenant_session(admin) as s:
        row = (
            await s.execute(
                select(EmailDeliveryIncident).where(EmailDeliveryIncident.admin_id == admin)
            )
        ).scalar_one()
    assert row.soft_bounce_count == 1
    assert not is_suppressed(row)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_untagged_event_is_unattributable_and_ignored(client):
    import json

    await _ready_account_with_luciel(client)
    r = await client.post(
        "/api/v1/webhooks/ses/events",
        json={
            "Type": "Notification",
            "Message": json.dumps(
                {
                    "eventType": "Bounce",
                    "bounce": {
                        "bounceType": "Permanent",
                        "bouncedRecipients": [{"emailAddress": "x@y.com"}],
                    },
                    "mail": {"tags": {}},
                }
            ),
        },
    )
    assert r.json() == {"status": "ignored", "reason": "unattributable"}


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_saving_a_contact_starts_the_confirmation_loop(client):
    admin = await _ready_account_with_luciel(client)
    r = await client.put(
        "/api/v1/admin/luciel/escalation",
        json={"primaryEmail": "owner-contact@shop.com", "preferredChannel": "email"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    health = body.get("escalationContactHealth") or []
    assert health and health[0]["address"] == "owner-contact@shop.com"
    assert health[0]["state"] == "pending_confirmation"
    async with tenant_session(admin) as s:
        row = (
            await s.execute(
                select(EmailDeliveryIncident).where(EmailDeliveryIncident.admin_id == admin)
            )
        ).scalar_one()
        assert row.verification_pending_jti


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_confirm_round_trip_consumes_the_single_use_token(client):
    from app.auth.tokens import mint_link_token
    from app.notifications.delivery_incidents import begin_verification

    admin = await _ready_account_with_luciel(client)
    jti = uuid.uuid4().hex
    token = mint_link_token(admin, "escalation_contact", 3600, jti)
    async with tenant_session(admin) as s:
        await begin_verification(s, admin_id=admin, email="contact@shop.com", jti=jti)

    first = await client.post("/api/v1/escalation-contact/confirm", json={"token": token})
    assert first.status_code == 200, first.text
    assert first.json() == {"status": "confirmed"}
    async with tenant_session(admin) as s:
        row = (
            await s.execute(
                select(EmailDeliveryIncident).where(EmailDeliveryIncident.admin_id == admin)
            )
        ).scalar_one()
        assert row.verified_at is not None
        assert row.verification_pending_jti is None

    replay = await client.post("/api/v1/escalation-contact/confirm", json={"token": token})
    assert replay.status_code == 422


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_malformed_contact_email_is_a_422_at_save_time(client):
    await _ready_account_with_luciel(client)
    r = await client.put(
        "/api/v1/admin/luciel/escalation",
        json={"primaryEmail": "not-an-email", "preferredChannel": "email"},
    )
    assert r.status_code == 422


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_reverified_after_bounce_unsuppresses(client):
    from datetime import UTC, datetime

    admin = await _ready_account_with_luciel(client)
    await client.post("/api/v1/webhooks/ses/events", json=_bounce_event(admin, "c@shop.com"))
    async with tenant_session(admin) as s:
        row = (
            await s.execute(
                select(EmailDeliveryIncident).where(EmailDeliveryIncident.admin_id == admin)
            )
        ).scalar_one()
        assert is_suppressed(row)
        row.verified_at = datetime.now(UTC)
        await s.flush()
        assert not is_suppressed(row)
