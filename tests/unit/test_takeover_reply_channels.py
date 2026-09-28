"""Round 7 WP-3b — a human takeover reply goes out AS the person who wrote it, in the
thread it answers, through a Meta grant that can actually send.

Channel-side half (``app/channels/outbound.py`` + ``email_adapter.py``); the route
half lives in tests/contract/test_takeover_truth.py.

  * item 3: the owner's reply is signed by the business, never "written by an AI
    assistant"; the opt-out promise stays on both footers.
  * item 2 (legacy SES branch): a known customer Message-ID becomes In-Reply-To /
    References; a platform sender without header support still sends, unthreaded.
  * item 7: the grant that OWNS the Meta surface must be ``connected`` before a reply
    is attempted; otherwise ``meta_reconnect_needed`` — a wire word, not a generic
    send_failed.
"""

from __future__ import annotations

import uuid

import pytest
from app.channels import outbound
from app.connections.secrets import secret_ref_for
from app.db.session import tenant_session
from app.models.connection import InstanceConnection

pytestmark = [pytest.mark.asyncio]

LEAD_EMAIL = "dana@lead.example"
TENANT_EMAIL = "hello@acmepatios.example"
PAGE_ID = "page-77"
PSID = "psid-1"


class FakePlatformSender:
    """Platform SES stand-in whose ``send`` accepts threading headers."""

    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    async def send(self, *, to, subject, body_text, from_address=None, headers=None):
        self.sent.append(
            {
                "to": to,
                "subject": subject,
                "body": body_text,
                "from_address": from_address,
                "headers": headers,
            }
        )
        return f"ses-{len(self.sent)}"


class HeaderlessPlatformSender:
    """A platform sender with no ``headers`` keyword at all (the shape the SES
    notifier has today): the reply must still go out, unthreaded."""

    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    async def send(self, *, to, subject, body_text, from_address=None):
        self.sent.append({"to": to, "subject": subject, "body": body_text})
        return f"ses-{len(self.sent)}"


class FakeMetaSender:
    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []

    async def send_message(self, *, channel, sender_id, recipient_id, body):
        self.sent.append(
            {"channel": channel, "sender_id": sender_id, "recipient_id": recipient_id, "body": body}
        )
        return f"mid-{len(self.sent)}"


@pytest.fixture
def platform_email(monkeypatch) -> FakePlatformSender:
    from app.notifications import email_sender as sender_mod

    fake = FakePlatformSender()
    monkeypatch.setattr(sender_mod, "build_email_sender", lambda: fake)
    return fake


@pytest.fixture
def meta_sender(monkeypatch) -> FakeMetaSender:
    from app.channels import meta_sender as sender_mod

    fake = FakeMetaSender()

    async def _build(session, admin_id, instance_id, *, connection_type="channel_auth", store=None):
        return fake

    monkeypatch.setattr(sender_mod, "build_tenant_meta_sender", _build)
    return fake


async def _seed_connection(
    admin: uuid.UUID, instance: uuid.UUID, *, connection_type: str, status: str, **fields
) -> None:
    async with tenant_session(admin) as s:
        s.add(
            InstanceConnection(
                connection_id=uuid.uuid4(),
                admin_id=admin,
                instance_id=instance,
                connection_type=connection_type,
                status=status,
                secret_ref=secret_ref_for(admin, instance, connection_type),
                **fields,
            )
        )
        await s.flush()


async def _seed_email_identity(admin: uuid.UUID, instance: uuid.UUID) -> None:
    await _seed_connection(
        admin,
        instance,
        connection_type="email_sender",
        status="connected",
        auth_class="api_key",
        provider="ses",
        non_secret_config={"address": TENANT_EMAIL},
    )


async def _seed_meta_grant(admin: uuid.UUID, instance: uuid.UUID, *, status: str) -> None:
    await _seed_connection(
        admin,
        instance,
        connection_type="channel_auth",
        status=status,
        auth_class="oauth_token",
        provider="meta",
        non_secret_config={"destinations": {"messenger": PAGE_ID}},
    )


async def _email_reply(admin: uuid.UUID, instance: uuid.UUID, **kwargs) -> outbound.OutboundResult:
    async with tenant_session(admin) as s:
        return await outbound.send_outbound_reply(
            s, admin_id=admin, instance_id=instance, channel="email", to=LEAD_EMAIL, **kwargs
        )


async def _meta_reply(admin: uuid.UUID, instance: uuid.UUID) -> outbound.OutboundResult:
    async with tenant_session(admin) as s:
        return await outbound.send_outbound_reply(
            s,
            admin_id=admin,
            instance_id=instance,
            channel="messenger",
            to=PSID,
            body="Sam here.",
            sender_id=PAGE_ID,
            author_role="human_agent",
        )


# --- item 3: the footer names the author truthfully ---------------------------------- #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_human_reply_email_is_signed_by_the_business_never_as_an_ai(platform_email):
    """The owner's takeover reply used to leave with "written by an AI assistant"
    under it — an AI disclosure on a person's words is the mirror lie of a human
    impersonating the AI. The opt-out promise stays (Legal §A2)."""
    admin, instance = uuid.uuid4(), uuid.uuid4()
    await _seed_email_identity(admin, instance)

    result = await _email_reply(
        admin, instance, body="Ana here, Tuesday at 10 works.", author_role="human_agent"
    )

    assert result.delivered is True
    body = str(platform_email.sent[0]["body"])
    assert body.startswith("Ana here, Tuesday at 10 works.")
    assert outbound.EMAIL_FOOTER_MARKER not in body
    assert "AI" not in body
    assert "Sent by this business." in body
    assert outbound.EMAIL_OPT_OUT_LINE in body
    assert "/legal/privacy" in body


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_luciel_reply_email_keeps_the_ai_disclosure_footer(platform_email):
    """The default author is Luciel and its footer is unchanged: disclosure present on
    every channel (root invariant), opt-out and privacy alongside."""
    admin, instance = uuid.uuid4(), uuid.uuid4()
    await _seed_email_identity(admin, instance)

    await _email_reply(admin, instance, body="We open at 9.")

    body = str(platform_email.sent[0]["body"])
    assert body.count(outbound.EMAIL_FOOTER_MARKER) == 1
    assert outbound.EMAIL_OPT_OUT_LINE in body
    assert "Sent by" not in body


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_human_footer_is_not_stacked_on_a_body_that_already_carries_it(
    platform_email,
):
    admin, instance = uuid.uuid4(), uuid.uuid4()
    await _seed_email_identity(admin, instance)
    footered = f"Sure.\n\n--\nSent by Acme. {outbound.EMAIL_OPT_OUT_LINE} Privacy: x"

    await _email_reply(admin, instance, body=footered, author_role="human_agent")

    assert platform_email.sent[0]["body"] == footered


# --- item 2: the legacy SES branch threads too ---------------------------------------- #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_legacy_ses_reply_threads_under_the_customers_message(platform_email):
    """With the customer's Message-ID known, the reply carries In-Reply-To /
    References so their client hangs it under the mail it answers."""
    admin, instance = uuid.uuid4(), uuid.uuid4()
    await _seed_email_identity(admin, instance)

    result = await _email_reply(
        admin,
        instance,
        body="Tuesday works.",
        subject="Patio quote",
        in_reply_to="<abc123@lead.example>",
        author_role="human_agent",
    )

    assert result.delivered is True
    sent = platform_email.sent[0]
    assert sent["subject"] == "Re: Patio quote"
    assert sent["from_address"] == TENANT_EMAIL
    assert sent["headers"] == {
        "In-Reply-To": "<abc123@lead.example>",
        "References": "<abc123@lead.example>",
    }


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_reply_with_no_known_inbound_message_sends_no_threading_headers(
    platform_email,
):
    admin, instance = uuid.uuid4(), uuid.uuid4()
    await _seed_email_identity(admin, instance)

    await _email_reply(admin, instance, body="Hi.", subject="Patio quote")

    assert platform_email.sent[0]["headers"] is None


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_platform_sender_without_header_support_still_sends_unthreaded(monkeypatch):
    """A degraded reply (no thread) is not a failed one: the send goes out and the
    result says delivered — never a crash on an unexpected keyword."""
    from app.notifications import email_sender as sender_mod

    fake = HeaderlessPlatformSender()
    monkeypatch.setattr(sender_mod, "build_email_sender", lambda: fake)
    admin, instance = uuid.uuid4(), uuid.uuid4()
    await _seed_email_identity(admin, instance)

    result = await _email_reply(
        admin, instance, body="Hi.", subject="Q", in_reply_to="<abc@lead.example>"
    )

    assert result.delivered is True
    assert len(fake.sent) == 1


async def test_threading_headers_normalise_the_angle_brackets():
    from app.channels.email_adapter import threading_headers

    assert threading_headers(None) is None
    assert threading_headers("   ") is None
    assert threading_headers("abc@lead.example") == {
        "In-Reply-To": "<abc@lead.example>",
        "References": "<abc@lead.example>",
    }
    bracketed = threading_headers("<abc@lead.example>")
    assert bracketed is not None
    assert bracketed["In-Reply-To"] == "<abc@lead.example>"


# --- item 7: the owning Meta grant must be able to send -------------------------------- #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
@pytest.mark.parametrize("status", ["expired", "error", "pending_destination"])
async def test_a_meta_reply_on_an_unhealthy_grant_is_refused_with_reconnect_needed(
    meta_sender, status
):
    """The row that OWNS the surface is not connected: Graph would reject the token
    (or there is no bound asset). The reply must not be attempted, and the reason
    must be the wire word the dashboard can act on, not a generic send_failed."""
    admin, instance = uuid.uuid4(), uuid.uuid4()
    await _seed_meta_grant(admin, instance, status=status)

    result = await _meta_reply(admin, instance)

    assert result.delivered is False
    assert result.detail == "meta_reconnect_needed"
    assert meta_sender.sent == []


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_meta_reply_on_a_connected_grant_goes_out_as_the_given_surface(meta_sender):
    admin, instance = uuid.uuid4(), uuid.uuid4()
    await _seed_meta_grant(admin, instance, status="connected")

    result = await _meta_reply(admin, instance)

    assert result.delivered is True
    assert meta_sender.sent == [
        {"channel": "messenger", "sender_id": PAGE_ID, "recipient_id": PSID, "body": "Sam here."}
    ]
