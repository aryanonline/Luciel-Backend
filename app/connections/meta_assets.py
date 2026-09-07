"""Which Meta assets a grant actually covers (2026-09-05 audit F082).

``bind_destination`` used to write whatever id the owner typed. A mistyped Page id,
WhatsApp phone-number id or Instagram account id produced a channel that read
"Answering on …" and silently received nothing: inbound resolution matched no row
and the webhook answered 200. Meta can say what the authorized user owns, so the
bind asks — the same "prove it before it goes live" posture the Outlook and Twilio
post-exchange verifiers take (``verify.py``).

Asset ids per grant:
  * ``channel_auth`` (Facebook Login): the Pages the user manages (``/me/accounts``),
    the Instagram business accounts linked to those Pages, and every WhatsApp
    phone-number id under the user's businesses' WABAs.
  * ``instagram_auth`` (Instagram Login): the Instagram professional account itself.

Only the LIVE posture talks to Meta (``lister_for_posture``); mock has no Meta and no
grant to check against, and says so rather than pretending — the seam is what the
tests replace.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

GRAPH_BASE = "https://graph.facebook.com/v19.0"
INSTAGRAM_GRAPH_BASE = "https://graph.instagram.com/v23.0"
_MAX_PAGES = 10
# The only origins a ``paging.next`` URL may point at (adversarial review R-WP2-1):
# the next URL carries the grant's access token in its query string, so following
# whatever the reply named would have sent that token wherever the reply said.
_GRAPH_ORIGINS = ("https://graph.facebook.com/", "https://graph.instagram.com/")


class AssetLookupFailed(Exception):
    """Meta could not be asked (transport error, rejected token, malformed reply)."""


class _GetTransport(Protocol):
    async def get(
        self, url: str, *, params: dict[str, str] | None = None, headers: Any = None
    ) -> Any: ...


AssetLister = Callable[..., Awaitable[set[str]]]


async def _get_json(transport: _GetTransport, url: str, params: dict[str, str]) -> dict[str, Any]:
    try:
        response = await transport.get(url, params=params)
    except Exception as exc:  # noqa: BLE001 — every transport failure is "could not ask"
        raise AssetLookupFailed(f"could not reach Meta ({type(exc).__name__})") from exc
    status = int(getattr(response, "status_code", 0))
    if status >= 400:
        raise AssetLookupFailed(f"Meta rejected the asset lookup (HTTP {status})")
    try:
        data = response.json()
    except Exception as exc:  # noqa: BLE001
        raise AssetLookupFailed("Meta returned an unreadable reply") from exc
    if not isinstance(data, dict):
        raise AssetLookupFailed("Meta returned an unexpected reply shape")
    return data


async def _get_all(
    transport: _GetTransport, url: str, params: dict[str, str]
) -> list[dict[str, Any]]:
    """Follow Graph ``paging.next`` (bounded) and return every ``data`` item."""
    items: list[dict[str, Any]] = []
    next_url: str | None = url
    next_params: dict[str, str] | None = params
    for _ in range(_MAX_PAGES):
        if next_url is None:
            break
        page = await _get_json(transport, next_url, next_params or {})
        items.extend(i for i in page.get("data", []) if isinstance(i, dict))
        paging = page.get("paging") or {}
        raw_next = paging.get("next") if isinstance(paging, dict) else None
        next_url = (
            str(raw_next)
            if raw_next and str(raw_next).startswith(_GRAPH_ORIGINS)
            else None  # an off-Graph next URL ends the walk; it is never fetched
        )
        next_params = None  # the next URL already carries its query string
    return items


async def list_grant_assets(
    *, material: dict[str, str], connection_type: str, transport: _GetTransport
) -> set[str]:
    """Every asset id the grant may answer as. Raises AssetLookupFailed if Meta
    cannot be asked — never returns a guess."""
    token = material.get("access_token", "")
    if not token:
        raise AssetLookupFailed("the grant holds no access token")
    ids: set[str] = set()
    if connection_type == "instagram_auth":
        me = await _get_json(
            transport, f"{INSTAGRAM_GRAPH_BASE}/me", {"fields": "id,user_id", "access_token": token}
        )
        for key in ("id", "user_id"):
            if me.get(key):
                ids.add(str(me[key]))
        return ids

    pages = await _get_all(
        transport,
        f"{GRAPH_BASE}/me/accounts",
        {"fields": "id,instagram_business_account", "access_token": token},
    )
    for page in pages:
        if page.get("id"):
            ids.add(str(page["id"]))
        linked = page.get("instagram_business_account")
        if isinstance(linked, dict) and linked.get("id"):
            ids.add(str(linked["id"]))
    businesses = await _get_all(
        transport, f"{GRAPH_BASE}/me/businesses", {"fields": "id", "access_token": token}
    )
    for business in businesses:
        if not business.get("id"):
            continue
        wabas = await _get_all(
            transport,
            f"{GRAPH_BASE}/{business['id']}/owned_whatsapp_business_accounts",
            {"fields": "id", "access_token": token},
        )
        for waba in wabas:
            if not waba.get("id"):
                continue
            numbers = await _get_all(
                transport,
                f"{GRAPH_BASE}/{waba['id']}/phone_numbers",
                {"fields": "id", "access_token": token},
            )
            for number in numbers:
                if number.get("id"):
                    ids.add(str(number["id"]))
    return ids


def lister_for_posture() -> AssetLister | None:
    """The asset lister for this deployment, or None when there is no Meta to ask.

    Live mode returns the Graph lister. Mock mode (CI, local, and production until the
    owner-gated cutover) has no Meta credentials and no real grant, so the bind cannot
    be checked and is not — explicitly, here, rather than by a lister that returns
    whatever it was asked. Tests replace this function to exercise both outcomes."""
    from app.config import get_settings  # noqa: PLC0415 — avoid import cycle

    if get_settings().integration_mode == "live":
        return list_grant_assets
    return None
