"""Signup bot-protection via hCaptcha siteverify (Architecture §3.7.1a).

Closes the gap where the signup captcha_token was accepted without verification.
Behavior mirrors the codebase's integration-mode seam pattern:

- Live mode WITH an hCaptcha secret configured -> the token is verified against
  hCaptcha's siteverify endpoint; a failed/absent token raises 400.
- Mock/CI or live-without-secret -> a no-op pass (so the suite and local dev stay
  hermetic and don't need a real captcha), exactly like the other live seams.

Secrets are read from config (bridged from the platform secret / env); never logged.
"""

from __future__ import annotations

import httpx

from app.api.errors import validation_error
from app.config import get_settings

_HCAPTCHA_VERIFY_URL = "https://api.hcaptcha.com/siteverify"


async def verify_captcha(token: str | None) -> None:
    """Verify a signup captcha token. Raises 400 on failure in live+configured mode.

    No-op when hCaptcha isn't configured (mock/CI/local), so existing tests and
    local signups keep working without a real captcha provider.
    """
    settings = get_settings()
    secret = settings.hcaptcha_secret_key
    if settings.integration_mode != "live" or not secret:
        return  # seam: bot-protection binds only when configured (like SES/Twilio)

    if not token:
        raise validation_error("Captcha verification required.")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                _HCAPTCHA_VERIFY_URL,
                data={"secret": secret, "response": token},
            )
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        # Fail closed: if we can't verify, reject rather than let a bot through.
        raise validation_error("Captcha verification failed.") from exc

    if not data.get("success", False):
        raise validation_error("Captcha verification failed.")
