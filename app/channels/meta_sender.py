"""Meta Graph API sender for WhatsApp + Instagram/Messenger (Architecture §3.1.2/§5.12).

The sending credential belongs to the TENANT, not to VantageMind (P1-1). WhatsApp,
Instagram DM and Messenger all ride the one ``channel_auth`` connection the admin
completes via Meta OAuth (Decision #7), and the access token that grant returns lives in
the SecretStore under that connection's ``secret_ref`` (§3.8.3, never in Postgres). This
mirrors ``tenant_twilio`` exactly, for the same reason: a send authorized by a
platform-wide token would go out from a Meta asset the customer never connected, billed
against an app they do not own, and would reach every tenant's recipients through one
credential.

There is deliberately no platform-token fallback. The previous ``build_meta_sender()``
read ``settings.meta_access_token`` and returned a live sender from it, which meant a
single environment variable silently became the sender for every tenant on the box; that
path is gone rather than deprecated, so it cannot be reached by accident.

Two implementations behind the MetaSender Protocol:

- ``GraphMetaSender`` — live: posts to the Meta Graph API with the tenant's token.
- ``NullMetaSender``  — mock/CI, or a tenant who has not connected Meta: records sends
  in-memory, no network. ``null_in_live`` turns the second case into a refusal with a
  specific reason instead of a fabricated delivery.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.whatsapp_adapter import MetaSender, NullMetaSender
from app.config import INSTAGRAM_GRAPH_HOST, meta_graph_base
from app.connections.secrets import SecretStore

# The Graph version comes from settings (``meta_graph_version``, round 7 WP-7) — this
# module pinned an older version than the Instagram host it also calls. Instagram Login
# tokens are issued by, and only accepted at, Instagram's own Graph host.


class GraphMetaSender:
    """Live Meta Graph sender. Requires a page/WhatsApp access token.

    ``graph_base`` is the host the token was issued by. An Instagram Login token is not a
    Facebook Graph token and graph.facebook.com rejects it, so the two cannot share a base
    URL just because they share a request shape.
    """

    def __init__(self, access_token: str, *, graph_base: str | None = None) -> None:
        self._token = access_token
        self._graph_base = graph_base or meta_graph_base()

    async def send_message(
        self, *, channel: str, sender_id: str, recipient_id: str, body: str
    ) -> str | None:
        import httpx  # noqa: PLC0415 — lazy/guarded (CI has no network)

        url = f"{self._graph_base}/{sender_id}/messages"
        if channel == "whatsapp":
            payload = {
                "messaging_product": "whatsapp",
                "to": recipient_id,
                "type": "text",
                "text": {"body": body},
            }
        else:  # messenger / instagram (same Messenger-API send shape)
            payload = {
                "recipient": {"id": recipient_id},
                "message": {"text": body},
            }
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                url,
                params={"access_token": self._token},
                json=payload,
            )
        if resp.status_code >= 400:
            return None
        data = resp.json()
        # WhatsApp returns messages[0].id; Messenger returns message_id.
        if "messages" in data and data["messages"]:
            return str(data["messages"][0].get("id"))
        return str(data.get("message_id")) if data.get("message_id") else None


async def load_tenant_meta_token(
    session: AsyncSession,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    *,
    connection_type: str = "channel_auth",
    store: SecretStore | None = None,
) -> str | None:
    """The tenant's Graph access token for this grant, or None if it is not connected.

    Round 5B item 14: refreshed at CALL time when the stored expiry HINT says the
    token is near/past expiry — this was the one sender reading only the raw
    stored token, so an expired grant meant dead WhatsApp/Messenger sends until
    the 6-hourly sweep noticed. A row with NO hint keeps the raw read (legacy
    rows predate hints; treating absent-as-stale here would force a live
    fb_exchange round-trip on every send-build, which the sweep, not the send
    path, owns)."""
    from sqlalchemy import select  # noqa: PLC0415

    from app.connections.secrets import get_secret_store  # noqa: PLC0415
    from app.models.connection import InstanceConnection  # noqa: PLC0415

    conn = (
        await session.execute(
            select(InstanceConnection).where(
                InstanceConnection.admin_id == admin_id,
                InstanceConnection.instance_id == instance_id,
                InstanceConnection.connection_type == connection_type,
                InstanceConnection.status != "revoked",
            )
        )
    ).scalar_one_or_none()
    if conn is None or not conn.secret_ref:
        return None

    from datetime import UTC, datetime  # noqa: PLC0415

    from app.connections.health import REFRESH_LEAD, _expiry_of  # noqa: PLC0415

    expiry = _expiry_of(conn)
    if expiry is not None and datetime.now(UTC) >= expiry - REFRESH_LEAD:
        from app.connections.refresh import ensure_fresh_access_token  # noqa: PLC0415

        token = await ensure_fresh_access_token(
            session, admin_id, instance_id, connection_type=connection_type, store=store
        )
        return token or None

    material = await (store or get_secret_store()).get(conn.secret_ref) or {}
    token = material.get("access_token", "")
    return token or None


async def build_tenant_meta_sender(
    session: AsyncSession,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    *,
    connection_type: str = "channel_auth",
    store: SecretStore | None = None,
) -> MetaSender:
    """The sender for this tenant's WhatsApp/IG/Messenger traffic: THEIR grant.

    ``connection_type`` selects the grant — the Facebook one for WhatsApp and Messenger,
    ``instagram_auth`` for Instagram DMs, which is a different app issuing a token only
    graph.instagram.com accepts.

    Outside live mode the hermetic Null sender is returned, so dev/CI never reaches the
    network even with a real token stored. In live mode a tenant with no connection also
    gets Null — ``null_in_live`` then refuses the send with ``meta_sender_not_provisioned``
    rather than letting it look delivered.
    """
    from app.config import get_settings

    if get_settings().integration_mode != "live":
        return NullMetaSender()
    token = await load_tenant_meta_token(
        session, admin_id, instance_id, connection_type=connection_type, store=store
    )
    if not token:
        return NullMetaSender()
    base = (
        meta_graph_base(INSTAGRAM_GRAPH_HOST)
        if connection_type == "instagram_auth"
        else meta_graph_base()
    )
    return GraphMetaSender(access_token=token, graph_base=base)
