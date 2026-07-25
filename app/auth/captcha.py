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

import hmac

import httpx

from app.api.errors import validation_error
from app.config import get_settings

_HCAPTCHA_VERIFY_URL = "https://api.hcaptcha.com/siteverify"

# Fixed token the non-production seed/live-test harness (§10) sends so it can create
# synthetic accounts through the REAL signup API path. A real hCaptcha token cannot be
# produced headlessly, so live-configured dev/preprod would otherwise be un-seedable
# (and the prod-release verification gate, which runs the same harness, would fail at
# signup). This bypass is accepted ONLY when `allow_captcha_bypass_token` is explicitly
# enabled in config (default False), so production — which never sets it — can never honor
# it even if the token is sent. Fail-safe: absent/false config = no bypass.
_SEEDTEST_BYPASS_TOKEN = "dev-seedtest-token"  # noqa: S105 # nosec B105 - not a secret; flag-gated


async def verify_captcha(token: str | None) -> None:
    """Verify a signup captcha token. Raises 400 on failure in live+configured mode.

    No-op when hCaptcha isn't configured (mock/CI/local), so existing tests and
    local signups keep working without a real captcha provider.
    """
    settings = get_settings()
    secret = settings.hcaptcha_secret_key
    if settings.integration_mode != "live" or not secret:
        return  # seam: bot-protection binds only when configured (like SES/Twilio)

    # Non-production seed/live-test bypass (see _SEEDTEST_BYPASS_TOKEN). Only honored
    # when explicitly enabled in config; prod never enables it. Constant-time compare so
    # the flag+token check is not a timing oracle.
    if getattr(settings, "allow_captcha_bypass_token", False) and token is not None:
        if hmac.compare_digest(token, _SEEDTEST_BYPASS_TOKEN):
            return

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
