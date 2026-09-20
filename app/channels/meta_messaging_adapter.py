"""Instagram DM + Facebook Messenger adapter (Architecture §3.1.2, §3.1.3).

Instagram DM and Messenger SHARE one adapter class — they ride the same Graph/Messenger
API surface — but each instance carries its OWN channel id (``instagram`` or
``messenger``, canonical §3.1.2: the wire enums carry separate channels). The webhook
edge picks the channel from the payload's ``object`` field, so an Instagram DM opens an
``instagram`` session and a Page message a ``messenger`` one. Inbound is a Meta webhook
whose X-Hub-Signature-256 is verified BEFORE tenant lookup (§3.1.3/§3.7.2); the
destination Instagram-account ID or Facebook-page ID resolves to the tenant. These
channels bind to the admin's Meta page/account, NOT a phone number (§3.1.3). Outbound
goes through the shared Meta Graph sender (fake/Null in mock/CI).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.channels.base import (
    DeliveryReceipt,
    InboundMessage,
    InboundRejected,
    InstanceContext,
    OutboundMessage,
)
from app.channels.signatures import verify_hmac_shared_secret
from app.channels.sms_adapter import classify_keyword
from app.channels.whatsapp_adapter import MetaSender, NullMetaSender


def _is_echo(event: dict[str, Any]) -> bool:
    return bool((event.get("message") or {}).get("is_echo"))


def _first_messaging_event(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Extract the first CUSTOMER messaging event from Meta's Messenger/IG webhook.

    Shape: entry[].messaging[] (Messenger) — each entry carries the recipient (the
    tenant's page/IG-account id) and the sender (the lead's PSID/IGSID).

    Two event kinds ride the same webhook and are NOT customer turns (round 7 WP-3a):

    * ``message.is_echo`` — the ``message_echoes`` field: an echo of the page's OWN
      outbound (Luciel's reply, or a person answering from the Page inbox), with
      ``sender.id`` = the page. Ingesting it opened a conversation keyed on the page
      id and answered Luciel's own words. Skipped.
    * ``entry.standby`` — the handover protocol's feed for an app that does NOT hold
      the thread. Luciel implements no handover today, so a standby event belongs to
      whichever app does; answering it would speak over that app. Ignored.
    """
    for entry in payload.get("entry", []):
        for event in entry.get("messaging") or []:
            if event.get("message") and not _is_echo(event):
                return {"entry": entry, "event": event}
    return None


def _no_message_reason(payload: dict[str, Any]) -> str:
    """Why ``_first_messaging_event`` found nothing — an honest rejection reason for
    the inbound log, so an echo storm or a standby feed is visible as itself."""
    saw_echo = saw_standby = False
    for entry in payload.get("entry", []):
        if any(_is_echo(event) for event in entry.get("messaging") or []):
            saw_echo = True
        if entry.get("standby"):
            saw_standby = True
    if saw_echo:
        return "meta_echo_ignored"
    if saw_standby:
        return "meta_standby_ignored"
    return "meta_no_message"


class MetaMessagingAdapter:
    def __init__(self, sender: MetaSender | None = None, *, channel: str = "messenger") -> None:
        # ``channel`` is the split channel id this instance serves (§3.1.2): the webhook
        # edge passes "instagram" for object=="instagram" payloads and "messenger" for
        # Page ones, so sessions/rate-limits/arbitration see the real channel.
        self.channel_id = channel
        # Per-tenant Graph token (P1-1) — see the note in ``WhatsAppAdapter.__init__``.
        self._sender = sender or NullMetaSender()

    async def verify_inbound(self, request: dict[str, Any]) -> InstanceContext:
        # Signature FIRST (§3.1.3): X-Hub-Signature-256 HMAC-SHA256 over the raw body.
        app_secret = request["app_secret"]
        raw_body: bytes = request["raw_body"]
        signature = request.get("x_hub_signature_256", "")
        if not verify_hmac_shared_secret(app_secret, raw_body, signature):
            raise InboundRejected("meta_signature_invalid")

        parsed = _first_messaging_event(request["payload"])
        if parsed is None:
            raise InboundRejected(_no_message_reason(request["payload"]))
        # Destination = tenant's page/IG-account id (entry.id), the resolution key.
        destination_id = parsed["entry"].get("id")
        if not destination_id:
            raise InboundRejected("meta_no_destination")
        # An IG account id is bound on the Instagram grant and a Page id on the Facebook
        # one, and the webhook entry does not say which kind of id it is carrying. Try
        # both: guessing from the payload's ``object`` would misroute a tenant who has
        # connected only one of the two.
        resolve = request["resolve"]
        for connection_type in ("instagram_auth", "channel_auth"):
            try:
                ctx: InstanceContext = await resolve(connection_type, destination_id)
            except InboundRejected:
                continue
            return ctx
        raise InboundRejected(f"unresolvable_destination:{self.channel_id}")

    async def receive(self, request: dict[str, Any], ctx: InstanceContext) -> InboundMessage:
        parsed = _first_messaging_event(request["payload"])
        if parsed is None:
            # verify_inbound already guaranteed a message event; a None here means the
            # payload changed between verify and receive. Reject rather than crash.
            raise InboundRejected(_no_message_reason(request["payload"]))
        event = parsed["event"]
        sender_id = str((event.get("sender") or {}).get("id") or "")
        if not sender_id:
            # No PSID/IGSID: nothing to key the conversation on and nothing to reply
            # to (round 7 WP-3a). Dropped here, with a reason, rather than admitted as
            # customer_identifier "" — which opened an unkeyable session per message.
            raise InboundRejected("meta_no_sender")
        body = (event.get("message") or {}).get("text", "")
        metadata: dict[str, Any] = {
            "mid": (event.get("message") or {}).get("mid"),
            "destination_id": parsed["entry"].get("id"),
        }
        # STOP/START/HELP on Messenger / Instagram too (audit F074).
        keyword = classify_keyword(body)
        if keyword is not None:
            metadata["opt_keyword"] = keyword
        return InboundMessage(
            admin_id=ctx.admin_id,
            instance_id=ctx.instance_id,
            channel=self.channel_id,
            session_id=None,
            customer_identifier=sender_id,
            body=body,
            channel_metadata=metadata,
            received_at=datetime.now(UTC),
        )

    async def send(self, message: OutboundMessage) -> DeliveryReceipt:
        sender_id = message.metadata.get("destination_id", "")
        try:
            mid = await self._sender.send_message(
                channel=self.channel_id,
                sender_id=sender_id,
                recipient_id=message.to,
                body=message.body,
            )
        except Exception as exc:  # noqa: BLE001
            return DeliveryReceipt(accepted=False, detail=f"meta_error:{type(exc).__name__}")
        if mid is None:
            return DeliveryReceipt(accepted=False, detail="meta_rejected")
        return DeliveryReceipt(accepted=True, provider_message_id=mid)
