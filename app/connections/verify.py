"""Post-exchange verification: prove the credential works before anything cuts over.

§3.1.6a orders the BYO-mailbox lifecycle "connect, VERIFY, bind, then live" — a token
exchange alone proves the code was redeemable, not that the mailbox is usable or WHOSE
it is. The verifier is the step between "token stored" and "status connected" in
``complete_oauth_connect``: it asks the provider who authorized (Graph ``/me``) and
returns the non-secret config the bind step writes (address, destination, expiry).

Failure raises :class:`PostExchangeVerifyFailed` and the caller keeps the previous
sender live (a swap resolves through ``confirm_reconnect(probe_ok=False)``), which is
what "never silently replaces a working sender" means mechanically.

Keyed by provider so the callback stays provider-agnostic; providers without an entry
skip verification and keep today's behavior exactly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

GRAPH_ME_ENDPOINT = "https://graph.microsoft.com/v1.0/me"
TWILIO_ACCOUNTS_ENDPOINT = "https://api.twilio.com/2010-04-01/Accounts.json"


class PostExchangeVerifyFailed(Exception):
    """The exchanged token did not verify. Carries a non-secret, owner-facing reason."""


class _GetTransport(Protocol):
    async def get(self, url: str, *, headers: dict[str, str] | None = None) -> Any: ...


async def verify_outlook_mailbox(
    *, material: dict[str, str], transport: _GetTransport
) -> dict[str, object]:
    """Resolve WHICH mailbox authorized, via Graph ``/me`` with the just-stored token.

    Returns the non-secret config for the bind step:
      * ``address`` — the mailbox (``mail`` falling back to ``userPrincipalName``),
        which becomes the FROM identity ``resolve_email_address`` serves;
      * ``destination`` — the same address, the inbound-routing key
        ``channel_resolve_destination`` matches on;
      * ``mode`` — ``byo_mailbox``, so provisioning views can tell this apart from
        the platform-SES modes;
      * ``token_expires_at`` — ISO-8601, from the exchange's ``expires_in``; the
        health worker's and refresh path's cue (§3.8.5). Microsoft access tokens
        live ~1 hour, so a missing hint would read as "never expires" and the first
        refresh would only happen after the first failed send.
    """
    token = material.get("access_token", "")
    if not token:
        raise PostExchangeVerifyFailed("exchange returned no access token")
    try:
        response = await transport.get(
            GRAPH_ME_ENDPOINT, headers={"Authorization": f"Bearer {token}"}
        )
    except Exception as exc:  # noqa: BLE001 — transport errors all mean "not verified"
        raise PostExchangeVerifyFailed(
            f"could not reach Microsoft Graph to verify the mailbox ({type(exc).__name__})"
        ) from exc
    if getattr(response, "status_code", None) != 200:
        raise PostExchangeVerifyFailed(
            "Microsoft Graph rejected the new token "
            f"(HTTP {getattr(response, 'status_code', '?')}) — the mailbox was not connected"
        )
    profile = response.json()
    address = str(profile.get("mail") or profile.get("userPrincipalName") or "").strip().lower()
    if "@" not in address:
        raise PostExchangeVerifyFailed(
            "Microsoft did not report a mailbox address for this account"
        )

    updates: dict[str, object] = {
        "address": address,
        "destination": address,
        "mode": "byo_mailbox",
    }
    raw_expiry = str(material.get("expires_in", "")).strip()
    if raw_expiry.isdigit():
        expires_at = datetime.now(UTC) + timedelta(seconds=int(raw_expiry))
        updates["token_expires_at"] = expires_at.isoformat()
    return updates


async def verify_twilio_account(
    *, material: dict[str, str], transport: _GetTransport
) -> dict[str, object]:
    """Resolve WHICH Twilio account authorized, with the just-exchanged bearer token.

    The one-click connect (audit round 3, C10) never sees an Account SID typed in,
    but every downstream surface — number listing, webhook auto-config, the send
    path, the picker UI — keys on it, so the bind step needs it in
    ``non_secret_config`` exactly where the credential-form path writes it
    (``accountSid``, service.submit_credentials). ``GET /Accounts`` with the bearer
    token is the cheapest call that both proves the token works against the REST
    API (not just that the code was redeemable) and names the account.
    """
    token = material.get("access_token", "")
    if not token:
        raise PostExchangeVerifyFailed("exchange returned no access token")
    try:
        response = await transport.get(
            TWILIO_ACCOUNTS_ENDPOINT, headers={"Authorization": f"Bearer {token}"}
        )
    except Exception as exc:  # noqa: BLE001 — transport errors all mean "not verified"
        raise PostExchangeVerifyFailed(
            f"could not reach Twilio to verify the account ({type(exc).__name__})"
        ) from exc
    if getattr(response, "status_code", None) != 200:
        raise PostExchangeVerifyFailed(
            "Twilio rejected the new token "
            f"(HTTP {getattr(response, 'status_code', '?')}) — the account was not connected"
        )
    body = response.json()
    accounts = body.get("accounts") if isinstance(body, dict) else None
    first = accounts[0] if isinstance(accounts, list) and accounts else {}
    sid = str(first.get("sid") or "").strip()
    if not sid.startswith("AC"):
        raise PostExchangeVerifyFailed("Twilio did not report an account for this grant")

    updates: dict[str, object] = {
        "accountSid": sid,
        # Non-secret display name so the dashboard can say WHOSE account is bound.
        "accountName": str(first.get("friendly_name") or "").strip(),
        "mode": "oauth",
    }
    raw_expiry = str(material.get("expires_in", "")).strip()
    if raw_expiry.isdigit():
        expires_at = datetime.now(UTC) + timedelta(seconds=int(raw_expiry))
        updates["token_expires_at"] = expires_at.isoformat()
    return updates


# Provider -> verifier. Absent means "no post-exchange verification", today's behavior.
POST_EXCHANGE_VERIFIERS = {
    "outlook": verify_outlook_mailbox,
    "twilio": verify_twilio_account,
}
