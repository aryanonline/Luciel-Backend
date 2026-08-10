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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.whatsapp_adapter import MetaSender, NullMetaSender
from app.connections.secrets import SecretStore, get_secret_store
from app.models.connection import InstanceConnection

_GRAPH_BASE = "https://graph.facebook.com/v19.0"
# Instagram Login tokens are issued by, and only accepted at, Instagram's own Graph host.
_INSTAGRAM_GRAPH_BASE = "https://graph.instagram.com/v23.0"

#: The key ``exchange_code_for_token`` writes the Graph token under.
FIELD_ACCESS_TOKEN = "access_token"  # noqa: S105 — field NAME, not a credential  # nosec B105


class GraphMetaSender:
    """Live Meta Graph sender. Requires a page/WhatsApp access token.

    ``graph_base`` is the host the token was issued by. An Instagram Login token is not a
    Facebook Graph token and graph.facebook.com rejects it, so the two cannot share a base
    URL just because they share a request shape.
    """

    def __init__(self, access_token: str, *, graph_base: str = _GRAPH_BASE) -> None:
        self._token = access_token
        self._graph_base = graph_base

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


async def _channel_auth_row(
    session: AsyncSession, admin_id: uuid.UUID, instance_id: uuid.UUID, connection_type: str
) -> InstanceConnection | None:
    return (
        await session.execute(
            select(InstanceConnection).where(
                InstanceConnection.admin_id == admin_id,
                InstanceConnection.instance_id == instance_id,
                InstanceConnection.connection_type == connection_type,
                InstanceConnection.status != "revoked",
            )
        )
    ).scalar_one_or_none()


async def load_tenant_meta_token(
    session: AsyncSession,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    *,
    connection_type: str = "channel_auth",
    store: SecretStore | None = None,
) -> str | None:
    """The tenant's Graph access token for this grant, or None if it is not connected."""
    conn = await _channel_auth_row(session, admin_id, instance_id, connection_type)
    if conn is None or not conn.secret_ref:
        return None
    material = await (store or get_secret_store()).get(conn.secret_ref) or {}
    token = material.get(FIELD_ACCESS_TOKEN, "")
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
    base = _INSTAGRAM_GRAPH_BASE if connection_type == "instagram_auth" else _GRAPH_BASE
    return GraphMetaSender(access_token=token, graph_base=base)
