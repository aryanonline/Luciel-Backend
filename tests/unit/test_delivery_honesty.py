"""A live deployment must never report a send it did not make (§3.3.4, §3.4.3).

The delivery seams all fail OPEN: ``build_email_sender`` / ``build_sms_sender`` /
``build_tenant_meta_sender`` return a Null implementation when their provider is unconfigured,
and the Null implementations used to hand back a plausible provider message id. Every
layer above reads a non-None id as proof of delivery — the channel adapters set
``accepted=True``, the tool catalog answers ``ok / accepted_for_delivery``, and Luciel
then tells the lead out loud that it emailed or texted them. Nothing left the process.

So the Null senders return ``None`` in live mode, and the two callers that build a sender
themselves (``channels/outbound.py`` and the ``tools/registry.py`` messenger backends)
also refuse up front, which is what turns a generic rejection into an actionable
``*_sender_not_provisioned``. Mock/CI keeps the fake ids: the whole suite depends on
them, and in mock mode a fabricated id is not a lie about a real provider.

The companion half is loud mode — a process that fabricates every send otherwise looks
identical from the outside to one that works.
"""

from __future__ import annotations

import logging
import uuid

import pytest
from app.channels import outbound
from app.channels.twilio_sender import NullSmsSender
from app.channels.whatsapp_adapter import NullMetaSender
from app.config import get_settings
from app.db.session import tenant_session
from app.notifications.email_sender import NullEmailSender
from app.runtime.integration_status import (
    degraded_bindings,
    is_degraded_binding,
    log_resolved_bindings,
    null_in_live,
)
from app.tools import catalog, registry
from app.tools.contract import Outcome, ToolContext

from tests.unit.live_settings import LIVE_PLATFORM_SECRET, REQUIRED_IN_LIVE_MODE

LEAD_EMAIL = "dana@lead.example"
LEAD_PHONE = "+15551230000"


@pytest.fixture
def null_email(monkeypatch: pytest.MonkeyPatch) -> NullEmailSender:
    """Force the platform email seam to resolve Null.

    In live mode ``PLATFORM_EMAIL_SENDER`` now fails closed at boot (P1-10), so this is
    the configuration the guard exists to survive rather than one the config can reach:
    a sender identity that is present but whose binding still degrades to Null.
    """
    from app.notifications import email_sender as sender_mod

    null = NullEmailSender()
    monkeypatch.setattr(sender_mod, "build_email_sender", lambda: null)
    monkeypatch.setattr(registry, "build_email_sender", lambda: null)
    return null


@pytest.fixture
def live(monkeypatch: pytest.MonkeyPatch):
    """Live mode via the environment, so every seam that asks ``live_mode()`` sees it.

    The Null senders read the mode through a function-local import of ``live_mode``, so
    patching a module attribute would miss them; the cache is what actually has to move.
    """
    monkeypatch.setenv("LUCIEL_INTEGRATION_MODE", "live")
    monkeypatch.setenv("LUCIEL_PLATFORM_SECRET", LIVE_PLATFORM_SECRET)
    get_settings.cache_clear()  # type: ignore[attr-defined]
    yield
    get_settings.cache_clear()  # type: ignore[attr-defined]


# --- P0-1: the Null senders stop fabricating provider ids in live mode ----------- #
async def test_null_email_sender_returns_no_id_in_live_mode(live):
    sender = NullEmailSender()
    assert await sender.send(to=LEAD_EMAIL, subject="Hi", body_text="body") is None
    # Still RECORDED: the intent is auditable even though nothing was delivered.
    assert sender.sent[0].to == LEAD_EMAIL


async def test_null_sms_sender_returns_no_id_in_live_mode(live):
    sender = NullSmsSender()
    assert await sender.send_sms("+15550000000", LEAD_PHONE, "body") is None
    assert sender.sent[0].to == LEAD_PHONE


async def test_null_meta_sender_returns_no_id_in_live_mode(live):
    sender = NullMetaSender()
    sent = await sender.send_message(
        channel="whatsapp", sender_id="pn-1", recipient_id="wa-1", body="body"
    )
    assert sent is None
    assert sender.sent[0]["to"] == "wa-1"


async def test_null_senders_keep_their_fake_ids_in_mock_mode():
    """Mock/CI is the only place a synthetic id is honest — and the suite runs on it."""
    get_settings.cache_clear()  # type: ignore[attr-defined]
    assert await NullEmailSender().send(to=LEAD_EMAIL, subject="s", body_text="b") == "null-1"
    assert await NullSmsSender().send_sms("+1", LEAD_PHONE, "b") == "SMnull" + "1".rjust(22, "0")
    assert (
        await NullMetaSender().send_message(
            channel="whatsapp", sender_id="p", recipient_id="r", body="b"
        )
        == "null-meta-1"
    )


def test_null_in_live_is_mode_aware(live):
    assert null_in_live(NullEmailSender()) is True
    assert null_in_live(NullSmsSender()) is True
    assert null_in_live(NullMetaSender()) is True


def test_null_in_live_is_false_in_mock_mode():
    get_settings.cache_clear()  # type: ignore[attr-defined]
    assert null_in_live(NullEmailSender()) is False


def test_a_real_sender_is_never_treated_as_degraded(live):
    class RealEnoughSender:
        async def send(self, **kwargs: object) -> str:
            return "provider-1"

    assert null_in_live(RealEnoughSender()) is False
    assert is_degraded_binding(RealEnoughSender()) is False


# --- P0-2: outbound replies report the failure instead of claiming delivery ------ #
async def _reply(admin: uuid.UUID, instance: uuid.UUID, **kwargs: object):
    async with tenant_session(admin) as s:
        return await outbound.send_outbound_reply(s, admin_id=admin, instance_id=instance, **kwargs)


async def _seed(admin: uuid.UUID, instance: uuid.UUID, **kwargs: object) -> None:
    from app.connections.secrets import secret_ref_for
    from app.models.connection import InstanceConnection

    async with tenant_session(admin) as s:
        s.add(
            InstanceConnection(
                connection_id=uuid.uuid4(),
                admin_id=admin,
                instance_id=instance,
                status="connected",
                secret_ref=secret_ref_for(admin, instance, str(kwargs["connection_type"])),
                **kwargs,
            )
        )
        await s.flush()


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_email_reply_reports_an_unprovisioned_sender_rather_than_delivery(live, null_email):
    """The tenant HAS an address, so the reply gets past ``channel_not_provisioned`` and
    reaches the platform sender — which is Null, so nothing can leave."""
    admin, instance = uuid.uuid4(), uuid.uuid4()
    await _seed(
        admin,
        instance,
        connection_type="email_sender",
        auth_class="api_key",
        provider="ses",
        non_secret_config={"address": "hello@tenant.example"},
    )

    result = await _reply(admin, instance, channel="email", to=LEAD_EMAIL, body="We open at 9.")

    assert result.delivered is False
    assert result.detail == "email_sender_not_provisioned"
    assert result.provider_message_id is None
    # Refused before the sender was even asked — no half-send to reconcile.
    assert null_email.sent == []


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_meta_reply_reports_an_unprovisioned_sender_rather_than_delivery(live):
    admin, instance = uuid.uuid4(), uuid.uuid4()

    result = await _reply(
        admin,
        instance,
        channel="instagram_messenger",
        to="ig-user-1",
        body="Thanks!",
        sender_id="ig-account-77",
    )

    assert result.delivered is False
    assert result.detail == "meta_sender_not_provisioned"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_provisioned_sender_still_delivers_in_live_mode(live, monkeypatch):
    """The guard keys on the sender TYPE, not on live mode — a real binding is untouched."""
    from app.notifications import email_sender as sender_mod

    class FakeSes:
        def __init__(self) -> None:
            self.sent: list[dict[str, object]] = []

        async def send(self, *, to, subject, body_text, from_address=None):
            self.sent.append({"to": to, "from": from_address})
            return "ses-real-1"

    fake = FakeSes()
    monkeypatch.setattr(sender_mod, "build_email_sender", lambda: fake)
    admin, instance = uuid.uuid4(), uuid.uuid4()
    await _seed(
        admin,
        instance,
        connection_type="email_sender",
        auth_class="api_key",
        provider="ses",
        non_secret_config={"address": "hello@tenant.example"},
    )

    result = await _reply(admin, instance, channel="email", to=LEAD_EMAIL, body="hi")

    assert result.delivered is True
    assert result.provider_message_id == "ses-real-1"
    assert fake.sent[0]["from"] == "hello@tenant.example"


# --- P0-2: the tool backends answer `failed`, never `ok` ------------------------- #
async def test_send_email_tool_fails_when_the_sender_is_null(live, null_email):
    tool = catalog.SendEmailTool(registry._EmailMessengerBackend())
    ctx = ToolContext(admin_id=uuid.uuid4(), instance_id=uuid.uuid4())

    result = await tool.execute({"to": LEAD_EMAIL, "body": "We open at 9."}, ctx)

    assert result.outcome is Outcome.failed
    assert result.reason == "provider_rejected"
    assert null_email.sent == []


async def test_send_sms_tool_fails_when_the_platform_sender_is_null(live):
    """No session -> the platform Twilio is the only sender available, and it is Null."""
    tool = catalog.SendSmsTool(registry._SmsMessengerBackend())
    ctx = ToolContext(admin_id=uuid.uuid4(), instance_id=uuid.uuid4())

    result = await tool.execute({"to": LEAD_PHONE, "body": "We open at 9."}, ctx)

    assert result.outcome is Outcome.failed
    assert result.reason == "provider_rejected"


async def test_send_email_tool_reports_ok_when_a_real_sender_is_bound(live, monkeypatch):
    class FakeSes:
        async def send(self, *, to, subject, body_text, from_address=None):
            return "ses-9"

    monkeypatch.setattr(registry, "build_email_sender", lambda: FakeSes())
    tool = catalog.SendEmailTool(registry._EmailMessengerBackend())
    ctx = ToolContext(admin_id=uuid.uuid4(), instance_id=uuid.uuid4())

    result = await tool.execute({"to": LEAD_EMAIL, "body": "hi"}, ctx)

    assert result.outcome is Outcome.ok
    assert result.payload["state"] == "accepted_for_delivery"


# --- loud integration mode ------------------------------------------------------- #
def test_degraded_bindings_names_every_null_seam_in_mock_mode():
    get_settings.cache_clear()  # type: ignore[attr-defined]
    degraded = degraded_bindings()
    seams = {entry.split(":", 1)[0] for entry in degraded}
    assert {"platform_email", "platform_sms", "secret_store"} <= seams
    # meta_sender is NOT a seam: the Graph token is per-tenant (P1-1), so there is no
    # process-wide binding whose degradation would mean anything.
    assert "meta_sender" not in seams
    # seam:ImplClass — a class NAME, never a value.
    assert all(entry.count(":") == 1 and entry.split(":", 1)[1] for entry in degraded)


def test_degraded_bindings_never_leaks_a_secret_value(live):
    rendered = " ".join(degraded_bindings())
    for value in REQUIRED_IN_LIVE_MODE.values():
        assert value not in rendered


def test_startup_log_warns_loudly_when_live_mode_is_degraded(live, caplog):
    with caplog.at_level(logging.WARNING, logger="app.runtime.integration_status"):
        log_resolved_bindings()

    record = next(r for r in caplog.records if r.name == "app.runtime.integration_status")
    assert record.levelno == logging.WARNING
    assert "live" in record.getMessage()
    for value in REQUIRED_IN_LIVE_MODE.values():
        assert value not in record.getMessage()


def test_startup_log_is_informational_in_mock_mode(caplog):
    """Mock mode is degraded BY DESIGN; warning on every local boot trains people to
    ignore the one message that matters."""
    get_settings.cache_clear()  # type: ignore[attr-defined]
    with caplog.at_level(logging.INFO, logger="app.runtime.integration_status"):
        log_resolved_bindings()

    record = next(r for r in caplog.records if r.name == "app.runtime.integration_status")
    assert record.levelno == logging.INFO


def test_healthz_reports_the_resolved_mode_and_degraded_seams():
    from app.main import create_app
    from fastapi.testclient import TestClient

    get_settings.cache_clear()  # type: ignore[attr-defined]
    with TestClient(create_app()) as client:
        body = client.get("/healthz").json()

    assert body["status"] == "ok"
    assert body["integrationMode"] == "mock"
    assert "platform_email:NullEmailSender" in body["degraded"]
