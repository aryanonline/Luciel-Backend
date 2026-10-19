"""GET /api/v1/public/config — the PUBLIC values the frontend needs at runtime (WP-7).

The frontend baked these in at build time: the hCaptcha SITE key (the backend's
``hcaptcha_site_key`` had no reader at all), the frontend and API origins, and the widget
embed script URL. A value baked into a build cannot follow the environment it is served
from — a dev bundle and a prod bundle need different keys and origins, and moving the
dev hostnames (owner decision D6) meant a rebuild per value. This route serves them from
the same settings the backend itself runs on, so the two cannot disagree.

Every value here is PUBLIC by nature: a site key is embedded in every page that renders
the captcha, the origins are in every URL bar, the embed URL is pasted onto customer
sites. Nothing secret is reachable from this module — ``PublicConfigOut`` is the whole
surface, and the contract test pins its exact key set. Unauthenticated (the signup and
contact pages need it before anyone has a session), cacheable, and on the CONTROL plane
with the rest of the dashboard's API.
"""

from __future__ import annotations

from fastapi import APIRouter, Response

from app.config import get_settings
from app.schemas_api.common import WireModel

router = APIRouter(prefix="/api/v1/public", tags=["public"])

#: How long a browser/CDN may reuse the answer. The values change only on a deploy or a
#: platform-secret rotation; five minutes bounds how stale a page can be afterwards.
PUBLIC_CONFIG_MAX_AGE_SECONDS = 300


class PublicConfigOut(WireModel):
    """The frontend's runtime configuration. ``None`` = not configured here (the
    frontend keeps its own fallback, e.g. hCaptcha's published test site key locally)."""

    hcaptcha_site_key: str | None
    app_base_url: str
    api_base_url: str | None
    widget_embed_url: str | None


@router.get("/config", response_model=PublicConfigOut)
async def public_config(response: Response) -> PublicConfigOut:
    settings = get_settings()
    response.headers["Cache-Control"] = f"public, max-age={PUBLIC_CONFIG_MAX_AGE_SECONDS}"
    return PublicConfigOut(
        hcaptcha_site_key=settings.hcaptcha_site_key or None,
        app_base_url=settings.app_base_url.rstrip("/"),
        api_base_url=settings.public_api_base_url.rstrip("/") or None,
        widget_embed_url=settings.widget_embed_url or None,
    )
