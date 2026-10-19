"""Post-exchange verification: prove the credential works before anything cuts over.

§3.1.6a orders the BYO-mailbox lifecycle "connect, VERIFY, bind, then live" — a token
exchange alone proves the code was redeemable, not that the account is usable or WHOSE
it is. The verifier is the step between "token stored" and "status connected" in
``complete_oauth_connect`` (and the knowledge-sync callback): it asks the provider who
authorized and returns the non-secret config the bind step writes (address,
destination, expiry, and the owner-facing ``connectedAs`` line).

Failure raises :class:`PostExchangeVerifyFailed` and the caller keeps the previous
sender live (a swap resolves through ``confirm_reconnect(probe_ok=False)``), which is
what "never silently replaces a working sender" means mechanically.

Round 6 WP-C (2026-09-07): every OAuth provider now has a verifier. Before, only
Outlook and Twilio proved their grant; a Salesforce or HubSpot connect flipped to
Connected on a redeemable code alone, Salesforce stamped no expiry (so the sweep never
refreshed it and a dead grant read Connected while every push returned 401), and
HubSpot was granted read-only scopes for a push that writes contacts. Each verifier:

  * proves the token against the provider's cheapest identity call;
  * refuses a grant that cannot do the job (HubSpot without the contacts write
    scope, Google without the scope this surface needs);
  * stamps ``token_expires_at`` so the health sweep's refresh branch fires; and
  * writes ``connectedAs`` — the org, portal, workspace, account or mailbox — so the
    dashboard can say WHOSE account this is in the provider's own words.

Keyed by provider so the callback stays provider-agnostic; providers without an entry
skip verification and keep today's behavior exactly.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import quote, urlparse

from app.config import INSTAGRAM_GRAPH_HOST, meta_graph_base

GRAPH_ME_ENDPOINT = "https://graph.microsoft.com/v1.0/me"
TWILIO_ACCOUNTS_ENDPOINT = "https://api.twilio.com/2010-04-01/Accounts.json"
GOOGLE_TOKENINFO_ENDPOINT = "https://oauth2.googleapis.com/tokeninfo"
HUBSPOT_TOKEN_INFO_ENDPOINT = "https://api.hubapi.com/oauth/v1/access-tokens"  # noqa: S105 — an endpoint, not a secret  # nosec B105
NOTION_ME_ENDPOINT = "https://api.notion.com/v1/users/me"
NOTION_VERSION = "2022-06-28"
CALENDLY_ME_ENDPOINT = "https://api.calendly.com/users/me"
CALENDLY_EVENT_TYPES_ENDPOINT = "https://api.calendly.com/event_types"


def meta_me_endpoint() -> str:
    """Facebook Graph ``/me`` at the pinned version (``meta_graph_version``)."""
    return f"{meta_graph_base()}/me"


def instagram_me_endpoint() -> str:
    """Instagram Login Graph ``/me`` at the pinned version."""
    return f"{meta_graph_base(INSTAGRAM_GRAPH_HOST)}/me"


GOOGLE_CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"
GOOGLE_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"

#: Salesforce token responses carry no reliable ``expires_in``; the access token lives
#: for the org's session timeout (2 h by default). Matches ``refresh.REFRESH_SPECS``.
SALESFORCE_TOKEN_LIFETIME_SECONDS = 7200
#: Calendly access tokens live two hours.
CALENDLY_TOKEN_LIFETIME_SECONDS = 7200
#: HubSpot access tokens live thirty minutes.
HUBSPOT_TOKEN_LIFETIME_SECONDS = 1800
#: Meta / Instagram long-lived tokens live ~60 days.
META_LONG_LIVED_LIFETIME_SECONDS = 60 * 24 * 3600
#: The hosts a Salesforce org's ``instance_url`` may point at. The value comes from
#: Salesforce's own token response, but it is stored and used as an API base for every
#: later call, so it is pinned to Salesforce's domains rather than trusted blindly.
SALESFORCE_INSTANCE_SUFFIXES = (".salesforce.com", ".force.com", ".salesforce.mil")


class PostExchangeVerifyFailed(Exception):
    """The exchanged token did not verify. Carries a non-secret, owner-facing reason."""


class _GetTransport(Protocol):
    async def get(self, url: str, *, headers: dict[str, str] | None = None) -> Any: ...


Verifier = Callable[..., Awaitable[dict[str, object]]]


def _token_expiry(material: dict[str, str], *, default_seconds: int | None = None) -> str | None:
    """ISO-8601 expiry from the exchange's ``expires_in``, else ``default_seconds``
    from now, else None (a provider whose tokens do not expire)."""
    raw = str(material.get("expires_in", "")).strip()
    seconds = int(raw) if raw.isdigit() else default_seconds
    if seconds is None:
        return None
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()


async def _get_json(
    transport: _GetTransport,
    url: str,
    *,
    headers: dict[str, str],
    provider_label: str,
    what: str,
) -> dict[str, Any]:
    """One identity call. Every failure is a :class:`PostExchangeVerifyFailed` with an
    owner-facing reason; the provider's body never rides the reason (it can echo the
    token or the account)."""
    try:
        response = await transport.get(url, headers=headers)
    except Exception as exc:  # noqa: BLE001 — transport errors all mean "not verified"
        raise PostExchangeVerifyFailed(
            f"could not reach {provider_label} to verify {what} ({type(exc).__name__})"
        ) from exc
    status = getattr(response, "status_code", None)
    if status != 200:
        raise PostExchangeVerifyFailed(
            f"{provider_label} rejected the new token (HTTP {status or '?'}) — {what} was not "
            "connected"
        )
    try:
        body = response.json()
    except Exception as exc:  # noqa: BLE001
        raise PostExchangeVerifyFailed(f"{provider_label} returned an unreadable reply") from exc
    if not isinstance(body, dict):
        raise PostExchangeVerifyFailed(f"{provider_label} returned an unexpected reply shape")
    return body


def _bearer(material: dict[str, str], provider_label: str) -> tuple[str, dict[str, str]]:
    token = str(material.get("access_token", "")).strip()
    if not token:
        raise PostExchangeVerifyFailed(f"{provider_label} exchange returned no access token")
    return token, {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# Outlook / Twilio (unchanged since round 5B)
# --------------------------------------------------------------------------- #


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
    _token, headers = _bearer(material, "Microsoft")
    profile = await _get_json(
        transport,
        GRAPH_ME_ENDPOINT,
        headers=headers,
        provider_label="Microsoft Graph",
        what="the mailbox",
    )
    address = str(profile.get("mail") or profile.get("userPrincipalName") or "").strip().lower()
    if "@" not in address:
        raise PostExchangeVerifyFailed(
            "Microsoft did not report a mailbox address for this account"
        )

    updates: dict[str, object] = {
        "address": address,
        "destination": address,
        "mode": "byo_mailbox",
        "connectedAs": address,
    }
    expiry = _token_expiry(material)
    if expiry:
        updates["token_expires_at"] = expiry
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
    _token, headers = _bearer(material, "Twilio")
    body = await _get_json(
        transport,
        TWILIO_ACCOUNTS_ENDPOINT,
        headers=headers,
        provider_label="Twilio",
        what="the account",
    )
    accounts = body.get("accounts")
    first = accounts[0] if isinstance(accounts, list) and accounts else {}
    sid = str(first.get("sid") or "").strip()
    if not sid.startswith("AC"):
        raise PostExchangeVerifyFailed("Twilio did not report an account for this grant")

    friendly = str(first.get("friendly_name") or "").strip()
    updates: dict[str, object] = {
        "accountSid": sid,
        # Non-secret display name so the dashboard can say WHOSE account is bound.
        "accountName": friendly,
        "mode": "oauth",
        "connectedAs": f"{friendly} ({sid})" if friendly else sid,
    }
    expiry = _token_expiry(material)
    if expiry:
        updates["token_expires_at"] = expiry
    return updates


# --------------------------------------------------------------------------- #
# Round 6 WP-C: the rest of the catalog
# --------------------------------------------------------------------------- #


async def verify_salesforce_org(
    *, material: dict[str, str], transport: _GetTransport
) -> dict[str, object]:
    """Prove the grant against the org it named, and record WHICH org.

    The exchange returns ``instance_url`` (the org's own API base — every later call
    goes there, ``registry`` ``token_fields``). ``/services/oauth2/userinfo`` with the
    new token proves the token works against THAT org and names it (organization id,
    the authorizing user). A token response with no instance URL, or one outside
    Salesforce's domains, is refused: nothing downstream could use it honestly.

    Salesforce reports no ``expires_in``; the access token lives for the org's session
    timeout, so the expiry is stamped from :data:`SALESFORCE_TOKEN_LIFETIME_SECONDS`
    — without it the health sweep never refreshed a Salesforce grant (round 6 WP-C:
    a dead grant read Connected while every push returned 401).
    """
    _token, headers = _bearer(material, "Salesforce")
    instance_url = str(material.get("instance_url") or "").strip().rstrip("/")
    host = (urlparse(instance_url).hostname or "").lower()
    if (
        not instance_url.startswith("https://")
        or not host
        or not host.endswith(SALESFORCE_INSTANCE_SUFFIXES)
    ):
        raise PostExchangeVerifyFailed(
            "Salesforce did not report a usable org URL for this grant — the org was not connected"
        )
    info = await _get_json(
        transport,
        f"{instance_url}/services/oauth2/userinfo",
        headers=headers,
        provider_label="Salesforce",
        what="the org",
    )
    org_id = str(info.get("organization_id") or "").strip()
    username = str(info.get("preferred_username") or info.get("email") or "").strip()
    if not org_id:
        raise PostExchangeVerifyFailed("Salesforce did not report an org for this grant")
    connected_as = f"{host} · org {org_id}"
    if username:
        connected_as += f" · {username}"
    return {
        "instanceUrl": instance_url,
        "orgId": org_id,
        "username": username,
        "connectedAs": connected_as,
        "token_expires_at": _token_expiry(
            material, default_seconds=SALESFORCE_TOKEN_LIFETIME_SECONDS
        ),
    }


async def verify_hubspot_grant(
    *, material: dict[str, str], transport: _GetTransport
) -> dict[str, object]:
    """Prove the grant and refuse one that cannot write contacts.

    ``/oauth/v1/access-tokens/{token}`` names the portal (``hub_domain``, ``hub_id``),
    the authorizing user and — the part that matters — the SCOPES the token carries.
    The push writes contacts (``crm_backend.HubSpotCrmApi.create``/``update``), so a
    token without ``crm.objects.contacts.write`` would connect cleanly and fail on the
    first lead (round 6 WP-C: the registry requested read scopes only). A missing
    required scope is a refusal with the fix in it.
    """
    from app.connections import registry  # noqa: PLC0415 — avoid an import cycle

    token, _headers = _bearer(material, "HubSpot")
    info = await _get_json(
        transport,
        f"{HUBSPOT_TOKEN_INFO_ENDPOINT}/{quote(token, safe='')}",
        headers={},
        provider_label="HubSpot",
        what="the portal",
    )
    granted = {s for s in info.get("scopes", []) if isinstance(s, str)}
    spec = registry.get_provider("hubspot")
    required = set(spec.authorize.required_scopes) if spec and spec.authorize else set()
    missing = sorted(required - granted)
    if missing:
        raise PostExchangeVerifyFailed(
            f"HubSpot granted the token without {', '.join(missing)} — the HubSpot app's "
            f"scopes must include {', '.join(sorted(required))}; update the app, then "
            "reconnect"
        )
    hub_domain = str(info.get("hub_domain") or "").strip()
    hub_id = info.get("hub_id")
    user = str(info.get("user") or "").strip()
    connected_as = hub_domain or "HubSpot"
    if hub_id:
        connected_as += f" (portal {hub_id})"
    if user:
        connected_as += f" · {user}"
    lifetime = info.get("expires_in")
    expiry_source = {"expires_in": str(lifetime)} if isinstance(lifetime, (int, str)) else material
    return {
        "hubDomain": hub_domain,
        "hubId": str(hub_id) if hub_id else "",
        "user": user,
        "grantedScopes": " ".join(sorted(granted)),
        "connectedAs": connected_as,
        "token_expires_at": _token_expiry(
            expiry_source, default_seconds=HUBSPOT_TOKEN_LIFETIME_SECONDS
        ),
    }


def _google_verifier(required_scope: str, what: str) -> Verifier:
    """Google's ``tokeninfo`` names the scopes a token actually carries. Calendar and
    Drive are two scopes on ONE Google app (incremental auth), so a token that came
    back without the scope this surface needs is refused with the fix in it."""

    async def verify(*, material: dict[str, str], transport: _GetTransport) -> dict[str, object]:
        token, _headers = _bearer(material, "Google")
        info = await _get_json(
            transport,
            f"{GOOGLE_TOKENINFO_ENDPOINT}?access_token={quote(token, safe='')}",
            headers={},
            provider_label="Google",
            what=what,
        )
        granted = {s for s in str(info.get("scope") or "").split() if s}
        if required_scope not in granted:
            raise PostExchangeVerifyFailed(
                f"Google granted the token without the {what} permission — reconnect and "
                "allow it on the consent screen"
            )
        account = str(info.get("email") or "").strip().lower()
        connected_as = f"Google · {what} permission granted"
        if account:
            connected_as = f"{account} · {what} permission granted"
        lifetime = info.get("expires_in")
        expiry_source = (
            {"expires_in": str(lifetime)} if isinstance(lifetime, (int, str)) else material
        )
        updates: dict[str, object] = {
            "grantedScopes": " ".join(sorted(granted)),
            "connectedAs": connected_as,
        }
        if account:
            updates["googleAccount"] = account
        expiry = _token_expiry(expiry_source)
        if expiry:
            updates["token_expires_at"] = expiry
        return updates

    return verify


verify_google_calendar = _google_verifier(GOOGLE_CALENDAR_SCOPE, "calendar")
verify_google_drive = _google_verifier(GOOGLE_DRIVE_SCOPE, "Drive read")


async def verify_notion_workspace(
    *, material: dict[str, str], transport: _GetTransport
) -> dict[str, object]:
    """``/v1/users/me`` answers as the integration's bot user and names the workspace.
    Notion tokens do not expire, so no expiry is stamped."""
    _token, headers = _bearer(material, "Notion")
    headers["Notion-Version"] = NOTION_VERSION
    info = await _get_json(
        transport,
        NOTION_ME_ENDPOINT,
        headers=headers,
        provider_label="Notion",
        what="the workspace",
    )
    raw_bot = info.get("bot")
    bot: dict[str, Any] = raw_bot if isinstance(raw_bot, dict) else {}
    workspace = str(material.get("workspace_name") or bot.get("workspace_name") or "").strip()
    bot_name = str(info.get("name") or "").strip()
    connected_as = (
        f"Notion workspace {workspace}" if workspace else f"Notion · {bot_name or 'integration'}"
    )
    return {
        "workspaceName": workspace,
        "botName": bot_name,
        "connectedAs": connected_as,
    }


async def verify_calendly_user(
    *, material: dict[str, str], transport: _GetTransport
) -> dict[str, object]:
    """``/users/me`` names the Calendly user and scheduling page; the first active event
    type is recorded as the booking target (WP-H's scheduling-link contract reads it).
    No active event type is not a refusal — the dashboard says so and the owner
    creates one — but the count is recorded so the honest state is visible."""
    _token, headers = _bearer(material, "Calendly")
    me = await _get_json(
        transport,
        CALENDLY_ME_ENDPOINT,
        headers=headers,
        provider_label="Calendly",
        what="the account",
    )
    raw_resource = me.get("resource")
    resource: dict[str, Any] = raw_resource if isinstance(raw_resource, dict) else {}
    user_uri = str(resource.get("uri") or "").strip()
    if not user_uri:
        raise PostExchangeVerifyFailed("Calendly did not report a user for this grant")
    name = str(resource.get("name") or "").strip()
    scheduling_url = str(resource.get("scheduling_url") or "").strip()
    organization = str(resource.get("current_organization") or "").strip()
    types = await _get_json(
        transport,
        f"{CALENDLY_EVENT_TYPES_ENDPOINT}?user={quote(user_uri, safe='')}&active=true&count=20",
        headers=headers,
        provider_label="Calendly",
        what="the event types",
    )
    collection = [t for t in types.get("collection", []) if isinstance(t, dict)]
    active = [t for t in collection if t.get("active", True)]
    first = active[0] if active else None
    connected_as = name or "Calendly"
    if scheduling_url:
        connected_as += f" · {scheduling_url}"
    connected_as += f" · booking: {first.get('name')}" if first else " · no active event type yet"
    return {
        "userUri": user_uri,
        "userName": name,
        "schedulingUrl": scheduling_url,
        "organizationUri": organization,
        "eventTypeUri": str(first.get("uri") or "") if first else "",
        "eventTypeName": str(first.get("name") or "") if first else "",
        "eventTypeCount": len(active),
        "connectedAs": connected_as,
        "token_expires_at": _token_expiry(
            material, default_seconds=CALENDLY_TOKEN_LIFETIME_SECONDS
        ),
    }


async def verify_meta_grant(
    *, material: dict[str, str], transport: _GetTransport
) -> dict[str, object]:
    """Graph ``/me`` proves the (long-lived) user token and names who authorized. Which
    Page / number the channel answers on is still chosen at bind time and checked
    against the grant's assets there (``meta_assets``)."""
    _token, headers = _bearer(material, "Meta")
    me = await _get_json(
        transport,
        f"{meta_me_endpoint()}?fields=id,name",
        headers=headers,
        provider_label="Meta",
        what="the account",
    )
    user_id = str(me.get("id") or "").strip()
    if not user_id:
        raise PostExchangeVerifyFailed("Meta did not report a user for this grant")
    name = str(me.get("name") or "").strip()
    return {
        "metaUserId": user_id,
        "metaUserName": name,
        "connectedAs": f"Meta · {name or user_id}",
        "token_expires_at": _token_expiry(
            material, default_seconds=META_LONG_LIVED_LIFETIME_SECONDS
        ),
    }


async def verify_instagram_grant(
    *, material: dict[str, str], transport: _GetTransport
) -> dict[str, object]:
    """``graph.instagram.com/me`` proves the long-lived token and names the professional
    account (@username) the DMs will be answered as."""
    _token, headers = _bearer(material, "Instagram")
    me = await _get_json(
        transport,
        f"{instagram_me_endpoint()}?fields=id,username",
        headers=headers,
        provider_label="Instagram",
        what="the account",
    )
    user_id = str(me.get("id") or material.get("user_id") or "").strip()
    if not user_id:
        raise PostExchangeVerifyFailed("Instagram did not report an account for this grant")
    username = str(me.get("username") or "").strip()
    return {
        "instagramUserId": user_id,
        "instagramUsername": username,
        "connectedAs": f"@{username}" if username else f"Instagram account {user_id}",
        "token_expires_at": _token_expiry(
            material, default_seconds=META_LONG_LIVED_LIFETIME_SECONDS
        ),
    }


# Provider -> verifier. Absent means "no post-exchange verification", today's behavior.
POST_EXCHANGE_VERIFIERS: dict[str, Verifier] = {
    "outlook": verify_outlook_mailbox,
    "twilio": verify_twilio_account,
    "salesforce": verify_salesforce_org,
    "hubspot": verify_hubspot_grant,
    "google_calendar": verify_google_calendar,
    "google_drive": verify_google_drive,
    "notion": verify_notion_workspace,
    "calendly": verify_calendly_user,
    "meta": verify_meta_grant,
    "instagram": verify_instagram_grant,
}
