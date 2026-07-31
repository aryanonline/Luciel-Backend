"""GET /api/v1/connections/{client}/callback — the provider's OAuth redirect target (§3.8).

This is the route the PROVIDER sends the browser to. It is the other half of the connect
flow from ``POST /api/v1/admin/connections/{id}/oauth-callback``: that one is called by
our own frontend with a JSON body, this one is entered by Google/HubSpot/Salesforce/
Notion/Meta navigating the admin's browser to the redirect URI registered on the OAuth
app. Those registered URIs point here, and until now nothing served them — so every live
consent screen ended on a 404 and no connection could ever leave ``unconfigured``.

Three things follow from being a browser redirect rather than an API call:

  * **No session, no path parameters to trust.** The URL is fixed at app-registration
    time, so it cannot carry the connection id, and the provider's navigation is not
    guaranteed to carry our session cookie. The signed ``state`` is therefore the only
    thing that says which attempt this is — safe because it is signed with the platform
    key, short-lived, and single-use against the jti parked on the row (§5.1).

  * **The path segment is the OAuth CLIENT, not the provider.** Drive and Calendar are
    two scopes on one Google app and share one redirect URI, so ``/google/callback``
    serves both ``google_drive`` and ``google_calendar``. One handler parametrised by
    client label serves every provider rather than one near-duplicate route each.

  * **Every outcome is a 302, never an error page.** A forged state, an expired attempt,
    a user who clicked "Deny" — all of it lands the admin back on the dashboard with a
    reason they can act on. Returning 4xx/5xx here would strand them on a blank provider
    redirect with no way back.
"""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Query
from fastapi.responses import RedirectResponse

from app.api.v1.admin.connections import (
    OAuthExchangeRejected,
    OAuthStateRejected,
    complete_oauth_connect,
)
from app.config import get_settings
from app.connections import oauth_flow, registry

router = APIRouter(prefix="/api/v1/connections", tags=["connections"])


def _return_to(**params: str) -> RedirectResponse:
    """302 back to the dashboard page that owns connections, carrying the outcome."""
    s = get_settings()
    base = f"{s.app_base_url.rstrip('/')}{s.oauth_return_path}"
    # 303: the browser arrived here by GET and must continue by GET. 302 is ambiguous
    # enough that some agents preserve the method.
    return RedirectResponse(f"{base}?{urlencode(params)}", status_code=303)


@router.get("/{client}/callback")
async def provider_oauth_callback(
    client: str,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
) -> RedirectResponse:
    """Redeem the provider's ``?code`` and bounce the browser back to the dashboard.

    ``error`` is the provider's own failure channel (the admin denied consent, the app is
    misconfigured); it arrives INSTEAD of a code, so it is handled before anything else
    and never reaches the exchange.
    """
    if error:
        return _return_to(status="error", reason="provider_denied", client=client)

    try:
        claims = oauth_flow.decode_state(state)
    except oauth_flow.StateRejected:
        # Nothing trustworthy to report against — not even which connection was meant.
        return _return_to(status="error", reason="invalid_state", client=client)

    if registry.oauth_client_label(claims.provider) != client:
        # The state was minted for a provider on a DIFFERENT OAuth app than the one whose
        # redirect URI we were reached on. Signed or not, that pairing is incoherent.
        return _return_to(status="error", reason="provider_mismatch", client=client)

    connection_id = str(claims.connection_id)
    if not code:
        return _return_to(
            status="error",
            reason="missing_code",
            provider=claims.provider,
            connectionId=connection_id,
        )

    try:
        await complete_oauth_connect(
            admin_id=claims.admin_id,
            # No admin session on a provider-initiated navigation; the audit row records
            # the tenant and the reason, with no user to attribute the click to.
            actor_user_id=None,
            connection_id=claims.connection_id,
            code=code,
            state=state,
        )
    except OAuthStateRejected:
        return _return_to(
            status="error",
            reason="invalid_state",
            provider=claims.provider,
            connectionId=connection_id,
        )
    except OAuthExchangeRejected:
        # The reason is already persisted to status_detail by the exchange path; keep the
        # provider's wording out of the URL so nothing sensitive lands in browser history.
        return _return_to(
            status="error",
            reason="exchange_failed",
            provider=claims.provider,
            connectionId=connection_id,
        )

    return _return_to(status="connected", provider=claims.provider, connectionId=connection_id)
