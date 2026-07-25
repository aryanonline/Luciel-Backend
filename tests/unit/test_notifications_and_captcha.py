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


@pytest.mark.asyncio
async def test_captcha_bypass_rejected_when_flag_off(monkeypatch):
    """Fail-safe: the seedtest bypass token must NOT pass when the flag is off, even in
    live+configured mode (this is the prod posture — prod never sets the flag)."""
    monkeypatch.setenv("LUCIEL_INTEGRATION_MODE", "live")
    monkeypatch.setenv("LUCIEL_HCAPTCHA_SECRET_KEY", "0xtest")
    monkeypatch.delenv("LUCIEL_ALLOW_CAPTCHA_BYPASS_TOKEN", raising=False)
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    # Bypass token with flag OFF -> falls through to real siteverify (network), which
    # for a fake token+secret will fail closed. Either a validation error or a network
    # error is acceptable; what matters is it does NOT silently pass.
    with pytest.raises(Exception):
        await verify_captcha("dev-seedtest-token")
    get_settings.cache_clear()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_captcha_bypass_passes_when_flag_on(monkeypatch):
    """Non-prod seam: with the flag ON, the exact bypass token passes with no network."""
    monkeypatch.setenv("LUCIEL_INTEGRATION_MODE", "live")
    monkeypatch.setenv("LUCIEL_HCAPTCHA_SECRET_KEY", "0xtest")
    monkeypatch.setenv("LUCIEL_ALLOW_CAPTCHA_BYPASS_TOKEN", "true")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    # Exact bypass token -> passes (returns None, no exception, no network).
    await verify_captcha("dev-seedtest-token")
    get_settings.cache_clear()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_captcha_wrong_token_still_rejected_when_flag_on(monkeypatch):
    """Flag ON but a token that isn't the bypass constant must not shortcut."""
    monkeypatch.setenv("LUCIEL_INTEGRATION_MODE", "live")
    monkeypatch.setenv("LUCIEL_HCAPTCHA_SECRET_KEY", "0xtest")
    monkeypatch.setenv("LUCIEL_ALLOW_CAPTCHA_BYPASS_TOKEN", "true")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    with pytest.raises(Exception):
        await verify_captcha("not-the-bypass-token")
    get_settings.cache_clear()  # type: ignore[attr-defined]
