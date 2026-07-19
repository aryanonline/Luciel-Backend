"""Email delivery layer + hCaptcha enforcement (§3.1.2/§3.6.7/§5.10/§3.7.1a)."""

from __future__ import annotations

import pytest
from app.auth.captcha import verify_captcha
from app.notifications.email_sender import NullEmailSender, build_email_sender


async def test_null_sender_records_without_network():
    s = NullEmailSender()
    mid = await s.send(to="a@b.com", subject="Hi", body_text="body")
    assert mid == "null-1"
    assert s.sent[0].to == "a@b.com" and s.sent[0].subject == "Hi"


def test_build_email_sender_defaults_to_null_in_mock(monkeypatch):
    # Mock mode -> Null (no SES). Default integration_mode is mock.
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    assert isinstance(build_email_sender(), NullEmailSender)


async def test_captcha_noop_when_unconfigured():
    # Mock mode / no secret -> verification is a no-op pass (does not raise).
    await verify_captcha(None)
    await verify_captcha("anything")


async def test_captcha_enforced_when_live_and_configured(monkeypatch):
    monkeypatch.setenv("LUCIEL_INTEGRATION_MODE", "live")
    monkeypatch.setenv("LUCIEL_HCAPTCHA_SECRET_KEY", "0xtest")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]

    # Missing token -> rejected (fail-closed) without any network call.
    with pytest.raises(Exception):
        await verify_captcha(None)

    get_settings.cache_clear()  # type: ignore[attr-defined]
