"""The delete drain works on EVERY channel, not only the widget (round 7 WP-5, item 1).

Live condition this file models: ``delete_luciel`` used to set every connection
``revoked`` at the delete instant, and ``channel_resolve_destination`` filters
``status <> 'revoked'`` — so from that instant every non-widget inbound (SMS, email,
WhatsApp, Messenger, Instagram) resolved to NO tenant and was dropped as
``unresolvable_destination``. The §3.6.3 "in-flight session finishes on its channel"
drain was reachable only on the widget; a customer mid-text got silence forever.

Now the delete writes ``quiescing``: the resolver (migration 0068) keeps routing the
destination to the tenant for INBOUND, the runtime drain gate admits the in-flight
session and refuses new ones, the outbound seams judge a quiescing row by its stashed
pre-delete status, and restore / the day-30 cascade treat quiescing exactly as revoked.
A quiescing row holds NO destination claim (c21 semantics kept): the claim gate asks
the resolver to exclude quiescing rows and the 0045 index does too.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from app.channels import inbound_dispatch, outbound, routing
from app.channels.base import InboundMessage, InboundRejected
from app.channels.mailbox_poll import poll_mailbox
from app.channels.runtime_entry import run_inbound_message
from app.connections import gate
from app.connections.secrets import LocalSecretStore, secret_ref_for
from app.db.session import tenant_session
from app.lifecycle import cascade, state_machine
from app.models.connection import InstanceConnection
from app.models.conversation import EscalationEvent
from app.models.conversation import Session as ConvSession
from app.models.instance import Account, AccountState, Instance, LucielState
from app.notifications import escalation_dispatch
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

NUM = "+16045550188"
TEXT_CHANNELS = ["sms", "email", "whatsapp", "messenger", "instagram"]
_IDENTIFIER = {
    "sms": "+14165550143",
    "whatsapp": "+14165550143",
    "messenger": "PSID_1",
    "instagram": "IGSID_1",
    "email": "dana@lead.example",
}
_SECOND_IDENTIFIER = {
    "sms": "+14165550199",
    "whatsapp": "+14165550199",
    "messenger": "PSID_2",
    "instagram": "IGSID_2",
    "email": "erin@lead.example",
}


async def _seed(admin: uuid.UUID, *, state: LucielState = LucielState.active) -> uuid.UUID:
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
                name="Acme Patios",
                state=state,
                embed_key_public_id=f"vm_live_{admin.hex[:8]}",
                channels=[{"id": c, "enabled": True} for c in ("widget", *TEXT_CHANNELS, "voice")],
                tools=[],
                escalation={},
                personality={},
            )
        )
        await s.flush()
    return iid


async def _add_connection(
    admin: uuid.UUID,
    iid: uuid.UUID,
    *,
    connection_type: str = "sms_sender",
    provider: str = "twilio",
    status: str = "connected",
    status_stashed: str | None = None,
    destination: str | None = NUM,
    secret_ref: str | None = None,
    created_at: datetime | None = None,
) -> uuid.UUID:
    cid = uuid.uuid4()
    async with tenant_session(admin) as s:
        row = InstanceConnection(
            connection_id=cid,
            admin_id=admin,
            instance_id=iid,
            connection_type=connection_type,
            auth_class="api_key",
            provider=provider,
            status=status,
            status_stashed=status_stashed,
            secret_ref=secret_ref,
            non_secret_config={"destination": destination} if destination else {},
        )
        if created_at is not None:
            row.created_at = created_at
        s.add(row)
        await s.flush()
    return cid


async def _rows(admin: uuid.UUID) -> dict[uuid.UUID, InstanceConnection]:
    async with tenant_session(admin) as s:
        return {
            r.connection_id: r
            for r in (
                await s.execute(
                    select(InstanceConnection).where(InstanceConnection.admin_id == admin)
                )
            )
            .scalars()
            .all()
        }


def _inbound(admin: uuid.UUID, iid: uuid.UUID, channel: str, who: str) -> InboundMessage:
    return InboundMessage(
        admin_id=admin,
        instance_id=iid,
        channel=channel,
        session_id=None,
        customer_identifier=who,
        body="Are you open on Sunday?",
        channel_metadata={},
    )


async def _session_count(admin: uuid.UUID) -> int:
    async with tenant_session(admin) as s:
        return (
            await s.scalar(
                select(func.count()).select_from(ConvSession).where(ConvSession.admin_id == admin)
            )
        ) or 0


# --------------------------------------------------------------------------- #
# The delete writes quiescing
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_delete_writes_quiescing_with_the_pre_delete_status_stashed():
    admin = uuid.uuid4()
    iid = await _seed(admin)
    cid = await _add_connection(admin, iid, status="pending_carrier_registration")
    async with tenant_session(admin) as s:
        await state_machine.delete_luciel(s, admin, actor_user_id=admin)
    row = (await _rows(admin))[cid]
    assert row.status == state_machine.QUIESCING
    assert row.status != "revoked", "revoked would make the resolver drop every inbound"
    assert row.status_stashed == "pending_carrier_registration"
    assert state_machine.serving_status(row) == "pending_carrier_registration"


def test_serving_status_never_reads_a_stashless_quiescing_row_as_connected():
    row = InstanceConnection(
        connection_id=uuid.uuid4(),
        admin_id=uuid.uuid4(),
        instance_id=uuid.uuid4(),
        connection_type="calendar",
        auth_class="oauth_token",
        provider="google_calendar",
        status=state_machine.QUIESCING,
    )
    assert state_machine.serving_status(row) == "unconfigured"
    row.status = "connected"
    assert state_machine.serving_status(row) == "connected"


def test_the_wire_status_of_a_draining_row_is_what_a_revoked_one_shows():
    """The frozen wire enum has no drain value; ``error`` would be a lie."""
    assert gate._to_wire_status(state_machine.QUIESCING) == "revoked"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_broker_gate_hides_a_draining_row_exactly_as_a_revoked_one():
    """Tools do not dispatch on a deleted Luciel: the universal gate reads a quiescing
    calendar as absent (``unconfigured``), which is what the row's old ``revoked``
    value gave — the drain answers the customer, it does not book on their behalf."""
    admin = uuid.uuid4()
    iid = await _seed(admin, state=LucielState.luciel_grace_window)
    await _add_connection(
        admin,
        iid,
        connection_type="calendar",
        provider="google_calendar",
        status=state_machine.QUIESCING,
        status_stashed="connected",
        destination=None,
    )
    async with tenant_session(admin) as s:
        status = await gate.connection_status(
            s, admin_id=admin, instance_id=iid, connection_type="calendar"
        )
        assert status is None
        assert (await gate.channel_connection_statuses(s, admin_id=admin, instance_id=iid)) == {
            channel: "unconfigured" for channel in gate._CHANNEL_CONNECTION_TYPE
        }


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_draining_row_keeps_its_single_active_slot():
    """``uq_conn_active_type`` still counts a quiescing row as the type's one live row:
    a re-attach during grace must REUSE the draining row (every non-revoked reader in
    the connections/senders is single-row), never sit beside it as a second one."""
    admin = uuid.uuid4()
    iid = await _seed(admin, state=LucielState.luciel_grace_window)
    await _add_connection(
        admin,
        iid,
        connection_type="crm",
        provider="hubspot",
        status=state_machine.QUIESCING,
        status_stashed="connected",
        destination=None,
    )
    with pytest.raises(IntegrityError, match="uq_conn_active_type"):
        await _add_connection(
            admin,
            iid,
            connection_type="crm",
            provider="hubspot",
            status="connected",
            destination=None,
        )


# --------------------------------------------------------------------------- #
# The resolver (SQL, migration 0068)
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_resolver_routes_inbound_to_a_quiescing_row_but_never_a_revoked_one():
    admin = uuid.uuid4()
    iid = await _seed(admin, state=LucielState.luciel_grace_window)
    await _add_connection(admin, iid, status=state_machine.QUIESCING, status_stashed="connected")
    async with tenant_session(admin) as s:
        ctx = await routing.resolve_destination(s, "sms_sender", NUM)
        assert (ctx.admin_id, ctx.instance_id) == (admin, iid)

    other = uuid.uuid4()
    other_iid = await _seed(other, state=LucielState.luciel_grace_window)
    await _add_connection(other, other_iid, status="revoked", destination="+16045550100")
    async with tenant_session(other) as s:
        with pytest.raises(InboundRejected):
            await routing.resolve_destination(s, "sms_sender", "+16045550100")


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_quiescing_row_routes_inbound_but_holds_no_destination_claim():
    """c21 kept: a deleted Luciel releases its number for the grace window."""
    admin = uuid.uuid4()
    iid = await _seed(admin, state=LucielState.luciel_grace_window)
    await _add_connection(admin, iid, status=state_machine.QUIESCING, status_stashed="connected")
    async with tenant_session(admin) as s:
        assert (await routing.resolve_destination(s, "sms_sender", NUM)).admin_id == admin
        assert await routing.owner_of_destination(s, "sms_sender", NUM) is None


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_newer_live_claim_wins_routing_over_an_older_quiescing_row():
    """Tenant B claimed the number during tenant A's grace: from that moment inbound is
    B's (newest claim wins, 0044) — and the 0045 index lets B's row exist."""
    a, b = uuid.uuid4(), uuid.uuid4()
    a_iid = await _seed(a, state=LucielState.luciel_grace_window)
    b_iid = await _seed(b)
    old = datetime.now(UTC) - timedelta(days=2)
    await _add_connection(
        a, a_iid, status=state_machine.QUIESCING, status_stashed="connected", created_at=old
    )
    await _add_connection(b, b_iid, status="connected", created_at=old + timedelta(days=1))
    async with tenant_session(b) as s:
        assert (await routing.resolve_destination(s, "sms_sender", NUM)).admin_id == b
        assert await routing.owner_of_destination(s, "sms_sender", NUM) == b


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_migration_0068_shape_is_what_the_app_calls():
    """The 3-arg resolver (default admits quiescing) and the index that excludes
    quiescing rows are both present — the two halves the claim gate relies on."""
    async with tenant_session(uuid.uuid4()) as s:
        args = (
            (
                await s.execute(
                    text(
                        "SELECT pg_get_function_arguments(p.oid) FROM pg_proc p "
                        "WHERE p.proname = 'channel_resolve_destination'"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(args) == 1, args
        assert "p_include_quiescing boolean DEFAULT true" in args[0]
        index_def = (
            await s.execute(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_sms_destination'")
            )
        ).scalar_one()
        assert "'quiescing'" in index_def and "'revoked'" in index_def


# --------------------------------------------------------------------------- #
# The runtime drain gate, per channel kind
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("channel", TEXT_CHANNELS)
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_drain_admits_the_in_flight_session_and_refuses_new_ones(channel):
    admin = uuid.uuid4()
    iid = await _seed(admin)
    who = _IDENTIFIER[channel]
    async with tenant_session(admin) as s:
        opened = await run_inbound_message(s, _inbound(admin, iid, channel, who))
    assert opened.is_new_session and opened.session_id
    assert await _session_count(admin) == 1

    async with tenant_session(admin) as s:
        await state_machine.delete_luciel(s, admin, actor_user_id=admin)

    # The customer mid-conversation keeps getting answered on their channel.
    async with tenant_session(admin) as s:
        follow_up = await run_inbound_message(s, _inbound(admin, iid, channel, who))
    assert follow_up.session_id == opened.session_id
    assert follow_up.render_state != "paused"
    assert not follow_up.is_new_session
    assert await _session_count(admin) == 1

    # A NEW conversation gets the honest drain refusal: no session, no LLM, nothing said.
    async with tenant_session(admin) as s:
        refused = await run_inbound_message(
            s, _inbound(admin, iid, channel, _SECOND_IDENTIFIER[channel])
        )
    assert (refused.reply_text, refused.render_state, refused.session_id) == ("", "paused", "")
    assert await _session_count(admin) == 1


# --------------------------------------------------------------------------- #
# The outbound seams judge a quiescing row by its stashed status
# --------------------------------------------------------------------------- #


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


@pytest.fixture
def email_sender(monkeypatch):
    from app.notifications import email_sender as sender_mod

    class FakeEmailSender:
        def __init__(self) -> None:
            self.sent: list[dict[str, str | None]] = []

        async def send(self, *, to, subject, body_text, from_address=None):
            self.sent.append({"to": to, "subject": subject})
            return f"ses-{len(self.sent)}"

    fake = FakeEmailSender()
    monkeypatch.setattr(sender_mod, "build_email_sender", lambda: fake)
    return fake


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_sms_drain_reply_leaves_on_a_quiescing_number(twilio):
    admin = uuid.uuid4()
    iid = await _seed(admin, state=LucielState.luciel_grace_window)
    await _add_connection(admin, iid, status=state_machine.QUIESCING, status_stashed="connected")
    async with tenant_session(admin) as s:
        result = await outbound.send_outbound_reply(
            s,
            admin_id=admin,
            instance_id=iid,
            channel="sms",
            to="+15557778888",
            body="We open at 9.",
        )
    assert result.delivered is True
    assert twilio[0]["from_number"] == NUM


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_quiescing_number_that_was_not_operable_before_the_delete_still_refuses(twilio):
    admin = uuid.uuid4()
    iid = await _seed(admin, state=LucielState.luciel_grace_window)
    await _add_connection(admin, iid, status=state_machine.QUIESCING, status_stashed="expired")
    async with tenant_session(admin) as s:
        result = await outbound.send_outbound_reply(
            s,
            admin_id=admin,
            instance_id=iid,
            channel="sms",
            to="+15557778888",
            body="We open at 9.",
        )
    assert result.delivered is False
    assert result.detail == "sms_sender_not_operable"
    assert twilio == []


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_email_drain_reply_leaves_on_a_quiescing_address(email_sender):
    admin = uuid.uuid4()
    iid = await _seed(admin, state=LucielState.luciel_grace_window)
    async with tenant_session(admin) as s:
        s.add(
            InstanceConnection(
                connection_id=uuid.uuid4(),
                admin_id=admin,
                instance_id=iid,
                connection_type="email_sender",
                auth_class="api_key",
                provider="ses",
                status=state_machine.QUIESCING,
                status_stashed="connected",
                non_secret_config={"address": "hello@acmepatios.example"},
            )
        )
    async with tenant_session(admin) as s:
        result = await outbound.send_outbound_reply(
            s,
            admin_id=admin,
            instance_id=iid,
            channel="email",
            to="dana@lead.example",
            body="We open at 9.",
            subject="Hours?",
        )
    assert result.delivered is True
    assert email_sender.sent[0]["to"] == "dana@lead.example"


# --------------------------------------------------------------------------- #
# Meta (WhatsApp / Messenger / Instagram): the grant that owns the surface is judged
# by its pre-delete status too. The gate was written on a parallel branch and read the
# raw status, so the drain reply was refused with meta_reconnect_needed on all three
# channels and the owner was paged to reconnect a healthy grant.
# --------------------------------------------------------------------------- #

#: channel -> (the grant's connection type, its provider, the customer's identifier).
_META_GRANT = {
    "whatsapp": ("channel_auth", "meta", "15557778888"),
    "messenger": ("channel_auth", "meta", "PSID_1"),
    "instagram": ("instagram_auth", "instagram", "IGSID_1"),
}
META_CHANNELS = sorted(_META_GRANT)
SURFACE_ID = "surface-4411"


class _FakeMetaSender:
    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []
        self.grants: list[str] = []

    async def send_message(self, *, channel, sender_id, recipient_id, body):
        self.sent.append({"channel": channel, "sender_id": sender_id, "recipient_id": recipient_id})
        return f"mid-{len(self.sent)}"


@pytest.fixture
def meta_sender(monkeypatch) -> _FakeMetaSender:
    from app.channels import meta_sender as sender_mod

    fake = _FakeMetaSender()

    async def _build(session, admin_id, instance_id, *, connection_type="channel_auth", store=None):
        fake.grants.append(connection_type)
        return fake

    monkeypatch.setattr(sender_mod, "build_tenant_meta_sender", _build)
    return fake


async def _add_meta_grant(
    admin: uuid.UUID, iid: uuid.UUID, channel: str, *, status: str, status_stashed: str | None
) -> None:
    """The grant that OWNS ``SURFACE_ID`` for ``channel``. The ``destinations`` map is
    what pairs the surface with its grant: without it the Instagram case would fall
    back to a ``channel_auth`` row that does not exist and skip the gate vacuously."""
    connection_type, provider, _ = _META_GRANT[channel]
    async with tenant_session(admin) as s:
        s.add(
            InstanceConnection(
                connection_id=uuid.uuid4(),
                admin_id=admin,
                instance_id=iid,
                connection_type=connection_type,
                auth_class="oauth_token",
                provider=provider,
                status=status,
                status_stashed=status_stashed,
                secret_ref=secret_ref_for(admin, iid, connection_type),
                non_secret_config={"destinations": {channel: SURFACE_ID}},
            )
        )
        await s.flush()


@pytest.mark.parametrize("channel", META_CHANNELS)
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_meta_drain_reply_leaves_on_a_quiescing_grant(channel, meta_sender):
    admin = uuid.uuid4()
    iid = await _seed(admin, state=LucielState.luciel_grace_window)
    await _add_meta_grant(
        admin, iid, channel, status=state_machine.QUIESCING, status_stashed="connected"
    )
    connection_type, _, customer = _META_GRANT[channel]
    async with tenant_session(admin) as s:
        result = await outbound.send_outbound_reply(
            s,
            admin_id=admin,
            instance_id=iid,
            channel=channel,
            to=customer,
            body="We open at 9.",
            sender_id=SURFACE_ID,
        )
    assert result.delivered is True, result.detail
    assert meta_sender.sent == [
        {"channel": channel, "sender_id": SURFACE_ID, "recipient_id": customer}
    ]
    assert meta_sender.grants == [connection_type], "sent with the grant that owns the surface"


@pytest.mark.parametrize("stashed", [None, "expired", "error", "pending_destination"])
@pytest.mark.parametrize("channel", META_CHANNELS)
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_quiescing_meta_grant_that_could_not_send_before_the_delete_still_refuses(
    channel, stashed, meta_sender
):
    """No stash (never written by ``delete_luciel``; defensive) and a stashed
    not-connected status both stay refused: the drain does not turn a broken grant
    into a sending one."""
    admin = uuid.uuid4()
    iid = await _seed(admin, state=LucielState.luciel_grace_window)
    await _add_meta_grant(
        admin, iid, channel, status=state_machine.QUIESCING, status_stashed=stashed
    )
    async with tenant_session(admin) as s:
        result = await outbound.send_outbound_reply(
            s,
            admin_id=admin,
            instance_id=iid,
            channel=channel,
            to=_META_GRANT[channel][2],
            body="We open at 9.",
            sender_id=SURFACE_ID,
        )
    assert result.delivered is False
    assert result.detail == "meta_reconnect_needed"
    assert meta_sender.sent == []


def _meta_inbound(admin: uuid.UUID, iid: uuid.UUID, channel: str, body: str) -> InboundMessage:
    surface_key = "phone_number_id" if channel == "whatsapp" else "destination_id"
    return InboundMessage(
        admin_id=admin,
        instance_id=iid,
        channel=channel,
        session_id=None,
        customer_identifier=_META_GRANT[channel][2],
        body=body,
        channel_metadata={surface_key: SURFACE_ID},
    )


@pytest.mark.parametrize("channel", META_CHANNELS)
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_customer_mid_conversation_on_meta_is_answered_after_the_delete(
    channel, meta_sender
):
    """The reported sequence end to end: the customer is mid-conversation, the owner
    deletes the Luciel, the customer writes again. The answer reaches them on their
    channel and the owner is NOT paged about a reply that could not be delivered."""
    admin = uuid.uuid4()
    iid = await _seed(admin)
    await _add_meta_grant(admin, iid, channel, status="connected", status_stashed=None)

    opened = await inbound_dispatch.run_and_reply_full(
        _meta_inbound(admin, iid, channel, "Are you open on Sunday?")
    )
    assert opened.is_new_session and opened.reply_text
    assert len(meta_sender.sent) == 1

    async with tenant_session(admin) as s:
        await state_machine.delete_luciel(s, admin, actor_user_id=admin)
    grant = next(iter((await _rows(admin)).values()))
    assert (grant.status, grant.status_stashed) == (state_machine.QUIESCING, "connected")

    follow_up = await inbound_dispatch.run_and_reply_full(
        _meta_inbound(admin, iid, channel, "And what time do you close?")
    )
    await escalation_dispatch.drain_background_notifications()
    assert follow_up.session_id == opened.session_id
    assert follow_up.reply_text, "the drain answered the in-flight customer"
    assert len(meta_sender.sent) == 2, "and the answer left on their channel"
    assert meta_sender.sent[1] == {
        "channel": channel,
        "sender_id": SURFACE_ID,
        "recipient_id": _META_GRANT[channel][2],
    }
    async with tenant_session(admin) as s:
        undeliverable = (
            await s.scalar(
                select(func.count())
                .select_from(EscalationEvent)
                .where(
                    EscalationEvent.admin_id == admin,
                    EscalationEvent.signal == "reply_undeliverable",
                )
            )
        ) or 0
    assert undeliverable == 0, "no page telling the owner to reconnect a healthy grant"


# --------------------------------------------------------------------------- #
# The BYO-mailbox poll is the email channel's inbound door: it keeps reading a
# quiescing mailbox so the in-flight thread is seen and answered
# --------------------------------------------------------------------------- #

MAILBOX = "owner@acmepatios.example"
DELTA_FINAL = "https://graph.microsoft.com/v1.0/delta-final"


class _FakeGraph:
    """Serves delta pages keyed by URL; records every fetch (mirrors test_mailbox_poll)."""

    def __init__(self, pages: dict[str, dict]) -> None:
        self.pages = pages
        self.fetched: list[str] = []

    async def get(self, url, *, headers=None):
        self.fetched.append(url)
        page = self.pages.get(url)
        if page is None:
            return httpx.Response(404, json={}, request=httpx.Request("GET", url))
        return httpx.Response(200, json=page, request=httpx.Request("GET", url))

    async def post(self, url, *, data=None, headers=None):  # pragma: no cover
        raise AssertionError("no token refresh expected in this test")


async def _seed_outlook_mailbox(
    admin: uuid.UUID, iid: uuid.UUID, *, status: str, status_stashed: str | None, now: datetime
) -> None:
    ref = secret_ref_for(admin, iid, "email_sender")
    await LocalSecretStore().put(ref, {"access_token": "ms-tok", "refresh_token": "ms-r"})
    async with tenant_session(admin) as s:
        s.add(
            InstanceConnection(
                connection_id=uuid.uuid4(),
                admin_id=admin,
                instance_id=iid,
                connection_type="email_sender",
                auth_class="oauth_token",
                provider="outlook",
                status=status,
                status_stashed=status_stashed,
                secret_ref=ref,
                non_secret_config={
                    "address": MAILBOX,
                    "destination": MAILBOX,
                    "mode": "byo_mailbox",
                    "token_expires_at": (now + timedelta(minutes=50)).isoformat(),
                    "mailbox_delta_link": DELTA_FINAL,
                    "mailbox_polling_since": (now - timedelta(hours=1)).isoformat(),
                },
            )
        )


def _graph_page(*, sender: str, received: datetime) -> dict:
    return {
        "value": [
            {
                "id": "msg-1",
                "subject": "Sunday?",
                "from": {"emailAddress": {"address": sender}},
                "receivedDateTime": received.isoformat().replace("+00:00", "Z"),
                "internetMessageId": "<msg-1@lead.example>",
                "body": {"contentType": "text", "content": "And what time do you open?"},
                "bodyPreview": "And what time do you open?",
                "isDraft": False,
            }
        ],
        "@odata.deltaLink": "https://graph.microsoft.com/v1.0/delta-next",
    }


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_mailbox_poll_keeps_reading_a_quiescing_mailbox_for_the_in_flight_thread(
    monkeypatch,
):
    LocalSecretStore.reset()
    now = datetime.now(UTC)
    admin = uuid.uuid4()
    iid = await _seed(admin)
    await _seed_outlook_mailbox(admin, iid, status="connected", status_stashed=None, now=now)
    # Dana's thread is open when the owner deletes the Luciel.
    async with tenant_session(admin) as s:
        opened = await run_inbound_message(s, _inbound(admin, iid, "email", "dana@lead.example"))
    assert opened.is_new_session
    async with tenant_session(admin) as s:
        await state_machine.delete_luciel(s, admin, actor_user_id=admin)
    async with tenant_session(admin) as s:
        row = (
            await s.execute(select(InstanceConnection).where(InstanceConnection.admin_id == admin))
        ).scalar_one()
        assert row.status == state_machine.QUIESCING

    sent: list[dict] = []
    real_send = outbound.send_outbound_reply

    async def capture(session, **kwargs):
        sent.append(kwargs)
        return await real_send(session, **kwargs)

    monkeypatch.setattr(outbound, "send_outbound_reply", capture)
    graph = _FakeGraph(
        {DELTA_FINAL: _graph_page(sender="dana@lead.example", received=now + timedelta(minutes=30))}
    )
    async with tenant_session(admin) as s:
        processed = await poll_mailbox(
            s, admin_id=admin, now=now + timedelta(minutes=35), transport=graph
        )
    assert graph.fetched == [DELTA_FINAL], "the draining mailbox was read"
    assert processed == 1
    assert sent and sent[0]["to"] == "dana@lead.example", "her follow-up was answered"
    assert await _session_count(admin) == 1, "the drain admitted her thread, opened none"
    LocalSecretStore.reset()


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_mailbox_poll_skips_a_quiescing_mailbox_that_was_not_connected_before():
    LocalSecretStore.reset()
    now = datetime.now(UTC)
    admin = uuid.uuid4()
    iid = await _seed(admin, state=LucielState.luciel_grace_window)
    await _seed_outlook_mailbox(
        admin, iid, status=state_machine.QUIESCING, status_stashed="expired", now=now
    )
    graph = _FakeGraph({DELTA_FINAL: _graph_page(sender="dana@lead.example", received=now)})
    async with tenant_session(admin) as s:
        processed = await poll_mailbox(s, admin_id=admin, now=now, transport=graph)
    assert processed == 0
    assert graph.fetched == []
    LocalSecretStore.reset()


# --------------------------------------------------------------------------- #
# Restore and the day-30 cascade treat quiescing exactly as revoked
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_restore_revives_quiescing_and_legacy_revoked_rows_alike():
    admin = uuid.uuid4()
    iid = await _seed(admin)
    draining = await _add_connection(admin, iid, status="connected")
    legacy = await _add_connection(
        admin,
        iid,
        connection_type="calendar",
        provider="google_calendar",
        status="revoked",
        status_stashed="connected",
        destination=None,
    )
    async with tenant_session(admin) as s:
        await state_machine.delete_luciel(s, admin, actor_user_id=admin)
    rows = await _rows(admin)
    assert rows[draining].status == state_machine.QUIESCING
    assert rows[legacy].status == "revoked", "delete leaves a pre-WP-5 revoked row alone"
    async with tenant_session(admin) as s:
        await state_machine.restore(s, admin, actor_user_id=admin)
    rows = await _rows(admin)
    assert rows[draining].status == "connected"
    assert rows[draining].status_stashed is None
    assert (rows[draining].non_secret_config or {}).get("destination") == NUM
    assert rows[legacy].status == "connected", "a pre-WP-5 revoked row revives the same way"
    assert rows[legacy].status_stashed is None
    async with tenant_session(admin) as s:
        # The revived number is routable AND held again (c21: restore re-arms the claim).
        assert (await routing.resolve_destination(s, "sms_sender", NUM)).admin_id == admin
        assert await routing.owner_of_destination(s, "sms_sender", NUM) == admin


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_restore_retires_a_draining_row_a_live_one_superseded():
    """A many-active type (knowledge_source, 0058) is the one place a draining row and a
    newer live row of the same type can coexist; restore must not leave the draining
    row quiescing on an active Luciel."""
    admin = uuid.uuid4()
    iid = await _seed(admin, state=LucielState.luciel_grace_window)
    superseded = await _add_connection(
        admin,
        iid,
        connection_type="knowledge_source",
        provider="google_drive",
        status=state_machine.QUIESCING,
        status_stashed="connected",
        destination=None,
        created_at=datetime.now(UTC) - timedelta(days=1),
    )
    replacement = await _add_connection(
        admin,
        iid,
        connection_type="knowledge_source",
        provider="google_drive",
        status="connected",
        destination=None,
    )
    async with tenant_session(admin) as s:
        await state_machine.restore(s, admin, actor_user_id=admin)
    rows = await _rows(admin)
    assert rows[replacement].status == "connected"
    assert rows[superseded].status == "revoked", (
        "a draining row must not linger on an active Luciel"
    )
    assert rows[superseded].status_stashed is None


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_cascade_destroys_a_quiescing_rows_secret_like_a_revoked_one(monkeypatch):
    import app.connections.secrets as secrets_module

    store = LocalSecretStore()
    monkeypatch.setattr(secrets_module, "_default", store)
    admin = uuid.uuid4()
    iid = await _seed(admin)
    ref = secret_ref_for(admin, iid, "sms_sender")
    await store.put(ref, {"auth_token": "tok"})
    cid = await _add_connection(admin, iid, secret_ref=ref)
    async with tenant_session(admin) as s:
        await state_machine.delete_luciel(s, admin, actor_user_id=admin)
    assert (await _rows(admin))[cid].status == state_machine.QUIESCING
    assert await store.get(ref) is not None, "grace retains the secret (restore needs it)"
    async with tenant_session(admin) as s:
        await cascade.hard_delete_cascade(s, admin)
    assert await store.get(ref) is None
    assert await _rows(admin) == {}
