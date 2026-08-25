"""Verification / reset mail must actually be dispatched (§1, CJ Phase 2).

Both resend-verification and forgot-password used to MINT a token and then drop it on
the floor: the endpoints returned 200, no mail was ever handed to a sender, and the
customer was stranded behind the verify wall with no way forward. These tests pin the
dispatch itself, the link the customer receives, and the honest degraded signal when a
send fails — plus the non-enumeration property that the forgot-password shape must keep.
"""

from __future__ import annotations

import uuid

import pytest
from app.notifications.email_dispatcher import DeliveryOutcome

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]


def _email() -> str:
    return f"mail-{uuid.uuid4().hex[:8]}@sarahchen.com"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_resend_verification_dispatches_the_link(client, monkeypatch):
    """The whole point of the endpoint is the mail; a 200 with no send is a lie."""
    captured: dict[str, str] = {}

    async def _capture(session, *, admin_id, to, verify_url):
        captured.update(to=to, url=verify_url)
        return DeliveryOutcome(status="sent")

    email = _email()
    await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "pw-123456", "captchaToken": "t"},
    )
    await client.post("/api/v1/auth/login", json={"email": email, "password": "pw-123456"})

    monkeypatch.setattr("app.api.v1.auth.email_dispatcher.send_verify_email", _capture)
    r = await client.post("/api/v1/auth/resend-verification")

    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["emailDeliveryDegraded"] is False
    assert captured["to"] == email
    assert "/verify?token=" in captured["url"]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_resend_verification_reports_a_failed_send(client, monkeypatch):
    """An undelivered link strands the customer behind the verify wall, so the client
    must learn the mail never left — without seeing the provider error."""

    async def _degraded(session, *, admin_id, to, verify_url):
        return DeliveryOutcome(status="failed")

    email = _email()
    await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "pw-123456", "captchaToken": "t"},
    )
    await client.post("/api/v1/auth/login", json={"email": email, "password": "pw-123456"})

    monkeypatch.setattr("app.api.v1.auth.email_dispatcher.send_verify_email", _degraded)
    r = await client.post("/api/v1/auth/resend-verification")

    assert r.status_code == 200
    assert r.json()["emailDeliveryDegraded"] is True


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_forgot_password_dispatches_the_reset_link(client, monkeypatch):
    """The reset template did not exist and the token was discarded — password reset
    was unreachable for every customer."""
    captured: dict[str, str] = {}

    async def _capture(session, *, admin_id, to, reset_url):
        captured.update(to=to, url=reset_url)
        return DeliveryOutcome(status="sent")

    email = _email()
    await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "pw-123456", "captchaToken": "t"},
    )
    monkeypatch.setattr("app.api.v1.auth.email_dispatcher.send_reset_email", _capture)

    r = await client.post("/api/v1/auth/forgot-password", json={"email": email})
    assert r.status_code == 200
    assert captured["to"] == email
    assert "/reset?token=" in captured["url"]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_forgot_password_stays_non_enumerating(client, monkeypatch):
    """§3.7.1a: an unregistered address must get the SAME body — and no dispatch, since
    a timing/behaviour difference is itself an enumeration oracle."""
    sends = 0

    async def _count(session, *, admin_id, to, reset_url):
        nonlocal sends
        sends += 1
        return DeliveryOutcome(status="sent")

    monkeypatch.setattr("app.api.v1.auth.email_dispatcher.send_reset_email", _count)
    r = await client.post("/api/v1/auth/forgot-password", json={"email": _email()})

    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert sends == 0


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_raising_sender_is_logged_and_audited_not_swallowed(caplog):
    """A send failure used to vanish: only the exception CLASS reached the audit payload
    and nothing reached the log, so a misconfigured SES identity was undiagnosable."""
    import logging

    from app.db.session import tenant_session
    from app.models.audit import AdminAuditLog
    from app.notifications import email_dispatcher
    from sqlalchemy import select

    class ExplodingSender:
        async def send(self, *, to, subject, body_text, tags=None):
            raise RuntimeError("SES said no")

    admin = uuid.uuid4()
    with caplog.at_level(logging.ERROR):
        async with tenant_session(admin) as s:
            outcome = await email_dispatcher.send_verify_email(
                s,
                admin_id=admin,
                to="sarah@sarahchen.com",
                verify_url="https://vantagemind.ai/verify?token=t",
                sender=ExplodingSender(),
            )

    assert outcome.status == "failed"
    assert outcome.degraded is True
    assert "email delivery failed" in caplog.text
    assert "SES said no" in caplog.text

    async with tenant_session(admin) as s:
        row = (
            await s.execute(
                select(AdminAuditLog).where(AdminAuditLog.event_type == "verification_resent")
            )
        ).scalar_one()
    assert row.event_payload["delivery_status"] == "failed"
    assert row.event_payload["delivery_degraded"] is True
