"""Outbound replies on EMAIL and META (Architecture §3.1.2 / §3.1.6a / §3.4.9 / §3.4.12).

SMS already answers inline through TwiML. Email and Instagram/Messenger have no reply
channel in the webhook response, so the platform has to push the answer back out — and
these are the ways that push goes wrong quietly: sending from a platform address instead
of the tenant's, replying on a different channel than the lead wrote in on, or posting an
Instagram DM in the WhatsApp Graph shape (which Meta accepts and silently drops).

Both reply paths funnel through the same ``send_outbound_reply``, so each channel is
pinned once for Luciel's own answer and once for the admin's takeover reply.
"""

from __future__ import annotations

import uuid

import pytest
from app.channels import outbound
from app.connections.secrets import secret_ref_for
from app.db.session import tenant_session
from app.models.connection import InstanceConnection

LEAD_EMAIL = "dana@lead.example"
TENANT_EMAIL = "hello@acmepatios.example"
PAGE_ID = "ig-account-77"


class FakeEmailSender:
    def __init__(self) -> None:
        self.sent: list[dict[str, str | None]] = []

    async def send(self, *, to, subject, body_text, from_address=None):
        self.sent.append(
            {"to": to, "subject": subject, "body": body_text, "from_address": from_address}
        )
        return f"ses-{len(self.sent)}"


class FakeMetaSender:
    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []
        self.grants: list[str] = []

    async def send_message(self, *, channel, sender_id, recipient_id, body):
        self.sent.append(
            {
                "channel": channel,
                "sender_id": sender_id,
                "recipient_id": recipient_id,
                "body": body,
            }
        )
        return f"mid-{len(self.sent)}"


@pytest.fixture
def email_sender(monkeypatch) -> FakeEmailSender:
    from app.notifications import email_sender as sender_mod

    fake = FakeEmailSender()
    monkeypatch.setattr(sender_mod, "build_email_sender", lambda: fake)
    return fake


@pytest.fixture
def meta_sender(monkeypatch) -> FakeMetaSender:
    from app.channels import meta_sender as sender_mod

    fake = FakeMetaSender()

    async def _build(session, admin_id, instance_id, *, connection_type="channel_auth", store=None):
        # The grant the sender was built from decides which Meta app's token signs the
        # send, so the tests assert on it (see ``grants`` below).
        fake.grants.append(connection_type)
        return fake

    monkeypatch.setattr(sender_mod, "build_tenant_meta_sender", _build)
    return fake


async def _seed_connection(
    admin: uuid.UUID,
    instance: uuid.UUID,
    *,
    connection_type: str,
    auth_class: str,
    provider: str,
    non_secret_config: dict[str, object],
) -> None:
    async with tenant_session(admin) as s:
        s.add(
            InstanceConnection(
                connection_id=uuid.uuid4(),
                admin_id=admin,
                instance_id=instance,
                connection_type=connection_type,
                auth_class=auth_class,
                provider=provider,
                status="connected",
                secret_ref=secret_ref_for(admin, instance, connection_type),
                non_secret_config=non_secret_config,
            )
        )
        await s.flush()


async def _seed_email_identity(admin: uuid.UUID, instance: uuid.UUID) -> None:
    await _seed_connection(
        admin,
        instance,
        connection_type="email_sender",
        auth_class="api_key",
        provider="ses",
        non_secret_config={"address": TENANT_EMAIL},
    )


async def _seed_meta_channel(admin: uuid.UUID, instance: uuid.UUID) -> None:
    await _seed_connection(
        admin,
        instance,
        connection_type="channel_auth",
        auth_class="oauth_token",
        provider="meta",
        non_secret_config={"destination": PAGE_ID},
    )


async def _reply(admin: uuid.UUID, instance: uuid.UUID, **kwargs) -> outbound.OutboundResult:
    async with tenant_session(admin) as s:
        return await outbound.send_outbound_reply(s, admin_id=admin, instance_id=instance, **kwargs)


# --- email --------------------------------------------------------------------- #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_email_reply_sends_from_the_tenants_own_address(email_sender):
    """§3.1.6a: the FROM identity is the tenant's provisioned address. A reply from a
    platform address is unreplyable and unrecognizable to the lead."""
    admin, instance = uuid.uuid4(), uuid.uuid4()
    await _seed_email_identity(admin, instance)

    result = await _reply(
        admin, instance, channel="email", to=LEAD_EMAIL, body="We open at 9.", subject=None
    )

    assert result.delivered is True
    assert result.provider_message_id == "ses-1"
    assert email_sender.sent[0]["to"] == LEAD_EMAIL
    assert email_sender.sent[0]["from_address"] == TENANT_EMAIL
    # The answer leads; the AI-identity / opt-out / privacy footer follows (F073 —
    # pinned in tests/unit/test_outbound_health_gates.py).
    assert email_sender.sent[0]["body"].startswith("We open at 9.")


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_email_reply_threads_onto_the_leads_own_subject(email_sender):
    """A reply that starts a new thread reads like spam; keep the lead's subject with a
    single Re: prefix (and do not double it up when replying to a reply)."""
    admin, instance = uuid.uuid4(), uuid.uuid4()
    await _seed_email_identity(admin, instance)

    await _reply(admin, instance, channel="email", to=LEAD_EMAIL, body="x", subject="Patio quote")
    await _reply(
        admin, instance, channel="email", to=LEAD_EMAIL, body="x", subject="Re: Patio quote"
    )

    assert [m["subject"] for m in email_sender.sent] == ["Re: Patio quote", "Re: Patio quote"]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_email_reply_without_a_provisioned_identity_is_reported_not_sent(email_sender):
    """No provisioned address means no honest FROM — say so instead of sending from a
    platform address the lead has never seen."""
    admin, instance = uuid.uuid4(), uuid.uuid4()

    result = await _reply(admin, instance, channel="email", to=LEAD_EMAIL, body="hi")

    assert result.delivered is False
    assert result.detail == "channel_not_provisioned"
    assert email_sender.sent == []


# --- Meta (Instagram DM / Messenger) ------------------------------------------- #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
@pytest.mark.parametrize("channel", ["messenger", "instagram"])
async def test_meta_reply_uses_the_messenger_send_shape(meta_sender, channel):
    """IG DM / Messenger is a DIFFERENT Graph shape from WhatsApp. Routing it through the
    WhatsApp adapter is accepted-then-dropped, which looks like success. Since the §3.1.2
    split each ride their own channel id, threaded through to the sender."""
    admin, instance = uuid.uuid4(), uuid.uuid4()

    result = await _reply(
        admin,
        instance,
        channel=channel,
        to="ig-user-1",
        body="Thanks for reaching out!",
        sender_id=PAGE_ID,
    )

    assert result.delivered is True
    assert meta_sender.sent == [
        {
            "channel": channel,
            "sender_id": PAGE_ID,
            "recipient_id": "ig-user-1",
            "body": "Thanks for reaching out!",
        }
    ]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_whatsapp_reply_uses_the_whatsapp_send_shape(meta_sender):
    """The same call on whatsapp must select the WhatsApp Cloud API shape."""
    admin, instance = uuid.uuid4(), uuid.uuid4()

    result = await _reply(
        admin,
        instance,
        channel="whatsapp",
        to="+15557778888",
        body="We open at 9.",
        sender_id="wa-phone-1",
    )

    assert result.delivered is True
    assert meta_sender.sent[0]["channel"] == "whatsapp"
    assert meta_sender.sent[0]["sender_id"] == "wa-phone-1"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_meta_takeover_reply_falls_back_to_the_provisioned_channel_identity(meta_sender):
    """The takeover path has no inbound message to read the page id off, so it must
    resolve the tenant's provisioned channel identity instead (§3.4.12)."""
    admin, instance = uuid.uuid4(), uuid.uuid4()
    await _seed_meta_channel(admin, instance)

    result = await _reply(admin, instance, channel="messenger", to="ig-user-1", body="Sam here.")

    assert result.delivered is True
    assert meta_sender.sent[0]["sender_id"] == PAGE_ID
    # A Page is a Facebook asset, so the Facebook grant's token is the one that can send
    # as it — even though that grant is shared with the WhatsApp channel.
    assert meta_sender.grants == ["channel_auth"]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_instagram_asset_sends_with_the_instagram_grants_token(meta_sender):
    """The asset we send AS and the token we send WITH have to come from the same Meta
    app, or Graph rejects the call: an Instagram Login token is not a Facebook token and
    is not even accepted at the same host. The instagram channel resolves its asset on
    the Instagram grant even when a Facebook grant is also connected."""
    admin, instance = uuid.uuid4(), uuid.uuid4()
    ig_account = "ig-account-1"
    await _seed_connection(
        admin,
        instance,
        connection_type="instagram_auth",
        auth_class="oauth_token",
        provider="instagram",
        non_secret_config={"destinations": {"instagram": ig_account}},
    )
    await _seed_meta_channel(admin, instance)

    result = await _reply(admin, instance, channel="instagram", to="ig-user-1", body="Sam here.")

    assert result.delivered is True
    assert meta_sender.sent[0]["sender_id"] == ig_account
    assert meta_sender.grants == ["instagram_auth"]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_legacy_combined_channel_sessions_can_still_reply(meta_sender):
    """Sessions opened before the §3.1.2 split carry channel='instagram_messenger'.
    Their in-flight replies must keep working: the legacy id still resolves across BOTH
    grants (Instagram first, then the Facebook Page)."""
    admin, instance = uuid.uuid4(), uuid.uuid4()
    await _seed_meta_channel(admin, instance)

    result = await _reply(
        admin, instance, channel="instagram_messenger", to="ig-user-1", body="Sam here."
    )

    assert result.delivered is True
    assert meta_sender.sent[0]["sender_id"] == PAGE_ID
    assert meta_sender.grants == ["channel_auth"]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_meta_reply_without_a_provisioned_channel_is_reported(meta_sender):
    admin, instance = uuid.uuid4(), uuid.uuid4()

    result = await _reply(admin, instance, channel="messenger", to="ig-user-1", body="hi")

    assert result.delivered is False
    assert result.detail == "channel_not_provisioned"
    assert meta_sender.sent == []


# --- the two callers ----------------------------------------------------------- #
async def test_sms_is_the_only_inline_reply_channel():
    """Luciel's answer rides back in Twilio's TwiML response, so the webhook must NOT
    push it a second time — while every other channel MUST be pushed or the lead gets
    silence. This membership is what decides that, for both reply paths."""
    assert outbound.INLINE_REPLY_CHANNELS == frozenset({"sms"})
    assert "email" not in outbound.INLINE_REPLY_CHANNELS
    assert "messenger" not in outbound.INLINE_REPLY_CHANNELS
    assert "instagram" not in outbound.INLINE_REPLY_CHANNELS
    assert "whatsapp" not in outbound.INLINE_REPLY_CHANNELS


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_inbound_email_answer_is_pushed_back_to_the_lead(email_sender, monkeypatch):
    """End of the Luciel-answer path: an admitted inbound email must have the runtime's
    reply pushed back out, addressed to the identifier the lead wrote in from (§3.4.9)."""
    from app.api.v1.webhooks import channels as webhook_channels
    from app.channels.base import InboundMessage

    admin, instance = uuid.uuid4(), uuid.uuid4()
    await _seed_email_identity(admin, instance)

    class Reply:
        reply_text = "We open at 9."
        # RuntimeReply.session_id: empty = no session to pin reply context on.
        session_id = ""

    async def fake_run(session, msg):
        return Reply()

    from app.channels import inbound_dispatch

    # The runtime entry moved with the shared dispatch (channels/inbound_dispatch);
    # webhook_channels._run_and_reply is an alias of the same function.
    monkeypatch.setattr(inbound_dispatch, "run_inbound_message", fake_run)

    await webhook_channels._run_and_reply(
        InboundMessage(
            admin_id=admin,
            instance_id=instance,
            channel="email",
            session_id=None,
            customer_identifier=LEAD_EMAIL,
            body="What time do you open?",
            channel_metadata={"subject": "Hours"},
        )
    )

    assert email_sender.sent[0]["to"] == LEAD_EMAIL
    assert email_sender.sent[0]["subject"] == "Re: Hours"
    assert email_sender.sent[0]["from_address"] == TENANT_EMAIL
