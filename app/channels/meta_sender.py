"""Meta Graph API sender for WhatsApp + Instagram/Messenger (Architecture §3.1.2/§5.12).

Two implementations behind the MetaSender Protocol, mirroring the SES/Twilio pattern:

- ``GraphMetaSender`` — live: posts to the Meta Graph API. Lazy httpx use; the page/
  WhatsApp access token is read from config (bridged from the platform secret / the
  per-tenant channel_auth connection in a full deployment).
- ``NullMetaSender``  — mock/CI: records sends in-memory, no network. Default.

Live send is gated on Meta credentials the owner must provision (App secret + access
token); until then ``build_meta_sender`` returns the Null sender so the suite and dev
stay hermetic.
"""

from __future__ import annotations

from app.channels.whatsapp_adapter import MetaSender, NullMetaSender

_GRAPH_BASE = "https://graph.facebook.com/v19.0"


class GraphMetaSender:
    """Live Meta Graph sender. Requires a page/WhatsApp access token."""

    def __init__(self, access_token: str) -> None:
        self._token = access_token

    async def send_message(
        self, *, channel: str, sender_id: str, recipient_id: str, body: str
    ) -> str | None:
        import httpx  # noqa: PLC0415 — lazy/guarded (CI has no network)

        if channel == "whatsapp":
            url = f"{_GRAPH_BASE}/{sender_id}/messages"
            payload = {
                "messaging_product": "whatsapp",
                "to": recipient_id,
                "type": "text",
                "text": {"body": body},
            }
        else:  # instagram_messenger
            url = f"{_GRAPH_BASE}/{sender_id}/messages"
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


def build_meta_sender() -> MetaSender:
    """Live GraphMetaSender when configured, else NullMetaSender (mock/CI)."""
    from app.config import get_settings

    s = get_settings()
    token = getattr(s, "meta_access_token", "") or ""
    if s.integration_mode == "live" and token:
        return GraphMetaSender(access_token=token)
    return NullMetaSender()
