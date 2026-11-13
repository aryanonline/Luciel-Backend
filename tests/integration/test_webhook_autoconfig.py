"""Twilio webhook auto-configuration (c20, live-caught 2026-08-18).

Nothing in the product — code or copy — ever pointed the designated number's
Voice/SMS webhooks at the platform: a number only answered after someone edited
the Twilio Console by hand, and a number CHANGE silently killed both channels.
The platform now configures the three webhooks (voice, SMS, call-status) at
designate time using the tenant's own stored credentials. Pinned here:

  - an OPERABLE attach configures the three exact URLs built from
    settings.public_api_base_url;
  - a configuration failure NEVER fails the attach — status_detail carries the
    manual-fallback URLs and the failure is audited;
  - a number change re-points the NEW number;
  - a non-operable number attempts nothing;
  - no public API base configured → silent no-op (mock/CI posture);
  - submit_credentials with a destination already on file configures (the
    credentials-after-number order); without one it does not.
"""

from __future__ import annotations

import uuid

import pytest
from app.channels.number_operability import NullOperabilityProbe
from app.channels.phone_lifecycle import attach_sms_number
from app.channels.tenant_twilio import WebhookUrls
from app.config import get_settings
from app.db.session import tenant_session
from app.models.audit import AdminAuditLog
from app.models.instance import Account, AccountState, Instance, LucielState
from sqlalchemy import select

NUM = "+16045550190"
BASE = "https://api.vantagemind.test"


class RecordingConfigurator:
    def __init__(self, *, succeed: bool = True) -> None:
        self.succeed = succeed
        self.calls: list[tuple[str, WebhookUrls]] = []

    async def configure(self, e164_number: str, urls: WebhookUrls) -> bool:
        self.calls.append((e164_number, urls))
        return self.succeed


async def _seed(admin: uuid.UUID) -> uuid.UUID:
    iid = uuid.uuid4()
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
                name="Hook Co",
                state=LucielState.active,
                embed_key_public_id=f"vm_live_{admin.hex[:8]}",
                channels=[],
                tools=[],
                escalation={},
                personality={},
            )
        )
        await s.flush()
    return iid


async def _audit_events(admin: uuid.UUID, event_type: str) -> int:
    async with tenant_session(admin) as s:
        rows = (
            (
                await s.execute(
                    select(AdminAuditLog).where(
                        AdminAuditLog.admin_id == admin,
                        AdminAuditLog.event_type == event_type,
                    )
                )
            )
            .scalars()
            .all()
        )
    return len(rows)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_operable_attach_configures_the_three_exact_urls(monkeypatch):
    monkeypatch.setattr(get_settings(), "public_api_base_url", BASE, raising=False)
    admin = uuid.uuid4()
    iid = await _seed(admin)
    rec = RecordingConfigurator()
    async with tenant_session(admin) as s:
        await attach_sms_number(
            s,
            admin_id=admin,
            instance_id=iid,
            e164_number=NUM,
            probe=NullOperabilityProbe(operable_numbers={NUM}),
            webhook_config=rec,
        )
    assert len(rec.calls) == 1
    number, urls = rec.calls[0]
    assert number == NUM
    assert urls.voice_url == f"{BASE}/api/v1/webhooks/twilio/voice"
    assert urls.sms_url == f"{BASE}/api/v1/webhooks/twilio/sms"
    assert urls.status_callback_url == f"{BASE}/api/v1/webhooks/twilio/voice/status"
    assert await _audit_events(admin, "webhook_autoconfig_applied") == 1


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_configuration_failure_never_fails_the_attach(monkeypatch):
    monkeypatch.setattr(get_settings(), "public_api_base_url", BASE, raising=False)
    admin = uuid.uuid4()
    iid = await _seed(admin)
    async with tenant_session(admin) as s:
        conn = await attach_sms_number(
            s,
            admin_id=admin,
            instance_id=iid,
            e164_number=NUM,
            probe=NullOperabilityProbe(operable_numbers={NUM}),
            webhook_config=RecordingConfigurator(succeed=False),
        )
        assert conn.status == "pending_carrier_registration"  # the attach survived
        detail = conn.status_detail or ""
        assert "couldn't point your number at Luciel automatically" in detail
        assert f"{BASE}/api/v1/webhooks/twilio/voice" in detail
        assert f"{BASE}/api/v1/webhooks/twilio/sms" in detail
        assert f"{BASE}/api/v1/webhooks/twilio/voice/status" in detail
    assert await _audit_events(admin, "webhook_autoconfig_failed") == 1


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_number_change_repoints_the_new_number(monkeypatch):
    monkeypatch.setattr(get_settings(), "public_api_base_url", BASE, raising=False)
    admin = uuid.uuid4()
    iid = await _seed(admin)
    other = "+16045550191"
    rec = RecordingConfigurator()
    async with tenant_session(admin) as s:
        await attach_sms_number(
            s,
            admin_id=admin,
            instance_id=iid,
            e164_number=NUM,
            probe=NullOperabilityProbe(operable_numbers={NUM, other}),
            webhook_config=rec,
        )
        await attach_sms_number(
            s,
            admin_id=admin,
            instance_id=iid,
            e164_number=other,
            probe=NullOperabilityProbe(operable_numbers={NUM, other}),
            webhook_config=rec,
        )
    assert [c[0] for c in rec.calls] == [NUM, other]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_non_operable_number_attempts_no_configuration(monkeypatch):
    monkeypatch.setattr(get_settings(), "public_api_base_url", BASE, raising=False)
    admin = uuid.uuid4()
    iid = await _seed(admin)
    rec = RecordingConfigurator()
    async with tenant_session(admin) as s:
        await attach_sms_number(
            s,
            admin_id=admin,
            instance_id=iid,
            e164_number=NUM,
            probe=NullOperabilityProbe(operable_numbers=set()),
            webhook_config=rec,
        )
    assert rec.calls == []


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_no_public_api_base_is_a_silent_noop():
    """Mock/CI/local: auto-config is not part of the deployment — no failure note,
    no audit noise, and the honest status_detail is untouched."""
    admin = uuid.uuid4()
    iid = await _seed(admin)
    rec = RecordingConfigurator()
    async with tenant_session(admin) as s:
        conn = await attach_sms_number(
            s,
            admin_id=admin,
            instance_id=iid,
            e164_number=NUM,
            probe=NullOperabilityProbe(operable_numbers={NUM}),
            webhook_config=rec,
        )
        assert "couldn't point your number" not in (conn.status_detail or "")
    assert rec.calls == []
    assert await _audit_events(admin, "webhook_autoconfig_failed") == 0


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_credentials_after_number_configures_and_without_number_does_not(monkeypatch):
    """submit_credentials with a destination on file runs the configurator (the
    designate ran before working credentials existed); with no destination there
    is nothing to point anywhere."""
    from app.channels import phone_lifecycle
    from app.connections import service

    monkeypatch.setattr(get_settings(), "public_api_base_url", BASE, raising=False)
    calls: list[str] = []

    async def fake_configure(
        session, *, admin_id, instance_id, conn, e164_number, webhook_config=None
    ):
        calls.append(e164_number)
        return True

    monkeypatch.setattr(phone_lifecycle, "configure_number_webhooks", fake_configure)

    admin = uuid.uuid4()
    iid = await _seed(admin)
    async with tenant_session(admin) as s:
        conn, _ = await service.start_connection(
            s,
            admin_id=admin,
            instance_id=iid,
            connection_type="sms_sender",
            provider="twilio",
            user_id=None,
        )
        # No destination yet: credentials alone configure nothing.
        await service.submit_credentials(
            s,
            admin_id=admin,
            connection_id=conn.connection_id,
            fields={"accountSid": "AC" + "0" * 32, "authToken": "tok"},
            user_id=None,
        )
        assert calls == []
        # Designate (attaches the destination)...
        await attach_sms_number(
            s,
            admin_id=admin,
            instance_id=iid,
            e164_number=NUM,
            probe=NullOperabilityProbe(operable_numbers={NUM}),
        )
        calls.clear()  # attach's own call is covered elsewhere
        # ...then re-submitting credentials re-points the number.
        await service.submit_credentials(
            s,
            admin_id=admin,
            connection_id=conn.connection_id,
            fields={"accountSid": "AC" + "0" * 32, "authToken": "tok2"},
            user_id=None,
        )
        assert calls == [NUM]
