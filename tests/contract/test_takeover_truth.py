"""Round 7 WP-3b — live takeover truth on every channel, the ROUTE half.

POST /api/v1/dashboard/conversations/{id}/messages (the owner's reply while holding
a conversation), take-over, and the conversation list. The channel half lives in
tests/unit/test_takeover_reply_channels.py, the ingress half in
tests/integration/test_takeover_reply_context.py.

  * item 1: the reply goes to an identifier of the SESSION's channel kind — after a
    widget-then-SMS unification the lead record is keyed on the older email, and the
    SMS reply went there. No identifier of that kind → ``no_recipient_for_channel``,
    never a send to another channel.
  * item 2: an email reply threads under the customer's mail — Graph's
    ``/me/messages/{id}/reply`` on a BYO mailbox, In-Reply-To / References on the
    legacy SES path — instead of a fresh "Re: your inquiry".
  * item 3: the reply is signed by the business, never "written by an AI assistant".
  * item 7: a Meta reply goes out AS the surface the conversation happened on TO the
    persisted sender id; an unhealthy grant is ``meta_reconnect_needed``.
  * item 4: an ENDED session is refused for take-over and for a reply (409
    ``session_ended``); ``endedAt`` is on the summary so the page can say why.
  * item 5: a human reply's delivery outcome is persisted on its row and replays on
    reopen — a Twilio-rejected reply reads ``failed`` on GET .../messages.
  * item 6: ``countedForBilling`` on the summary — a session that never reached the
    fire-once trigger is listed as unbilled, not beside the billed ones as one of them.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from app.connections.secrets import secret_ref_for
from app.db.session import tenant_session
from app.models.audit import AdminAuditLog
from app.models.budget import CountedSession
from app.models.connection import InstanceConnection
from app.models.conversation import Session as SessionModel
from app.models.conversation import Transcript
from app.models.instance import Instance, LucielState
from app.models.lead import Lead, LeadIdentifier, LeadState
from sqlalchemy import select

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]

LEAD_EMAIL = "dana@lead.example"
LEAD_PHONE = "+15557778888"
TENANT_NUMBER = "+15550001111"
TENANT_EMAIL = "hello@acmepatios.example"
PAGE_ID = "page-4242"
PSID = "psid-9001"
REPLY = "Ana here — Tuesday at 10 works."
ALL_CHANNELS = ("widget", "email", "sms", "voice", "whatsapp", "messenger", "instagram")


async def _signup_verify_login(client) -> uuid.UUID:
    from app.auth import access

    email = f"takeover-{uuid.uuid4().hex[:8]}@x.com"
    await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "pw-123456", "captchaToken": "t"},
    )
    acct, token = await access.signup(email, "pw-123456")
    await client.post("/api/v1/auth/verify-email", json={"token": token})
    await client.post("/api/v1/auth/login", json={"email": email, "password": "pw-123456"})
    return acct.id


@dataclass(frozen=True)
class Seeded:
    instance_id: uuid.UUID
    session_id: uuid.UUID
    lead_id: uuid.UUID


async def _seed(
    admin: uuid.UUID,
    *,
    channel: str,
    contact: str | None,
    identifiers: tuple[tuple[str, str], ...] = (),
    sms_status: str | None = None,
    email_provider: str | None = None,
    email_status: str = "connected",
    meta_status: str | None = None,
    mode: str = "ai",
    **session_fields: object,
) -> Seeded:
    """One tenant with a Luciel, a lead, a session on ``channel`` resolved to that lead,
    and whichever sender connections the case needs.

    ``contact`` is the lead record's ``contact_identifier`` (the identity it was keyed
    on first); ``identifiers`` are the (kind, value) rows the lead is ALSO known by —
    what a unification leaves behind (confirmed by the transport that named them)."""
    iid, sid, lead_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    now = datetime.now(UTC)
    async with tenant_session(admin) as s:
        s.add(
            Instance(
                id=iid,
                admin_id=admin,
                name="Acme Patios",
                state=LucielState.active,
                # Unique per call: embed keys are structurally unique since 0067, and
                # some tests seed twice for one tenant to get two conversations.
                embed_key_public_id=f"vm_live_{iid.hex[:12]}",
                channels=[{"id": c, "enabled": True} for c in ALL_CHANNELS],
                tools=[],
                escalation={},
                personality={},
            )
        )
        s.add(
            Lead(
                id=lead_id,
                admin_id=admin,
                instance_id=iid,
                name="Dana",
                contact_identifier=contact,
                state=LeadState.active,
            )
        )
        for kind, value in identifiers:
            s.add(
                LeadIdentifier(
                    id=uuid.uuid4(),
                    admin_id=admin,
                    instance_id=iid,
                    lead_id=lead_id,
                    kind=kind,
                    value=value,
                    confirmed_at=now,
                    source="transport",
                )
            )
        s.add(
            SessionModel(
                id=sid,
                admin_id=admin,
                instance_id=iid,
                channel=channel,
                mode=mode,
                resolved_lead_id=lead_id,
                summary="Asked about a quote",
                **session_fields,  # type: ignore[arg-type]
            )
        )
        if sms_status is not None:
            s.add(
                InstanceConnection(
                    connection_id=uuid.uuid4(),
                    admin_id=admin,
                    instance_id=iid,
                    connection_type="sms_sender",
                    auth_class="api_key",
                    provider="twilio",
                    status=sms_status,
                    secret_ref=secret_ref_for(admin, iid, "sms_sender"),
                    non_secret_config={"destination": TENANT_NUMBER},
                )
            )
        if email_provider is not None:
            s.add(
                InstanceConnection(
                    connection_id=uuid.uuid4(),
                    admin_id=admin,
                    instance_id=iid,
                    connection_type="email_sender",
                    auth_class="oauth_token" if email_provider == "outlook" else "api_key",
                    provider=email_provider,
                    status=email_status,
                    secret_ref=secret_ref_for(admin, iid, "email_sender"),
                    non_secret_config={"address": TENANT_EMAIL, "destination": TENANT_EMAIL},
                )
            )
        if meta_status is not None:
            s.add(
                InstanceConnection(
                    connection_id=uuid.uuid4(),
                    admin_id=admin,
                    instance_id=iid,
                    connection_type="channel_auth",
                    auth_class="oauth_token",
                    provider="meta",
                    status=meta_status,
                    secret_ref=secret_ref_for(admin, iid, "channel_auth"),
                    non_secret_config={"destinations": {"messenger": PAGE_ID}},
                )
            )
        await s.flush()
    return Seeded(iid, sid, lead_id)


class FakeTwilio:
    """Tenant Twilio stand-in: records the one send; ``accept=False`` models Twilio
    handing back no SID (the adapter reports ``twilio_rejected``)."""

    def __init__(self, *, accept: bool = True) -> None:
        self.accept = accept
        self.sent: dict[str, str] = {}

    async def send_sms(self, from_number: str, to: str, body: str) -> str | None:
        self.sent.update(from_number=from_number, to=to, body=body)
        return "SM-takeover-1" if self.accept else None


@pytest.fixture
def twilio(monkeypatch) -> FakeTwilio:
    from app.channels import tenant_twilio

    fake = FakeTwilio()

    async def _tenant_sender(*a, **kw):
        return fake

    monkeypatch.setattr(tenant_twilio, "build_tenant_sms_sender", _tenant_sender)
    return fake


class FakePlatformEmail:
    """Platform SES stand-in whose ``send`` accepts threading headers."""

    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    async def send(self, *, to, subject, body_text, from_address=None, headers=None, tags=None):
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


@pytest.fixture
def platform_email(monkeypatch) -> FakePlatformEmail:
    from app.notifications import email_sender as sender_mod

    fake = FakePlatformEmail()
    monkeypatch.setattr(sender_mod, "build_email_sender", lambda: fake)
    return fake


async def _take_over_and_reply(client, sid: uuid.UUID, text: str = REPLY) -> dict:
    rt = await client.post(f"/api/v1/dashboard/conversations/{sid}/take-over")
    assert rt.status_code == 200, rt.text
    r = await client.post(f"/api/v1/dashboard/conversations/{sid}/messages", json={"text": text})
    assert r.status_code == 200, r.text
    return r.json()


async def _sent_audit_payloads(admin: uuid.UUID) -> list[dict]:
    async with tenant_session(admin) as s:
        rows = (
            (
                await s.execute(
                    select(AdminAuditLog)
                    .where(
                        AdminAuditLog.admin_id == admin,
                        AdminAuditLog.event_type == "human_agent_message_sent",
                    )
                    .order_by(AdminAuditLog.created_at)
                )
            )
            .scalars()
            .all()
        )
    return [dict(r.event_payload) for r in rows]


# --- item 1: the destination is the SESSION's channel kind ---------------------------- #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_sms_reply_to_a_widget_then_sms_unified_lead_goes_to_the_phone(client, twilio):
    """The live condition: Dana first wrote on the widget (record keyed on her email),
    later texted; the unification kept the older email-keyed row and re-pointed her
    phone into lead_identifiers. The owner's reply on the SMS conversation used to be
    sent to ``contact_identifier`` — the email — as if it were a phone number."""
    admin = await _signup_verify_login(client)
    seeded = await _seed(
        admin,
        channel="sms",
        contact=LEAD_EMAIL,
        identifiers=(("email", LEAD_EMAIL), ("phone", LEAD_PHONE)),
        sms_status="connected",
    )

    body = await _take_over_and_reply(client, seeded.session_id)

    assert body["delivered"] is True
    assert body["deliveryDetail"] is None
    assert twilio.sent == {"from_number": TENANT_NUMBER, "to": LEAD_PHONE, "body": REPLY}


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_sms_reply_with_no_phone_on_record_is_refused_not_rerouted(client, twilio):
    """An email-only record on an SMS session (the identifier rows were lost, or the
    session predates them): nothing this channel can deliver to. The reply is kept
    in the transcript and the owner reads the honest wire word; no text is attempted
    to the email, and nothing goes to the email channel either."""
    admin = await _signup_verify_login(client)
    seeded = await _seed(
        admin,
        channel="sms",
        contact=LEAD_EMAIL,
        identifiers=(("email", LEAD_EMAIL),),
        sms_status="connected",
    )

    body = await _take_over_and_reply(client, seeded.session_id)

    assert body["delivered"] is False
    assert body["deliveryDetail"] == "no_recipient_for_channel"
    assert twilio.sent == {}
    msgs = await client.get(f"/api/v1/dashboard/conversations/{seeded.session_id}/messages")
    assert [m["role"] for m in msgs.json()] == ["human_agent"]
    (payload,) = await _sent_audit_payloads(admin)
    assert payload["delivered"] is False
    assert payload["delivery_detail"] == "no_recipient_for_channel"
    assert LEAD_EMAIL not in str(payload)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_email_reply_to_a_phone_keyed_lead_goes_to_the_email_identifier(
    client, platform_email
):
    """The mirror case: an SMS-first lead (record keyed on the phone) who later wrote
    an email. The reply on the EMAIL conversation goes to the email identifier, not
    to the phone number the record happens to be keyed on."""
    admin = await _signup_verify_login(client)
    seeded = await _seed(
        admin,
        channel="email",
        contact=LEAD_PHONE,
        identifiers=(("phone", LEAD_PHONE), ("email", LEAD_EMAIL)),
        email_provider="ses",
    )

    body = await _take_over_and_reply(client, seeded.session_id)

    assert body["delivered"] is True
    (sent,) = platform_email.sent
    assert sent["to"] == LEAD_EMAIL
    assert sent["from_address"] == TENANT_EMAIL


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_volunteered_phone_beside_an_email_key_reaches_the_sms_reply(client, twilio):
    """The record's own ``phone`` column (a typed number, F111) is an identifier of
    the SMS kind too — used before any identifier row, never the email key."""
    admin = await _signup_verify_login(client)
    seeded = await _seed(admin, channel="sms", contact=LEAD_EMAIL, sms_status="connected")
    async with tenant_session(admin) as s:
        lead = (await s.execute(select(Lead).where(Lead.id == seeded.lead_id))).scalar_one()
        lead.phone = LEAD_PHONE
        await s.flush()

    body = await _take_over_and_reply(client, seeded.session_id)

    assert body["delivered"] is True
    assert twilio.sent["to"] == LEAD_PHONE


# --- items 2 + 3: the email reply threads and is signed by a person ------------------ #
@pytest.fixture
def graph(monkeypatch) -> list[dict]:
    """A connected BYO mailbox whose Graph calls land in a fake httpx client, so the
    endpoint and payload the reply is sent with can be asserted."""
    import httpx
    from app.channels import outlook_sender

    calls: list[dict] = []

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc) -> None:
            return None

        async def post(self, url, *, json=None, headers=None):
            calls.append({"url": url, "json": json, "headers": headers})
            return SimpleNamespace(status_code=202)

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    async def _build(session, admin_id, instance_id, *, store=None):
        return outlook_sender.GraphMailSender("ms-tok")

    monkeypatch.setattr(outlook_sender, "build_tenant_outlook_sender", _build)
    return calls


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_byo_mailbox_reply_threads_under_the_customers_message(client, graph):
    """The inbound Graph id persisted at ingress is the ``/messages/{id}/reply``
    handle: the reply hangs under Dana's own mail in her client, from the owner's
    mailbox — not a new "Re: your inquiry" thread through /me/sendMail."""
    admin = await _signup_verify_login(client)
    seeded = await _seed(
        admin,
        channel="email",
        contact=LEAD_EMAIL,
        email_provider="outlook",
        inbound_subject="Patio quote",
        inbound_message_id="AAMkAGE1",
    )

    body = await _take_over_and_reply(client, seeded.session_id)

    assert body["delivered"] is True
    assert body["deliveryDetail"] is None
    (call,) = graph
    assert call["url"].endswith("/me/messages/AAMkAGE1/reply")
    assert call["headers"] == {"Authorization": "Bearer ms-tok"}
    comment = call["json"]["comment"]
    assert comment.startswith(REPLY)
    # Item 3: a person's words are never signed as an AI's; the opt-out promise stays.
    assert "written by an AI assistant" not in comment
    assert "Sent by" in comment
    assert "Reply STOP to opt out" in comment


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_byo_mailbox_reply_with_no_inbound_context_is_a_new_mail_to_the_lead(client, graph):
    """A session from before the reply context existed has nothing to thread under:
    the reply is still sent, as a new mail to the lead's address, and says so
    through the endpoint it takes — never a fabricated reply handle."""
    admin = await _signup_verify_login(client)
    seeded = await _seed(admin, channel="email", contact=LEAD_EMAIL, email_provider="outlook")

    body = await _take_over_and_reply(client, seeded.session_id)

    assert body["delivered"] is True
    (call,) = graph
    assert call["url"].endswith("/me/sendMail")
    message = call["json"]["message"]
    assert message["subject"] == "Re: your inquiry"
    assert message["toRecipients"] == [{"emailAddress": {"address": LEAD_EMAIL}}]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_legacy_ses_reply_threads_with_headers_and_is_signed_by_the_business(
    client, platform_email
):
    """No BYO mailbox: the reply leaves through the platform SES sender AS the tenant's
    provisioned address, on the customer's subject, with In-Reply-To / References
    set to their Message-ID — and the human footer."""
    admin = await _signup_verify_login(client)
    seeded = await _seed(
        admin,
        channel="email",
        contact=LEAD_EMAIL,
        email_provider="ses",
        inbound_subject="Patio quote",
        inbound_message_id="<abc123@lead.example>",
    )

    body = await _take_over_and_reply(client, seeded.session_id)

    assert body["delivered"] is True
    (sent,) = platform_email.sent
    assert sent["to"] == LEAD_EMAIL
    assert sent["from_address"] == TENANT_EMAIL
    assert sent["subject"] == "Re: Patio quote"
    assert sent["headers"] == {
        "In-Reply-To": "<abc123@lead.example>",
        "References": "<abc123@lead.example>",
    }
    text = str(sent["body"])
    assert text.startswith(REPLY)
    assert "written by an AI assistant" not in text
    assert "Sent by" in text
    assert "Reply STOP to opt out" in text


# --- item 7: the Meta reply goes out AS the surface the conversation happened on ----- #
class FakeMetaSender:
    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []

    async def send_message(self, *, channel, sender_id, recipient_id, body):
        self.sent.append(
            {"channel": channel, "sender_id": sender_id, "recipient_id": recipient_id, "body": body}
        )
        return f"mid-{len(self.sent)}"


@pytest.fixture
def meta_sender(monkeypatch) -> FakeMetaSender:
    from app.channels import meta_sender as sender_mod

    fake = FakeMetaSender()

    async def _build(session, admin_id, instance_id, *, connection_type="channel_auth", store=None):
        return fake

    monkeypatch.setattr(sender_mod, "build_tenant_meta_sender", _build)
    return fake


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_messenger_reply_goes_out_as_the_persisted_surface_to_the_persisted_sender(
    client, meta_sender
):
    """The lead record is email-keyed (unified with an earlier email conversation);
    the Messenger conversation's page and the customer's page-scoped id were
    persisted at ingress. The reply is sent AS that page TO that id — not resolved
    from the channel name, not sent to the email."""
    admin = await _signup_verify_login(client)
    seeded = await _seed(
        admin,
        channel="messenger",
        contact=LEAD_EMAIL,
        identifiers=(("email", LEAD_EMAIL),),
        meta_status="connected",
        meta_destination_id=PAGE_ID,
        meta_sender_id=PSID,
    )

    body = await _take_over_and_reply(client, seeded.session_id)

    assert body["delivered"] is True
    assert body["deliveryDetail"] is None
    assert meta_sender.sent == [
        {"channel": "messenger", "sender_id": PAGE_ID, "recipient_id": PSID, "body": REPLY}
    ]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_messenger_reply_on_an_expired_grant_reads_meta_reconnect_needed(
    client, meta_sender
):
    """The grant that owns the page is expired: Graph would reject its token. The
    reply is not attempted and the owner reads the wire word that names the fix —
    not ``send_failed`` with "try again" copy."""
    admin = await _signup_verify_login(client)
    seeded = await _seed(
        admin,
        channel="messenger",
        contact=PSID,
        meta_status="expired",
        meta_destination_id=PAGE_ID,
        meta_sender_id=PSID,
    )

    body = await _take_over_and_reply(client, seeded.session_id)

    assert body["delivered"] is False
    assert body["deliveryDetail"] == "meta_reconnect_needed"
    assert meta_sender.sent == []
    (payload,) = await _sent_audit_payloads(admin)
    assert payload["delivery_detail"] == "meta_reconnect_needed"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_legacy_messenger_session_on_an_email_keyed_lead_is_refused_not_rerouted(
    client, meta_sender
):
    """A Messenger session from before the sender id was persisted, whose lead record
    is keyed on an email: there is no page-scoped id to send to, and the email is
    not one. Refused honestly rather than handed to Graph as a recipient."""
    admin = await _signup_verify_login(client)
    seeded = await _seed(
        admin,
        channel="messenger",
        contact=LEAD_EMAIL,
        identifiers=(("email", LEAD_EMAIL),),
        meta_status="connected",
    )

    body = await _take_over_and_reply(client, seeded.session_id)

    assert body["delivered"] is False
    assert body["deliveryDetail"] == "no_recipient_for_channel"
    assert meta_sender.sent == []


# --- item 4: an ended session is not takeover-able and takes no reply --------------- #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_take_over_on_an_ended_session_is_refused_with_session_ended(client, twilio):
    """The sweep sealed Dana's SMS conversation last night. The take-over button used
    to switch it to human_controlled anyway — a person "holding" a conversation whose
    transport is no longer waiting on it, and a budget count for nothing. Refused as
    a typed 409 the page can name; the row is untouched and reads ``endedAt``."""
    admin = await _signup_verify_login(client)
    ended = datetime.now(UTC) - timedelta(hours=8)
    seeded = await _seed(
        admin, channel="sms", contact=LEAD_PHONE, sms_status="connected", ended_at=ended
    )

    r = await client.post(f"/api/v1/dashboard/conversations/{seeded.session_id}/take-over")

    assert r.status_code == 409, r.text
    assert "session_ended" in r.json()["message"]
    (row,) = (await client.get("/api/v1/dashboard/conversations")).json()
    assert row["mode"] == "ai"
    assert abs(datetime.fromisoformat(row["endedAt"]) - ended) < timedelta(seconds=1)
    assert row["countedForBilling"] is False


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_reply_into_an_ended_session_is_refused_before_anything_is_written(client, twilio):
    """Taken over live, then ended (the sweep sealed it while the owner had the page
    open): the reply box is still on screen. Nothing is persisted, sent or audited —
    a text out of nowhere hours after the conversation closed is not a takeover reply."""
    admin = await _signup_verify_login(client)
    seeded = await _seed(
        admin,
        channel="sms",
        contact=LEAD_PHONE,
        sms_status="connected",
        mode="human_controlled",
        ended_at=datetime.now(UTC) - timedelta(minutes=5),
    )

    r = await client.post(
        f"/api/v1/dashboard/conversations/{seeded.session_id}/messages", json={"text": REPLY}
    )

    assert r.status_code == 409, r.text
    assert "session_ended" in r.json()["message"]
    assert twilio.sent == {}
    msgs = await client.get(f"/api/v1/dashboard/conversations/{seeded.session_id}/messages")
    assert msgs.json() == []
    assert await _sent_audit_payloads(admin) == []


# --- item 5: the delivery outcome lives on the row and replays on reopen ------------- #
@pytest.fixture
def rejecting_twilio(monkeypatch) -> FakeTwilio:
    from app.channels import tenant_twilio

    fake = FakeTwilio(accept=False)

    async def _tenant_sender(*a, **kw):
        return fake

    monkeypatch.setattr(tenant_twilio, "build_tenant_sms_sender", _tenant_sender)
    return fake


async def _messages(client, sid: uuid.UUID) -> list[dict]:
    r = await client.get(f"/api/v1/dashboard/conversations/{sid}/messages")
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_twilio_rejected_reply_reads_failed_after_reopen(client, rejecting_twilio):
    """Twilio handed back no SID. The response said so once; the row did not, so on
    reopen the reply sat in the transcript like any delivered message. The row now
    carries the outcome and GET .../messages replays it as failed — beside the
    customer's own row, which carries no outcome at all (it is not a send)."""
    admin = await _signup_verify_login(client)
    seeded = await _seed(admin, channel="sms", contact=LEAD_PHONE, sms_status="connected")
    async with tenant_session(admin) as s:
        s.add(
            Transcript(
                id=uuid.uuid4(),
                admin_id=admin,
                instance_id=seeded.instance_id,
                session_id=seeded.session_id,
                role="lead",
                text="Can you do Tuesday?",
            )
        )
        await s.flush()

    body = await _take_over_and_reply(client, seeded.session_id)
    assert body["delivered"] is False
    assert body["deliveryDetail"] == "send_failed"
    assert body["message"]["deliveryStatus"] == "failed"

    lead_row, reply_row = await _messages(client, seeded.session_id)
    assert (lead_row["role"], reply_row["role"]) == ("lead", "human_agent")
    assert lead_row["deliveryStatus"] is None
    assert lead_row["deliveryDetail"] is None
    assert lead_row["deliveredAt"] is None
    assert reply_row["deliveryStatus"] == "failed"
    assert reply_row["deliveryDetail"] == "send_failed"
    assert reply_row["deliveredAt"] is None
    async with tenant_session(admin) as s:
        stored = (
            await s.execute(
                select(Transcript).where(Transcript.id == uuid.UUID(reply_row["messageId"]))
            )
        ).scalar_one()
    # The provider's own reason stays on the row for the record; the wire stays closed.
    assert stored.delivery_status == "failed"
    assert stored.delivery_detail == "twilio_rejected"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_delivered_reply_replays_as_delivered_with_the_time_it_left(client, twilio):
    admin = await _signup_verify_login(client)
    seeded = await _seed(admin, channel="sms", contact=LEAD_PHONE, sms_status="connected")

    body = await _take_over_and_reply(client, seeded.session_id)
    assert body["delivered"] is True

    (row,) = await _messages(client, seeded.session_id)
    assert row["deliveryStatus"] == "delivered"
    assert row["deliveryDetail"] is None
    assert row["deliveredAt"] is not None


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_widget_reply_replays_as_queued_not_delivered(client):
    """The widget has no push transport: the reply waits for the visitor's next
    history fetch. Queued is the honest word — not failed (nothing went wrong) and
    not delivered (nothing has reached them yet, so there is no delivered time)."""
    admin = await _signup_verify_login(client)
    seeded = await _seed(admin, channel="widget", contact=LEAD_EMAIL)

    body = await _take_over_and_reply(client, seeded.session_id)
    assert body["deliveryDetail"] == "widget_poll_only"

    (row,) = await _messages(client, seeded.session_id)
    assert row["deliveryStatus"] == "queued"
    assert row["deliveryDetail"] == "widget_poll_only"
    assert row["deliveredAt"] is None


# --- item 6: the list says which sessions were billed ------------------------------- #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_conversation_list_labels_a_never_billed_session(client):
    """Two conversations: one reached the fire-once trigger (a ``counted_sessions``
    row), one was refused at cap before its first model call. The list used to show
    both the same way; the unbilled one is now labelled, so the owner does not read
    an at-cap refusal as a conversation they paid for."""
    admin = await _signup_verify_login(client)
    now = datetime.now(UTC)
    billed = await _seed(admin, channel="widget", contact=LEAD_EMAIL, last_message_at=now)
    unbilled = await _seed(
        admin, channel="sms", contact=LEAD_PHONE, last_message_at=now - timedelta(hours=1)
    )
    async with tenant_session(admin) as s:
        s.add(
            CountedSession(
                id=uuid.uuid4(),
                admin_id=admin,
                instance_id=billed.instance_id,
                session_id=billed.session_id,
                billing_period_start=date.today(),
            )
        )
        await s.flush()

    rows = (await client.get("/api/v1/dashboard/conversations")).json()

    assert [r["sessionId"] for r in rows] == [str(billed.session_id), str(unbilled.session_id)]
    assert [r["countedForBilling"] for r in rows] == [True, False]
    assert [r["endedAt"] for r in rows] == [None, None]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_taking_over_an_unbilled_session_counts_it_and_says_so(client):
    """Becoming human-controlled IS the fire-once trigger (§3.4.1b): the take-over
    response reports the session as counted, read back from the ledger after the
    claim — never carried in from before it."""
    admin = await _signup_verify_login(client)
    seeded = await _seed(admin, channel="widget", contact=LEAD_EMAIL)
    (before,) = (await client.get("/api/v1/dashboard/conversations")).json()
    assert before["countedForBilling"] is False

    r = await client.post(f"/api/v1/dashboard/conversations/{seeded.session_id}/take-over")

    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "human_controlled"
    assert r.json()["countedForBilling"] is True
