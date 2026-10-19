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
async def test_meta_reply_reports_a_reconnect_rather_than_delivery(live):
    """Live with no Meta grant at all: nothing can leave. Round 7 WP-3b item 7 names
    the truth in the wire vocabulary — the owner must (re)connect Meta — instead of a
    ``meta_sender_not_provisioned`` the dashboard collapsed into "try again"."""
    admin, instance = uuid.uuid4(), uuid.uuid4()

    result = await _reply(
        admin,
        instance,
        channel="instagram",
        to="ig-user-1",
        body="Thanks!",
        sender_id="ig-account-77",
    )

    assert result.delivered is False
    assert result.detail == "meta_reconnect_needed"
    assert result.provider_message_id is None


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
def test_degraded_bindings_names_every_null_seam_in_mock_mode(monkeypatch):
    get_settings.cache_clear()  # type: ignore[attr-defined]
    # A platform Twilio account configured: its sender is a seam (Null in mock).
    monkeypatch.setattr(get_settings(), "twilio_account_sid", "AC_unit_test")
    degraded = degraded_bindings()
    seams = {entry.split(":", 1)[0] for entry in degraded}
    assert {"platform_email", "platform_sms", "secret_store"} <= seams
    # meta_sender is NOT a seam: the Graph token is per-tenant (P1-1), so there is no
    # process-wide binding whose degradation would mean anything.
    assert "meta_sender" not in seams
    # seam:ImplClass — a class NAME, never a value.
    assert all(entry.count(":") == 1 and entry.split(":", 1)[1] for entry in degraded)


@pytest.mark.parametrize("mode", ["mock", "live"])
def test_platform_sms_is_a_seam_only_when_a_platform_twilio_account_is_configured(
    monkeypatch, mode
):
    """Round 7 WP-7: the platform Twilio sender is a dev fallback (BYO telephony sends
    through the tenant's own account), so a deployment without a platform account
    omits the seam instead of reporting it degraded forever; any one of the three
    settings means one was intended, and its Null binding is then named."""
    get_settings.cache_clear()  # type: ignore[attr-defined]
    settings = get_settings()
    monkeypatch.setattr(settings, "integration_mode", mode)
    for name in ("twilio_account_sid", "twilio_api_key_sid", "twilio_api_key_secret"):
        monkeypatch.setattr(settings, name, "")
    assert not any(e.startswith("platform_sms:") for e in degraded_bindings())

    # Any ONE of the three means an account was intended: half-configured (or mock)
    # binds Null, and Null is named.
    for name in ("twilio_account_sid", "twilio_api_key_sid", "twilio_api_key_secret"):
        monkeypatch.setattr(settings, name, "set")
        assert "platform_sms:NullSmsSender" in degraded_bindings()
        monkeypatch.setattr(settings, name, "")

    # Fully configured: mock still binds Null (named), live binds the real sender.
    monkeypatch.setattr(settings, "twilio_account_sid", "AC123")
    monkeypatch.setattr(settings, "twilio_api_key_sid", "SK123")
    monkeypatch.setattr(settings, "twilio_api_key_secret", "unit-secret")
    listed = any(e.startswith("platform_sms:") for e in degraded_bindings())
    assert listed is (mode == "mock")


def test_degraded_bindings_names_an_unbound_payg_price_in_a_real_stripe_posture(monkeypatch):
    """Round 7 WP-1 #4: a real Stripe binding with no metered price can save cards and
    bill nothing — named ``stripe_payg_price:unset`` so /healthz shows it. Mock posture
    bills nothing by design, so an empty price is NOT degraded there."""
    get_settings.cache_clear()  # type: ignore[attr-defined]
    settings = get_settings()
    monkeypatch.setattr(settings, "stripe_integration_posture", "mock")
    monkeypatch.setattr(settings, "stripe_payg_price_id", "")
    assert "stripe_payg_price:unset" not in degraded_bindings()

    monkeypatch.setattr(settings, "stripe_integration_posture", "test")
    degraded = degraded_bindings()
    assert "stripe_payg_price:unset" in degraded
    assert "stripe_gateway:FakeStripeGateway" not in degraded

    monkeypatch.setattr(settings, "stripe_payg_price_id", "price_bound")
    assert "stripe_payg_price:unset" not in degraded_bindings()


def test_degraded_bindings_names_an_unbound_portal_configuration_in_a_real_posture(
    monkeypatch,
):
    """Round 7 WP-1 #5: Stripe TEST mode has no default Billing Portal configuration, so
    a "test" posture without one cannot open the card-update portal — named
    ``stripe_portal_configuration:unset``. Mock posture never opens a portal."""
    get_settings.cache_clear()  # type: ignore[attr-defined]
    settings = get_settings()
    monkeypatch.setattr(settings, "stripe_integration_posture", "mock")
    monkeypatch.setattr(settings, "stripe_portal_configuration_id", "")
    assert "stripe_portal_configuration:unset" not in degraded_bindings()

    monkeypatch.setattr(settings, "stripe_integration_posture", "test")
    assert "stripe_portal_configuration:unset" in degraded_bindings()

    monkeypatch.setattr(settings, "stripe_portal_configuration_id", "bpc_bound")
    assert "stripe_portal_configuration:unset" not in degraded_bindings()


def test_degraded_bindings_names_an_unbound_export_store_in_live_mode(live, monkeypatch):
    """Round 7 WP-5 item 3: the export store binds on the CDK-injected bucket variable,
    so a live process without one writes exports into its own memory. Named
    ``export_store:unbound`` so /healthz shows it; a bound bucket clears it."""
    monkeypatch.delenv("LUCIEL_EXPORT_BUCKET", raising=False)
    assert "export_store:unbound" in degraded_bindings()

    monkeypatch.setenv("LUCIEL_EXPORT_BUCKET", "luciel-prod-export-bundles-000")
    assert "export_store:unbound" not in degraded_bindings()


def test_degraded_bindings_does_not_name_the_export_store_in_mock_mode(monkeypatch):
    """Mock/CI keep the in-process fake by design: not a degraded binding there."""
    monkeypatch.delenv("LUCIEL_EXPORT_BUCKET", raising=False)
    monkeypatch.setenv("LUCIEL_INTEGRATION_MODE", "mock")
    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        assert "export_store:unbound" not in degraded_bindings()
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]


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
    # Round 6 WP-G (F044): the local extension meets the pin, so nothing is reported;
    # an older one would appear as pgvector_too_old:<version>.
    assert not any(e.startswith("pgvector_too_old") for e in body["degraded"]), body


def _sessionmaker_pointed_at_a_dead_database():
    """A REAL sessionmaker whose server does not exist, so the real driver failure runs.

    Port 1 refuses the connection immediately rather than black-holing it, which keeps
    the suite fast while still exercising the genuine asyncpg error path — a hand-rolled
    ``raise`` would prove only that the ``except`` clause is spelled correctly.
    """
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    return async_sessionmaker(
        bind=create_async_engine("postgresql+asyncpg://luciel:luciel@127.0.0.1:1/luciel"),
        class_=AsyncSession,
        expire_on_commit=False,
    )


def test_healthz_reports_degraded_when_the_database_is_unreachable(monkeypatch):
    """Dev ran for hours answering ``ok`` with its database stopped.

    ``/healthz`` reported every seam it had BOUND and never asked whether the one
    dependency that cannot be faked actually answers. The endpoint returned 200 in
    milliseconds while every real request hung for thirty seconds on an asyncpg connect
    timeout — the single endpoint whose whole job is honesty was the last place you
    could learn the service was down.
    """
    from app.main import create_app
    from fastapi.testclient import TestClient

    monkeypatch.setattr("app.db.engine.get_sessionmaker", _sessionmaker_pointed_at_a_dead_database)
    get_settings.cache_clear()  # type: ignore[attr-defined]
    with TestClient(create_app()) as client:
        response = client.get("/healthz")

    body = response.json()
    assert body["status"] == "degraded"
    assert any(entry.startswith("database:") for entry in body["degraded"])
    # Liveness stays 200: a 5xx here drains every ALB target at once and lets the ECS
    # circuit breaker roll back a healthy release over a dependency outage that
    # restarting the task cannot fix.
    assert response.status_code == 200


def test_the_database_probe_never_leaks_connection_details(monkeypatch):
    """``/healthz`` is unauthenticated, so the reason must name the error type only.

    A driver's own message carries the host, the port, the user and the database name
    (§3.8.3) — exactly the reconnaissance an anonymous caller should not get for free.
    """
    import asyncio

    from app.runtime.integration_status import database_unreachable_reason

    monkeypatch.setattr("app.db.engine.get_sessionmaker", _sessionmaker_pointed_at_a_dead_database)
    reason = asyncio.run(database_unreachable_reason())

    # A bare Python identifier by construction: no URL, no host, no user, no message.
    assert reason is not None
    assert reason.isidentifier(), reason
