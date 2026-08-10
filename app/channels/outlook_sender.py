"""Graph sendMail for the BYO Outlook mailbox (§3.1.6a) — the TENANT's credential.

Mirrors ``meta_sender``/``tenant_twilio``: the sending credential belongs to the
customer, never the platform. A reply goes out AS the connected mailbox — it lands in
the customer's own Sent folder (``saveToSentItems``) and threads under the original
message when the inbound context is known (``/messages/{id}/reply``), because that is
what "Luciel answers from your mailbox" has to mean to the person reading the thread.

Honesty notes:
  * Graph's ``sendMail``/``reply`` answer **202 Accepted with no message id** — so a
    successful send reports ``provider_message_id=None`` rather than a fabricated id.
  * ``NullOutlookSender`` is the mock/CI + not-connected stand-in. In live mode it is
    refused up front via ``null_in_live`` (``mailbox_reconnect_needed``), and its own
    ``send_mail`` returns False there as the second layer — nothing can fabricate a
    delivered reply through a mailbox that cannot send.
"""

from __future__ import annotations

import uuid
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.tenant_outlook import ensure_fresh_token
from app.connections.secrets import SecretStore

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


class MailboxSender(Protocol):
    """Send one plain-text mail from the connected mailbox. True iff accepted."""

    async def send_mail(
        self, *, to: str, subject: str, body: str, in_reply_to: str | None = None
    ) -> bool: ...


class GraphMailSender:
    """Live Graph sender bound to one tenant's (fresh) access token."""

    def __init__(self, access_token: str) -> None:
        self._token = access_token

    async def send_mail(
        self, *, to: str, subject: str, body: str, in_reply_to: str | None = None
    ) -> bool:
        import httpx  # noqa: PLC0415 — lazy/guarded (CI has no network)

        headers = {"Authorization": f"Bearer {self._token}"}
        if in_reply_to:
            # Real threading: the reply hangs under the customer's own message with
            # the provider filling in recipients/subject/references from the original.
            url = f"{GRAPH_BASE}/me/messages/{in_reply_to}/reply"
            payload: dict[str, object] = {"comment": body}
        else:
            url = f"{GRAPH_BASE}/me/sendMail"
            payload = {
                "message": {
                    "subject": subject,
                    "body": {"contentType": "Text", "content": body},
                    "toRecipients": [{"emailAddress": {"address": to}}],
                },
                # The owner's Sent folder is the audit trail THEY know how to read.
                "saveToSentItems": True,
            }
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(url, json=payload, headers=headers)
        return response.status_code == 202


class NullOutlookSender:
    """Mock/CI + not-connected stand-in. Records intent; never any network."""

    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    async def send_mail(
        self, *, to: str, subject: str, body: str, in_reply_to: str | None = None
    ) -> bool:
        from app.config import live_mode  # noqa: PLC0415 — read the mode at call time

        self.sent.append(
            {"to": to, "subject": subject, "body": body, "in_reply_to": in_reply_to}
        )
        # Mock: an accepted fake is honest (nothing real exists to disagree with it).
        # Live: nothing left the process — saying otherwise would fabricate a delivery.
        return not live_mode()


async def build_tenant_outlook_sender(
    session: AsyncSession,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    *,
    store: SecretStore | None = None,
) -> MailboxSender:
    """The sender for this tenant's mailbox traffic: THEIR token, freshly rotated.

    Outside live mode the hermetic Null sender is returned, so dev/CI never reaches
    the network even with material stored. In live mode a tenant whose token cannot
    be made fresh (no connection, no refresh token, refresh rejected) ALSO gets
    Null — ``null_in_live`` then refuses the send with ``mailbox_reconnect_needed``
    rather than letting it look delivered.
    """
    from app.config import get_settings  # noqa: PLC0415 — read the mode at call time

    if get_settings().integration_mode != "live":
        return NullOutlookSender()
    token = await ensure_fresh_token(session, admin_id, instance_id, store=store)
    if not token:
        return NullOutlookSender()
    return GraphMailSender(token)
