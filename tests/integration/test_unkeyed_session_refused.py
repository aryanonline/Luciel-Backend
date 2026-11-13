"""A conversation with no key is refused, never opened (round 7 WP-3a).

``resolve_or_open_session`` used to skip every lookup when it was handed no client
session id, no resolved lead AND no participant key, and open a brand-new ``sessions``
row for each such message — billed at the first model call. Reachable live from the
Meta messaging adapter: a messaging event with no ``sender.id`` yields
``customer_identifier ""``, which ``participant_key_for`` maps to no key. The session
opened that way could never be resumed (nothing to match it on) nor answered (the
reply goes ``to=customer_identifier``): an unkeyable, unanswerable conversation per
message, each one a billed unit.

The refusal is typed (``SessionUnkeyable``, an ``InboundRejected``) so the channel edge
records it with the same vocabulary as an unresolvable destination and drops the
message honestly. The widget — whose first message legitimately carries no key and
receives a server-minted session id — is pinned as the one exception.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from app.channels import inbound_dispatch, rate_limit
from app.channels.base import InboundMessage, InboundRejected, InstanceContext
from app.channels.runtime_entry import run_inbound_message
from app.db.session import tenant_session
from app.models.budget import CountedSession
from app.models.conversation import Session as ConvSession
from app.models.instance import Instance, LucielState
from app.runtime.sessions import SessionUnkeyable, resolve_or_open_session
from sqlalchemy import func, select

TRANSPORT_KEYED_CHANNELS = ("sms", "email", "whatsapp", "messenger", "instagram", "voice")


async def _seed(admin: uuid.UUID, iid: uuid.UUID) -> None:
    async with tenant_session(admin) as s:
        s.add(
            Instance(
                id=iid,
                admin_id=admin,
                name="Acme Patios",
                state=LucielState.active,
                channels=[
                    {"id": c, "enabled": True} for c in ("widget", *TRANSPORT_KEYED_CHANNELS)
                ],
                escalation={},
            )
        )
        await s.flush()


async def _count(admin: uuid.UUID, model: Any) -> int:
    async with tenant_session(admin) as s:
        return (
            await s.scalar(select(func.count()).select_from(model).where(model.admin_id == admin))
        ) or 0


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
@pytest.mark.parametrize("channel", TRANSPORT_KEYED_CHANNELS)
async def test_a_transport_keyed_channel_with_no_key_opens_nothing(channel: str):
    admin, iid = uuid.uuid4(), uuid.uuid4()
    await _seed(admin, iid)
    async with tenant_session(admin) as s:
        with pytest.raises(SessionUnkeyable) as e:
            await resolve_or_open_session(
                s,
                admin_id=admin,
                instance_id=iid,
                channel=channel,
                resolved_lead_id=None,
                session_id=None,
                participant_key=None,
            )
    assert isinstance(e.value, InboundRejected)  # the channel edge's vocabulary
    assert e.value.reason == f"unkeyable_session:{channel}"
    assert e.value.channel == channel
    assert await _count(admin, ConvSession) == 0


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_widget_still_opens_its_first_session_with_no_key():
    """The widget's first message carries no key by design (§3.4.9): the server mints
    the session id and the client carries it from then on. The guard must never
    creep onto this path, or no website chat could ever start."""
    admin, iid = uuid.uuid4(), uuid.uuid4()
    await _seed(admin, iid)
    async with tenant_session(admin) as s:
        sess, is_new = await resolve_or_open_session(
            s, admin_id=admin, instance_id=iid, channel="widget", resolved_lead_id=None
        )
        assert is_new
        # ...and the minted id is the key the next turn resumes by.
        again, is_new_again = await resolve_or_open_session(
            s,
            admin_id=admin,
            instance_id=iid,
            channel="widget",
            resolved_lead_id=None,
            session_id=sess.id,
        )
    assert again.id == sess.id and not is_new_again
    assert await _count(admin, ConvSession) == 1


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_runtime_entry_refuses_a_blank_identifier_before_any_session():
    """The live shape: an admitted InboundMessage whose customer_identifier is ""
    (what the Meta adapter produced for a sender-less event) reaches the runtime
    entry every webhook uses. No session row, nothing counted."""
    admin, iid = uuid.uuid4(), uuid.uuid4()
    await _seed(admin, iid)
    msg = InboundMessage(
        admin_id=admin,
        instance_id=iid,
        channel="messenger",
        session_id=None,
        customer_identifier="",
        body="hello?",
        channel_metadata={"destination_id": "PAGE_1"},
        received_at=datetime.now(UTC),
    )
    async with tenant_session(admin) as s:
        with pytest.raises(SessionUnkeyable):
            await run_inbound_message(s, msg)
    assert await _count(admin, ConvSession) == 0
    assert await _count(admin, CountedSession) == 0


class _SenderlessAdapter:
    """An adapter that admits a message with no transport identifier — the shape the
    Meta messaging adapter produced before it learned to refuse a sender-less event.
    Kept as the sessions-layer backstop for any adapter."""

    channel_id = "messenger"

    def __init__(self, admin: uuid.UUID, iid: uuid.UUID) -> None:
        self._ctx = InstanceContext(admin_id=admin, instance_id=iid)
        self.sent: list[Any] = []

    async def verify_inbound(self, request: dict[str, Any]) -> InstanceContext:
        return self._ctx

    async def receive(self, request: dict[str, Any], ctx: InstanceContext) -> InboundMessage:
        return InboundMessage(
            admin_id=ctx.admin_id,
            instance_id=ctx.instance_id,
            channel=self.channel_id,
            session_id=None,
            customer_identifier="",
            body=str(request["body"]),
            channel_metadata={"destination_id": "PAGE_1"},
            received_at=datetime.now(UTC),
        )

    async def send(self, message: Any) -> Any:
        self.sent.append(message)
        raise AssertionError("nothing to send to — there is no recipient")


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_channel_edge_records_the_rejection_and_drops_the_message(caplog):
    """End to end through the edge every webhook and the mailbox poll share: the
    refusal is recorded as an inbound rejection (same log line as an unresolvable
    destination — never a 5xx the provider would retry forever), no session row is
    opened, nothing is counted, and no send is attempted."""
    admin, iid = uuid.uuid4(), uuid.uuid4()
    await _seed(admin, iid)
    rate_limit._store = rate_limit.TokenBucketStore()
    adapter = _SenderlessAdapter(admin, iid)

    with caplog.at_level(logging.WARNING, logger="app.channels.pipeline"):
        reply = await inbound_dispatch.dispatch_inbound_reply(
            adapter, {"body": "hello?", "source_ip": "203.0.113.9"}
        )

    assert reply is None
    assert adapter.sent == []
    assert await _count(admin, ConvSession) == 0
    assert await _count(admin, CountedSession) == 0
    lines = [r.getMessage() for r in caplog.records if "inbound_rejected" in r.getMessage()]
    assert lines == [
        "inbound_rejected channel=messenger reason=unkeyable_session:messenger source_hint=None"
    ]
