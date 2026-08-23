"""``tenant_twilio.verify_credentials`` — the mock-mode skip is deliberate, disclosed,
and pinned here (product-owner audit round 3, Phase 0).

Outside live mode a credential submit is accepted with NO provider check — a mock
"connected" is a wiring statement, never a verification claim. In live mode the
account fetch is the proof, and any refusal maps to ``InvalidTwilioCredentials``.
No real Twilio network call anywhere in this file.
"""

from __future__ import annotations

import sys
import types

import pytest
from app.channels.tenant_twilio import (
    InvalidTwilioCredentials,
    TwilioCredentials,
    verify_credentials,
)
from app.config import get_settings

from tests.unit.live_settings import LIVE_PLATFORM_SECRET

CREDS = TwilioCredentials(account_sid="AC" + "0" * 32, auth_token="tok")


@pytest.mark.asyncio
async def test_mock_mode_skips_without_touching_the_sdk(monkeypatch):
    """The skip must return BEFORE any Client construction — prove it by making
    construction explosive."""

    class _Boom:
        def __init__(self, *a, **k):  # pragma: no cover - would fail the test if hit
            raise AssertionError("mock-mode verify must never construct a Twilio client")

    monkeypatch.setitem(sys.modules, "twilio.rest", types.SimpleNamespace(Client=_Boom))
    get_settings.cache_clear()
    try:
        await verify_credentials(CREDS)  # mock is the default posture; no exception
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_live_mode_maps_refusal_to_invalid_credentials(monkeypatch):
    """Live mode DOES verify: a provider refusal surfaces as the typed error the
    connect endpoint turns into an honest 422 — never a silent 'connected'."""

    class _Refuses:
        def __init__(self, *a, **k):
            pass

        @property
        def api(self):
            raise RuntimeError("twilio says no")

    monkeypatch.setenv("LUCIEL_INTEGRATION_MODE", "live")
    monkeypatch.setenv("LUCIEL_PLATFORM_SECRET", LIVE_PLATFORM_SECRET)
    monkeypatch.setitem(sys.modules, "twilio.rest", types.SimpleNamespace(Client=_Refuses))
    get_settings.cache_clear()
    try:
        with pytest.raises(InvalidTwilioCredentials):
            await verify_credentials(CREDS)
    finally:
        get_settings.cache_clear()
