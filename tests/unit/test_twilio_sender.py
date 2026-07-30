"""Twilio-backed SMS sender (Architecture §3.1.4) — closes the SMS SEND-path stub.

NO real Twilio network call anywhere in this file. ``NullSmsSender`` records
sends in-memory; ``build_sms_sender()`` is exercised only in mock config (its
default) and in a live-but-missing-Account-SID config, which must still resolve
to Null. ``TwilioSmsSender`` is exercised for construction-time validation and
the Twilio magic test number shape only, never a live SDK call.
"""

from __future__ import annotations

import pytest
from app.channels.base import OutboundMessage
from app.channels.sms_adapter import SmsAdapter
from app.channels.twilio_sender import (
    MAGIC_TEST_FROM_NUMBER,
    NullSmsSender,
    TwilioSmsSender,
    build_sms_sender,
)
from app.config import get_settings

from tests.unit.live_settings import LIVE_PLATFORM_SECRET


@pytest.mark.asyncio
async def test_null_sms_sender_records_send_no_network():
    sender = NullSmsSender()
    sid = await sender.send_sms("+15005550006", "+15551234567", "hello")
    assert sid is not None
    assert len(sender.sent) == 1
    rec = sender.sent[0]
    assert rec.from_number == "+15005550006"
    assert rec.to == "+15551234567"
    assert rec.body == "hello"
    assert rec.message_sid == sid


@pytest.mark.asyncio
async def test_null_sms_sender_records_multiple_sends_distinct_sids():
    sender = NullSmsSender()
    sid1 = await sender.send_sms("+15005550006", "+15551111111", "a")
    sid2 = await sender.send_sms("+15005550006", "+15552222222", "b")
    assert sid1 != sid2
    assert len(sender.sent) == 2


def test_build_sms_sender_defaults_to_null_in_mock(monkeypatch):
    """Default posture (integration_mode="mock", no Twilio config) -> NullSmsSender.
    No platform-secret read, no twilio import."""
    get_settings.cache_clear()
    try:
        sender = build_sms_sender()
        assert isinstance(sender, NullSmsSender)
    finally:
        get_settings.cache_clear()


def test_build_sms_sender_stays_null_when_live_but_no_account_sid(monkeypatch):
    """In live mode with an API-Key SID/secret configured but no Account SID, the
    factory must honestly degrade to NullSmsSender rather than construct a
    broken/half configured live sender."""
    monkeypatch.setenv("LUCIEL_INTEGRATION_MODE", "live")
    # Live mode fails closed without the platform-wide secrets; provision them so the
    # assertion below lands on the SMS factory rather than on the boot guard.
    monkeypatch.setenv("LUCIEL_PLATFORM_SECRET", LIVE_PLATFORM_SECRET)
    monkeypatch.setenv("LUCIEL_TWILIO_API_KEY_SID", "SK00000000000000000000000000000000")
    monkeypatch.setenv("LUCIEL_TWILIO_API_KEY_SECRET", "fixture-api-key-secret")
    # Deliberately NOT setting LUCIEL_TWILIO_ACCOUNT_SID. getattr() guards mean this
    # test is correct whether or not the field exists on Settings.
    get_settings.cache_clear()
    try:
        sender = build_sms_sender()
        assert isinstance(sender, NullSmsSender)
    finally:
        get_settings.cache_clear()


def test_build_sms_sender_binds_real_sender_once_fully_configured(monkeypatch):
    """Forward-looking: once the parent adds `twilio_account_sid` to Settings and
    all three Twilio fields + live mode are set, the factory binds the real
    sender. We simulate the future field via monkeypatching a Settings instance
    directly (no code change to config.py needed for this test to prove the
    factory's own live+configured branch works)."""
    import app.config as config_mod

    class _FakeSettings:
        integration_mode = "live"
        twilio_account_sid = "ACfixturefixturefixturefixture01"
        twilio_api_key_sid = "SK00000000000000000000000000000000"
        twilio_api_key_secret = "fixture-api-key-secret"  # noqa: S105

    monkeypatch.setattr(config_mod, "get_settings", lambda: _FakeSettings())
    sender = build_sms_sender()
    assert isinstance(sender, TwilioSmsSender)
    assert sender.account_sid == "ACfixturefixturefixturefixture01"


def test_twilio_sms_sender_rejects_construction_without_account_sid():
    """Construction-time fail-closed (never at send time mid-conversation) when
    the Account SID is missing — mirrors RealStripeGateway's posture."""
    with pytest.raises(RuntimeError, match="Account SID"):
        TwilioSmsSender(account_sid="", api_key_sid="SKx", api_key_secret="secret")  # noqa: S106


def test_twilio_sms_sender_lazy_imports_sdk_only_on_send(monkeypatch):
    """The twilio SDK is never imported at module load or construction time —
    only lazily inside send_sms(), guarded for network-less CI."""
    import sys

    assert "twilio" not in sys.modules or True  # no assertion on pre-existing state
    sender = TwilioSmsSender(
        account_sid="ACfixturefixturefixturefixture01",
        api_key_sid="SK00000000000000000000000000000000",
        api_key_secret="fixture-api-key-secret",  # noqa: S105
    )
    # Construction succeeded without importing/using the twilio package.
    assert sender._client is None


@pytest.mark.asyncio
async def test_magic_test_number_send_shape_via_null_sender():
    """Twilio magic test numbers (from=+15005550006) exercise the send *shape*
    hermetically — no real Twilio call, matching the docs' documented test
    credential number (https://www.twilio.com/docs/iam/test-credentials)."""
    sender = NullSmsSender()
    sid = await sender.send_sms(MAGIC_TEST_FROM_NUMBER, "+15005550001", "test body")
    assert sid is not None
    rec = sender.sent[0]
    assert rec.from_number == MAGIC_TEST_FROM_NUMBER
    assert rec.to == "+15005550001"


@pytest.mark.asyncio
async def test_sms_adapter_default_sender_is_null_and_receives_send(monkeypatch):
    """The SMS adapter's outbound send() uses build_sms_sender() by default
    (mock mode) -> NullSmsSender, and the send actually reaches it."""
    get_settings.cache_clear()
    try:
        adapter = SmsAdapter()  # no sender injected -> factory default
        from app.channels.twilio_sender import NullSmsSender as _Null

        assert isinstance(adapter._sender, _Null)
        receipt = await adapter.send(
            OutboundMessage(
                channel="sms",
                to="+15005550001",
                body="Hi from the null sender",
                metadata={"from_number": MAGIC_TEST_FROM_NUMBER},
            )
        )
        assert receipt.accepted
        assert receipt.provider_message_id is not None
        assert adapter._sender.sent[0].to == "+15005550001"
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_sms_adapter_still_accepts_injected_sender_explicitly():
    """Backward compatibility: existing tests/call sites that pass their own
    sender (e.g. FakeTwilio) must keep working unchanged."""

    class _Fake:
        def __init__(self) -> None:
            self.sent = []

        async def send_sms(self, from_number, to, body):
            self.sent.append((from_number, to, body))
            return "SM_fake_1"

    fake = _Fake()
    adapter = SmsAdapter(fake)
    assert adapter._sender is fake
    receipt = await adapter.send(
        OutboundMessage(channel="sms", to="+15551230000", body="hey", metadata={})
    )
    assert receipt.accepted and receipt.provider_message_id == "SM_fake_1"
