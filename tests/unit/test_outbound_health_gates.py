"""Outbound senders consult connection health before anything leaves (audit F070) and
every email reply carries the AI-identity / opt-out / privacy footer (audit F073).

``resolve_sms_number`` reads any non-revoked row, so a number sitting in
action-needed / pending-registration still "sent" and Twilio dropped it on the floor
with a green result in the dashboard. Email replies went out bare — no AI identity, no
opt-out, no privacy pointer — the one place §3.4.16 and Legal §A2 are non-negotiable.
"""

from __future__ import annotations

import uuid

import pytest
from app.channels import outbound
from app.channels.outbound import EMAIL_FOOTER_MARKER
from app.config import get_settings
from app.connections.secrets import secret_ref_for
from app.db.session import tenant_session
from app.models.connection import InstanceConnection
from app.models.instance import Instance, LucielState

LEAD_EMAIL = "dana@lead.example"
TENANT_EMAIL = "hello@acmepatios.example"
TENANT_NUMBER = "+15550001111"


class FakeEmailSender:
    def __init__(self) -> None:
        self.sent: list[dict[str, str | None]] = []

    async def send(self, *, to, subject, body_text, from_address=None):
        self.sent.append(
            {"to": to, "subject": subject, "body": body_text, "from_address": from_address}
        )
        return f"ses-{len(self.sent)}"


@pytest.fixture
def email_sender(monkeypatch) -> FakeEmailSender:
    from app.notifications import email_sender as sender_mod

    fake = FakeEmailSender()
    monkeypatch.setattr(sender_mod, "build_email_sender", lambda: fake)
    return fake


@pytest.fixture
def twilio(monkeypatch) -> list[dict[str, str]]:
    sent: list[dict[str, str]] = []

    class FakeSender:
        async def send_sms(self, from_number, to, body):
            sent.append({"from_number": from_number, "to": to, "body": body})
            return "SM-1"

    from app.channels import tenant_twilio

    async def _tenant_sender(*a, **kw):
        return FakeSender()

    monkeypatch.setattr(tenant_twilio, "build_tenant_sms_sender", _tenant_sender)
    return sent


async def _seed_connection(
    admin: uuid.UUID,
    instance: uuid.UUID,
    *,
    connection_type: str,
    provider: str,
    status: str,
    non_secret_config: dict[str, object],
) -> None:
    async with tenant_session(admin) as s:
        s.add(
            InstanceConnection(
                connection_id=uuid.uuid4(),
                admin_id=admin,
                instance_id=instance,
                connection_type=connection_type,
                auth_class="api_key",
                provider=provider,
                status=status,
                secret_ref=secret_ref_for(admin, instance, connection_type),
                non_secret_config=non_secret_config,
            )
        )
        await s.flush()


async def _reply(admin: uuid.UUID, instance: uuid.UUID, **kwargs) -> outbound.OutboundResult:
    async with tenant_session(admin) as s:
        return await outbound.send_outbound_reply(s, admin_id=admin, instance_id=instance, **kwargs)


# --- SMS health gate ----------------------------------------------------------------- #
@pytest.mark.parametrize(
    "status", ["not_operable_hosting_required", "action_needed", "dormant", "expired", "error"]
)
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_sms_number_that_is_not_connected_sends_nothing_and_says_so(twilio, status):
    admin, instance = uuid.uuid4(), uuid.uuid4()
    await _seed_connection(
        admin,
        instance,
        connection_type="sms_sender",
        provider="twilio",
        status=status,
        non_secret_config={"destination": TENANT_NUMBER},
    )
    result = await _reply(admin, instance, channel="sms", to="+15557778888", body="We open at 9.")
    assert result.delivered is False
    assert result.detail == "sms_sender_not_operable"
    assert twilio == []


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
@pytest.mark.parametrize("status", ["connected", "pending_carrier_registration"])
async def test_an_operable_sms_number_still_sends(twilio, status):
    """Attested-live sends; so does operable-and-pending — carrier registration is
    the tenant's job (§3.1.4) and holding every reply until then would silence a
    working number."""
    admin, instance = uuid.uuid4(), uuid.uuid4()
    await _seed_connection(
        admin,
        instance,
        connection_type="sms_sender",
        provider="twilio",
        status=status,
        non_secret_config={"destination": TENANT_NUMBER},
    )
    result = await _reply(admin, instance, channel="sms", to="+15557778888", body="We open at 9.")
    assert result.delivered is True
    assert twilio[0]["from_number"] == TENANT_NUMBER


# --- email health gate + footer -------------------------------------------------------- #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_email_sender_that_is_not_connected_sends_nothing_and_says_so(email_sender):
    admin, instance = uuid.uuid4(), uuid.uuid4()
    await _seed_connection(
        admin,
        instance,
        connection_type="email_sender",
        provider="ses",
        status="action_needed",
        non_secret_config={"address": TENANT_EMAIL},
    )
    result = await _reply(
        admin, instance, channel="email", to=LEAD_EMAIL, body="We open at 9.", subject=None
    )
    assert result.delivered is False
    assert result.detail == "email_sender_not_connected"
    assert email_sender.sent == []


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_every_email_reply_carries_the_ai_identity_opt_out_and_privacy_footer(
    email_sender,
):
    admin, instance = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            Instance(
                id=instance,
                admin_id=admin,
                name="Acme Patios",
                state=LucielState.active,
                business_one_liner="Acme Patios builds decks in Kelowna.",
            )
        )
        await s.flush()
    await _seed_connection(
        admin,
        instance,
        connection_type="email_sender",
        provider="ses",
        status="connected",
        non_secret_config={"address": TENANT_EMAIL},
    )
    result = await _reply(
        admin, instance, channel="email", to=LEAD_EMAIL, body="We open at 9.", subject=None
    )
    assert result.delivered is True
    body = email_sender.sent[0]["body"]
    assert body is not None
    assert body.startswith("We open at 9.")
    assert EMAIL_FOOTER_MARKER in body
    assert "Reply STOP" in body
    assert f"{get_settings().app_base_url.rstrip('/')}/legal/privacy" in body
    # The footer comes AFTER the answer, once.
    assert body.count(EMAIL_FOOTER_MARKER) == 1
    assert body.index("We open at 9.") < body.index(EMAIL_FOOTER_MARKER)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_footer_is_not_stacked_on_a_body_that_already_carries_it(email_sender):
    admin, instance = uuid.uuid4(), uuid.uuid4()
    await _seed_connection(
        admin,
        instance,
        connection_type="email_sender",
        provider="ses",
        status="connected",
        non_secret_config={"address": TENANT_EMAIL},
    )
    footered = f"Sure.\n\n--\n{EMAIL_FOOTER_MARKER} on behalf of Acme."
    result = await _reply(
        admin, instance, channel="email", to=LEAD_EMAIL, body=footered, subject=None
    )
    assert result.delivered is True
    assert email_sender.sent[0]["body"] == footered
