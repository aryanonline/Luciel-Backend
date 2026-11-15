"""Twilio's delivery receipt for an owner-notification text stamps the escalation row
(round 7 WP-4b): ``delivered`` means the carrier delivered it, ``failed`` /
``undelivered`` pages the owner by email now and the row says ``owner_fallback`` (or
``failed`` when the owner cannot be reached either), an interim status changes nothing,
and a receipt for a message this platform never sent is a 200 no-op. Same posture as the
voice status route: signature or capability-URL verified, per-number flood control, a
forged ``cap`` 403s and is audited under the tenant it names.

Round 7 delivery hardening: the flood bucket is charged only AFTER verification, so
unsigned posts cannot blind a tenant's real receipts; a verified receipt that arrives
before the sending transaction commits is looked up again rather than dropped.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from app.channels import rate_limit
from app.channels.tenant_twilio import FIELD_CAPABILITY_TOKEN
from app.connections.secrets import get_secret_store, secret_ref_for
from app.db.session import tenant_session
from app.models.audit import AdminAuditLog
from app.models.connection import InstanceConnection
from app.models.conversation import EscalationEvent
from app.models.instance import Account, AccountState, Instance, LucielState
from app.notifications import email_notifier
from sqlalchemy import select

pytestmark = [pytest.mark.contract]

ROUTE = "/api/v1/webhooks/twilio/notifications/status"
OWNER_EMAIL = "owner@acme.example"
CONTACT = "+16045551234"


class FakeEmailSender:
    def __init__(self) -> None:
        self.sent: list[dict[str, str | None]] = []

    async def send(self, *, to, subject, body_text, from_address=None, tags=None):
        self.sent.append({"to": to, "subject": subject, "body": body_text})
        return f"ses-{len(self.sent)}"


@pytest.fixture
def ses(monkeypatch) -> FakeEmailSender:
    fake = FakeEmailSender()
    monkeypatch.setattr(email_notifier, "build_email_sender", lambda: fake)
    return fake


async def _seed(
    number: str, *, sid: str, detail: str = "sms:primary"
) -> tuple[uuid.UUID, uuid.UUID]:
    """A capability-verified tenant (C11) with one page out on ``sid``, row ``sent``."""
    admin, iid, session_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    ref = secret_ref_for(admin, iid, "sms_sender")
    await get_secret_store().put(
        ref, {"access_token": "bearer-tok", FIELD_CAPABILITY_TOKEN: "status-cap"}
    )
    async with tenant_session(admin) as s:
        s.add(
            Account(
                id=admin,
                admin_id=admin,
                user_id=admin,
                email=OWNER_EMAIL,
                email_lower=OWNER_EMAIL,
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
                state=LucielState.active,
                embed_key_public_id=f"vm_live_{admin.hex[:8]}",
                channels=[],
                tools=[],
                escalation={"primarySms": CONTACT, "preferredChannel": "sms"},
                personality={},
            )
        )
        s.add(
            InstanceConnection(
                connection_id=uuid.uuid4(),
                admin_id=admin,
                instance_id=iid,
                connection_type="sms_sender",
                provider="twilio",
                auth_class="oauth_token",
                status="connected",
                secret_ref=ref,
                non_secret_config={"accountSid": "AC" + "5" * 32, "destination": number},
            )
        )
        s.add(
            EscalationEvent(
                admin_id=admin,
                instance_id=iid,
                session_id=session_id,
                signal="high_value_lead",
                gate="outcome",
                score_or_confidence=0.9,
                reasons=["named a budget"],
                fired_at=datetime.now(UTC),
                delivery_status="sent",
                delivery_detail=detail,
                delivery_provider_message_id=sid,
            )
        )
        await s.flush()
    return admin, session_id


async def _row(admin: uuid.UUID, session_id: uuid.UUID) -> EscalationEvent:
    async with tenant_session(admin) as s:
        return (
            await s.execute(
                select(EscalationEvent).where(
                    EscalationEvent.admin_id == admin, EscalationEvent.session_id == session_id
                )
            )
        ).scalar_one()


async def _audit(admin: uuid.UUID, event_type: str) -> list[AdminAuditLog]:
    async with tenant_session(admin) as s:
        return list(
            (
                await s.execute(
                    select(AdminAuditLog).where(
                        AdminAuditLog.admin_id == admin, AdminAuditLog.event_type == event_type
                    )
                )
            )
            .scalars()
            .all()
        )


def _receipt(number: str, *, sid: str, status: str, error_code: str | None = None) -> dict:
    form = {"From": number, "To": CONTACT, "MessageSid": sid, "MessageStatus": status}
    if error_code:
        form["ErrorCode"] = error_code
    return form


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_carriers_delivered_stamps_the_row_delivered(client, ses):
    number, sid = "+16045550181", "SM" + "a" * 32
    admin, session_id = await _seed(number, sid=sid)

    interim = await client.post(
        f"{ROUTE}?cap=status-cap", data=_receipt(number, sid=sid, status="sent")
    )
    assert interim.status_code == 200
    row = await _row(admin, session_id)
    assert row.delivery_status == "sent" and row.delivered_at is None  # nothing yet

    ok = await client.post(
        f"{ROUTE}?cap=status-cap", data=_receipt(number, sid=sid, status="delivered")
    )
    assert ok.status_code == 200
    row = await _row(admin, session_id)
    assert row.delivery_status == "delivered"
    assert row.delivered_at is not None
    assert ses.sent == []


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_failed_receipt_pages_the_owner_by_email_and_says_why(client, ses):
    number, sid = "+16045550182", "SM" + "b" * 32
    admin, session_id = await _seed(number, sid=sid)

    r = await client.post(
        f"{ROUTE}?cap=status-cap",
        data=_receipt(number, sid=sid, status="undelivered", error_code="30003"),
    )
    assert r.status_code == 200
    row = await _row(admin, session_id)
    assert row.delivery_status == "owner_fallback"
    assert row.delivery_detail == "sms_dlr:undelivered:30003"
    assert [m["to"] for m in ses.sent] == [OWNER_EMAIL]
    assert "named a budget" in str(ses.sent[0]["body"])
    failed = await _audit(admin, "escalation_delivery_failed")
    assert len(failed) == 1
    assert failed[0].event_payload["failure_reason"] == "sms_dlr:undelivered:30003"
    assert failed[0].event_payload["provider_message_id"] == sid
    fallback = await _audit(admin, "escalation_owner_fallback")
    assert len(fallback) == 1
    assert fallback[0].event_payload["reason"] == "sms_delivery_failed"

    # Twilio retries receipts: the second one for a settled row pages nobody again.
    again = await client.post(
        f"{ROUTE}?cap=status-cap",
        data=_receipt(number, sid=sid, status="undelivered", error_code="30003"),
    )
    assert again.status_code == 200
    assert len(ses.sent) == 1


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_failed_receipt_for_a_page_the_owner_was_already_copied_on_does_not_page_twice(
    client, ses
):
    """An unconfirmed number had the owner copied when the page went out; the carrier
    later failing it must not email the owner a second time — the row just says so."""
    number, sid = "+16045550183", "SM" + "c" * 32
    admin, session_id = await _seed(number, sid=sid, detail="sms:primary+owner")

    r = await client.post(
        f"{ROUTE}?cap=status-cap", data=_receipt(number, sid=sid, status="failed")
    )
    assert r.status_code == 200
    row = await _row(admin, session_id)
    assert row.delivery_status == "owner_fallback"
    assert row.delivery_detail == "sms_dlr:failed"
    assert ses.sent == []
    assert len(await _audit(admin, "escalation_delivery_failed")) == 1
    assert await _audit(admin, "escalation_owner_fallback") == []


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_receipt_for_a_message_this_platform_never_sent_is_a_no_op(
    client, ses, monkeypatch
):
    from app.api.v1.webhooks import channels as webhooks

    monkeypatch.setattr(webhooks, "RECEIPT_LOOKUP_RETRY_DELAYS", (0.01, 0.01))
    number, sid = "+16045550184", "SM" + "d" * 32
    admin, session_id = await _seed(number, sid=sid)

    r = await client.post(
        f"{ROUTE}?cap=status-cap", data=_receipt(number, sid="SM" + "0" * 32, status="delivered")
    )
    assert r.status_code == 200  # looked up again, then let go — never an error to Twilio
    assert (await _row(admin, session_id)).delivery_status == "sent"
    assert ses.sent == []


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_unsigned_posts_cannot_blind_a_tenants_real_receipts(client, ses, monkeypatch):
    """Round 7 delivery hardening. The flood bucket was charged on the unauthenticated
    From field BEFORE verification: anyone who knew a business's number could post
    enough unsigned receipts to exhaust it (burst 10) and auto-block the key for a day
    (50 rejections), after which every real receipt for that tenant was a 429 and a
    carrier failure never reached the owner. A forgery costs the forger a 403 now and
    nothing of the tenant's; the bucket still bounds VERIFIED traffic."""
    # The bucket's clock stands still here. It refills a token every 2 s, and on a loaded
    # runner each post can take longer than that, so the burst below never ran out (seen
    # under CPU load: not one 429 among the fifteen verified posts).
    store = rate_limit.get_store()
    frozen = store._now()
    monkeypatch.setattr(store, "_now", lambda: frozen)
    number, sid = "+16045550187", "SM" + "1" * 32
    admin, session_id = await _seed(number, sid=sid)

    for _ in range(rate_limit.AUTO_BLOCK_ABUSE_THRESHOLD + 5):
        forged = await client.post(
            f"{ROUTE}?cap=wrong", data=_receipt(number, sid=sid, status="delivered")
        )
        assert forged.status_code == 403
    assert (await _row(admin, session_id)).delivery_status == "sent"

    ok = await client.post(
        f"{ROUTE}?cap=status-cap", data=_receipt(number, sid=sid, status="delivered")
    )
    assert ok.status_code == 200
    assert (await _row(admin, session_id)).delivery_status == "delivered"

    # The flood control is still there for traffic that verifies.
    statuses = [
        (
            await client.post(
                f"{ROUTE}?cap=status-cap", data=_receipt(number, sid=sid, status="sent")
            )
        ).status_code
        for _ in range(rate_limit.LIMITS["voice_status"].burst + 5)
    ]
    assert 429 in statuses and statuses[0] == 200


#: Bound on every wait of a ``_RetryGate``: far longer than the test holds one, so a gate
#: that is never reached or never released fails the test instead of hanging the run.
_GATE_SECONDS = 10


class _RouteAsyncio:
    """What the route's module reads as ``asyncio`` while a ``_RetryGate`` holds it: the
    real module with only ``sleep`` swapped. ``asyncio.sleep`` itself is untouched, so the
    client, the database driver and the test keep their real timers."""

    def __init__(self, sleep: Callable[[float], Awaitable[None]]) -> None:
        self.sleep = sleep

    def __getattr__(self, name: str) -> Any:
        return getattr(asyncio, name)


class _RetryGate:
    """Holds the receipt route between its lookups until the test releases it.

    The route waits ``RECEIPT_LOOKUP_RETRY_DELAYS`` between lookups. A fixed sleep in the
    test only bet that the sender's commit would land inside one of those waits: on a slow
    runner it could land after the last lookup (the receipt answered ``unknown_message``
    and the test failed) or before the first (the test passed without the route ever
    looking again). Installed by ``_hold_receipt_retries``, the gate records the outcome
    of each owner-page lookup, says when one has missed, and ends every wait only after
    ``release``."""

    def __init__(self) -> None:
        self.lookups: list[str] = []
        self.waits = 0
        self._missed = asyncio.Event()
        self._released = asyncio.Event()

    def watch(self, lookup: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
        async def watched(*args: Any, **kwargs: Any) -> str:
            outcome = await lookup(*args, **kwargs)
            self.lookups.append(outcome)
            if outcome == "unknown_message":
                self._missed.set()
            return outcome

        return watched

    async def sleep(self, delay: float) -> None:
        """The route's wait before it looks again: it ends on ``release``, not on a timer."""
        self.waits += 1
        await asyncio.wait_for(self._released.wait(), timeout=_GATE_SECONDS)

    async def missed(self) -> None:
        """Return once a lookup has found no row carrying the SID."""
        await asyncio.wait_for(self._missed.wait(), timeout=_GATE_SECONDS)

    def release(self) -> None:
        """Let the route look again, now and after any later miss."""
        self._released.set()


def _hold_receipt_retries(monkeypatch: pytest.MonkeyPatch) -> _RetryGate:
    """Put the route's owner-page lookup and its waits between lookups on a ``_RetryGate``.
    Only the route's module sees the gated ``asyncio``; ``asyncio.sleep`` is never patched."""
    from app.api.v1.webhooks import channels as webhooks
    from app.notifications import escalation_dispatch

    gate = _RetryGate()
    monkeypatch.setattr(
        escalation_dispatch,
        "record_sms_delivery_receipt",
        gate.watch(escalation_dispatch.record_sms_delivery_receipt),
    )
    monkeypatch.setattr(webhooks, "asyncio", _RouteAsyncio(gate.sleep))
    return gate


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_receipt_that_arrives_before_the_senders_commit_is_not_dropped(
    client, ses, monkeypatch
):
    """Round 7 delivery hardening. Twilio posts the receipt within seconds of the
    accept, and the deferred sweep sends all of a tenant's queued pages inside one
    transaction: the receipt could land while the SID was not yet visible on any row,
    be answered 200 ``unknown_message`` and be gone (Twilio does not retry). The route
    now looks the SID up again, in fresh transactions, until the sender's commit is
    seen — and the row is stamped from the carrier's word, not left at ``sent``.

    The commit lands between two lookups by construction, not by a fixed sleep: it is
    made once the first lookup has missed, and the route's wait before the next lookup
    ends only after it."""
    gate = _hold_receipt_retries(monkeypatch)
    number, sid = "+16045550188", "SM" + "2" * 32
    admin, session_id = await _seed(number, sid="SM" + "8" * 32)
    async with tenant_session(admin) as s:
        row = (
            await s.execute(select(EscalationEvent).where(EscalationEvent.admin_id == admin))
        ).scalar_one()
        row.delivery_status = "pending"  # the send is in flight, its transaction open
        row.delivery_detail = None
        row.delivery_provider_message_id = None

    receipt = asyncio.create_task(
        client.post(f"{ROUTE}?cap=status-cap", data=_receipt(number, sid=sid, status="delivered"))
    )
    try:
        await gate.missed()  # the first lookup found nothing ...
        assert not receipt.done()  # ... and the route has not answered: it will look again
        async with tenant_session(admin) as s:  # the sender's commit
            row = (
                await s.execute(select(EscalationEvent).where(EscalationEvent.admin_id == admin))
            ).scalar_one()
            row.delivery_status = "sent"
            row.delivery_detail = "sms:primary"
            row.delivery_provider_message_id = sid
        gate.release()  # only now may the route look again
        response = await asyncio.wait_for(receipt, timeout=_GATE_SECONDS)
    finally:
        receipt.cancel()  # a no-op once it has answered; a failed step leaves no route held
        await asyncio.gather(receipt, return_exceptions=True)

    assert response.status_code == 200
    assert gate.lookups == ["unknown_message", "delivered"]  # missed, then saw the commit
    assert gate.waits == 1  # and in between it waited on the gate, never on a timer
    row = await _row(admin, session_id)
    assert row.delivery_status == "delivered"
    assert row.delivered_at is not None
    assert ses.sent == []


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_forged_cap_403s_and_is_audited_under_the_tenant(client, ses):
    number, sid = "+16045550185", "SM" + "e" * 32
    admin, session_id = await _seed(number, sid=sid)

    forged = await client.post(
        f"{ROUTE}?cap=wrong", data=_receipt(number, sid=sid, status="delivered")
    )
    assert forged.status_code == 403
    assert len(await _audit(admin, "inbound_capability_rejected")) == 1
    assert (await _row(admin, session_id)).delivery_status == "sent"


class _Resp:
    def __init__(self, status_code: int, body: dict[str, object]) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> dict[str, object]:
        return self._body


class _FakeTwilioRest:
    """Twilio's REST API as the bearer surface sees it: records each Messages POST."""

    def __init__(self, sid: str) -> None:
        self.sid = sid
        self.posts: list[dict[str, str]] = []

    async def post(self, url: str, *, headers: dict[str, str], data: dict[str, str]) -> _Resp:
        self.posts.append(dict(data))
        return _Resp(201, {"sid": self.sid, "status": "queued"})

    async def get(self, url: str, *, headers: dict[str, str], params: dict[str, str]) -> _Resp:
        raise AssertionError("no GET expected")


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_oauth_tenants_page_reads_sent_until_the_receipt_it_asked_for_arrives(
    client, ses, monkeypatch
):
    """Round 7 delivery hardening. A tenant connected with the one-click OAuth grant
    sends through the bearer sender, which requested no delivery receipt at all: the
    page read ``delivered`` the moment Twilio's API accepted it, and a carrier failure
    never reached anyone. Now the bearer send carries the same signed callback URL
    (the tenant's capability token — Twilio signs with an account auth token an OAuth
    tenant never gave us), the row reads ``sent``, and only the receipt Twilio posts
    to that URL makes it ``delivered``."""
    from urllib.parse import urlsplit

    from app.channels.twilio_bearer import TwilioBearerSmsSender
    from app.config import get_settings
    from app.notifications import escalation_dispatch, sms_notifier

    number, sid = "+16045550186", "SM" + "f" * 32
    admin, session_id = await _seed(number, sid="SM" + "9" * 32)
    async with tenant_session(admin) as s:
        row = (
            await s.execute(select(EscalationEvent).where(EscalationEvent.admin_id == admin))
        ).scalar_one()
        row.delivery_status = "pending"
        row.delivery_detail = None
        row.delivery_provider_message_id = None
        iid = row.instance_id

    rest = _FakeTwilioRest(sid)

    async def _oauth_sender(*a, **kw):
        return TwilioBearerSmsSender(
            account_sid="AC" + "5" * 32,
            access_token="bearer-tok",  # noqa: S106
            transport=rest,
        )

    monkeypatch.setattr(sms_notifier, "build_tenant_sms_sender", _oauth_sender)
    monkeypatch.setattr(get_settings(), "public_api_base_url", "https://api.example")

    async with tenant_session(admin) as s:
        assert await escalation_dispatch.notify_escalation(
            s,
            admin_id=admin,
            instance_id=iid,
            session_id=session_id,
            resolved_lead_id=None,
            signal="high_value_lead",
            gate="outcome",
            score=0.9,
        )

    (post,) = rest.posts
    assert post["From"] == number and post["To"] == CONTACT
    callback = post["StatusCallback"]
    assert callback == f"https://api.example{ROUTE}?cap=status-cap"
    row = await _row(admin, session_id)
    assert row.delivery_status == "sent"  # accepted by the API — nobody has heard yet
    assert row.delivered_at is None
    assert row.delivery_provider_message_id == sid

    # Twilio posts the receipt to the URL the send asked for.
    target = urlsplit(callback)
    ok = await client.post(
        f"{target.path}?{target.query}", data=_receipt(number, sid=sid, status="delivered")
    )
    assert ok.status_code == 200
    row = await _row(admin, session_id)
    assert row.delivery_status == "delivered"
    assert row.delivered_at is not None
