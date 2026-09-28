"""Inbound BYO-mailbox mail via Graph delta polling (§3.1.6a / §3.1.3).

Pull, not push, on purpose. Graph change notifications would need a new public
unauthenticated endpoint (validationToken handshake, clientState secret, ~3-day
subscription renewal) — three fresh failure modes for a channel whose session window
is 24 hours and whose users do not expect instant replies. A 5-minute delta poll
through the tenant's own authenticated token has none of that, and stays inside the
sweeps framework (advisory-locked, per-tenant transaction, at-least-once).

The polled message flows through the SAME admission pipeline as webhook ingress
(``inbound_dispatch``): rate limiting, opt-out handling, runtime, honest outbound
reply — threaded under the customer's own message via the Graph id.

THE BASELINE RULE (the highest-severity risk in this module): the FIRST poll after a
mailbox connects drains the entire delta chain WITHOUT processing anything. The
customer's historical inbox is not conversation backlog — answering year-old mail on
connect would be the worst possible first impression. Belt-and-braces, messages
received before the baseline stamp are skipped forever.

At-least-once caveat: progress (delta link + seen ids) persists when the sweep's
per-tenant transaction commits. A crash between an outbound reply and that commit can
replay a message on the next run; the capped ``mailbox_seen_ids`` list absorbs the
common case.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels import inbound_dispatch
from app.channels.base import ChannelAdapter, InboundMessage, InstanceContext
from app.channels.tenant_outlook import ensure_fresh_token
from app.models.connection import QUIESCING, InstanceConnection

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
DELTA_START = (
    f"{GRAPH_BASE}/me/mailFolders/inbox/messages/delta"
    "?$select=id,subject,from,receivedDateTime,internetMessageId,body,bodyPreview,isDraft"
)
# Bound one run's work so a Fargate sweep task cannot run away on a busy mailbox; an
# unfinished page chain resumes from the stored link on the next run (5 min later).
MAX_MESSAGES_PER_RUN = 25
# Crash-replay dedupe window (see the at-least-once caveat in the module docstring).
SEEN_IDS_KEPT = 50

_CFG_DELTA_LINK = "mailbox_delta_link"
_CFG_SEEN_IDS = "mailbox_seen_ids"
_CFG_POLLING_SINCE = "mailbox_polling_since"


class PulledMailboxAdapter:
    """ChannelAdapter for mail WE fetched (vs. mail pushed at us).

    ``verify_inbound`` returns the already-known tenant context: authenticity here is
    the tenant's own authenticated Graph session — there is no third-party signature
    to verify, and inventing one would be theater. Everything else (admission, rate
    limits, opt-out, normalization) runs exactly as for pushed ingress.
    """

    channel_id = "email"

    def __init__(self, ctx: InstanceContext) -> None:
        self._ctx = ctx

    async def verify_inbound(self, request: dict[str, Any]) -> InstanceContext:
        return self._ctx

    async def receive(self, request: dict[str, Any], ctx: InstanceContext) -> InboundMessage:
        return InboundMessage(
            admin_id=ctx.admin_id,
            instance_id=ctx.instance_id,
            channel="email",
            session_id=None,
            customer_identifier=request["from_address"],
            body=request.get("text", ""),
            channel_metadata={
                "subject": request.get("subject"),
                # The reply path uses this for true threading (/messages/{id}/reply).
                "graph_message_id": request.get("graph_message_id"),
                "internet_message_id": request.get("internet_message_id"),
            },
            received_at=datetime.now(UTC),
        )

    async def send(self, message: Any) -> Any:  # pragma: no cover — replies go via outbound
        raise NotImplementedError("polled-mailbox replies are sent through channels.outbound")


def _plain_text(message: dict[str, Any]) -> str:
    """The best plain-text body Graph gives us without an HTML pipeline."""
    body = message.get("body") or {}
    if str(body.get("contentType", "")).lower() == "text":
        return str(body.get("content", ""))
    # HTML bodies: bodyPreview is Graph's own plain-text rendering (truncated ~255
    # chars). Truncated-but-honest beats parsing HTML badly.
    return str(message.get("bodyPreview", ""))


async def _fetch_page(transport: Any, url: str, token: str) -> dict[str, Any] | None:
    try:
        response = await transport.get(url, headers={"Authorization": f"Bearer {token}"})
    except Exception as exc:  # noqa: BLE001 — a transport error ends this run, not the row
        logger.warning("mailbox_poll_transport_error error=%s", type(exc).__name__)
        return None
    if getattr(response, "status_code", None) != 200:
        logger.warning("mailbox_poll_rejected status=%s", getattr(response, "status_code", "?"))
        return None
    result: dict[str, Any] = response.json()
    return result


def _default_transport() -> Any:
    import httpx  # noqa: PLC0415 — live-path-only import

    return httpx.AsyncClient(timeout=15.0)


async def poll_mailbox(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    now: datetime | None = None,
    transport: Any = None,
    store: Any = None,
) -> int:
    """Poll this tenant's connected Outlook inbox once; return messages processed.

    Runs as per-tenant sweep work (advisory-locked). Progress is carried in
    non_secret_config: the delta link (or mid-chain resume link), the baseline stamp,
    and the capped seen-ids list.
    """
    now = now or datetime.now(UTC)
    # A QUIESCING mailbox (a Luciel in its delete drain, round 7 WP-5) is still read —
    # judged by the status it had before the delete. The poll IS the email channel's
    # inbound door: without this, a customer mid-thread at the delete instant never
    # got their follow-up seen, let alone answered. The runtime drain gate downstream
    # admits only the in-flight conversation; new threads are refused there.
    from app.lifecycle.state_machine import serving_status  # noqa: PLC0415 — cycle

    conn = (
        await session.execute(
            select(InstanceConnection).where(
                InstanceConnection.admin_id == admin_id,
                InstanceConnection.connection_type == "email_sender",
                InstanceConnection.provider == "outlook",
                InstanceConnection.status.in_(("connected", QUIESCING)),
            )
        )
    ).scalar_one_or_none()
    if conn is None or serving_status(conn) != "connected":
        return 0

    token = await ensure_fresh_token(
        session, admin_id, conn.instance_id, store=store, transport=transport, now=now
    )
    if token is None:
        # The health sweep owns the expired transition + audit; this run just
        # reports honestly that it could not look.
        logger.warning("mailbox_poll_no_fresh_token admin=%s", admin_id)
        return 0

    cfg = dict(conn.non_secret_config or {})
    own_address = str(cfg.get("address", "")).lower()
    stored_link = cfg.get(_CFG_DELTA_LINK)
    baseline_drain = not isinstance(stored_link, str) or not stored_link
    url: str = stored_link if not baseline_drain else DELTA_START  # type: ignore[assignment]

    poll_transport = transport or _default_transport()
    seen_raw = cfg.get(_CFG_SEEN_IDS)
    seen: list[str] = [str(x) for x in seen_raw] if isinstance(seen_raw, list) else []
    processed = 0
    ctx = InstanceContext(admin_id=admin_id, instance_id=conn.instance_id)
    adapter: ChannelAdapter = PulledMailboxAdapter(ctx)

    while True:
        page = await _fetch_page(poll_transport, url, token)
        if page is None:
            # Keep the stored link unchanged: this run learned nothing, the next one
            # retries from the same point. Never half-advance on an error.
            return processed
        if not baseline_drain:
            for message in page.get("value", []):
                if processed >= MAX_MESSAGES_PER_RUN:
                    # Bound the run; resume THIS page chain next time.
                    cfg[_CFG_DELTA_LINK] = url
                    cfg[_CFG_SEEN_IDS] = seen[-SEEN_IDS_KEPT:]
                    conn.non_secret_config = cfg
                    return processed
                handled = await _handle_message(
                    adapter, message, own_address=own_address, seen=seen, cfg=cfg
                )
                processed += handled
        next_link = page.get("@odata.nextLink")
        delta_link = page.get("@odata.deltaLink")
        if next_link:
            url = str(next_link)
            continue
        if delta_link:
            cfg[_CFG_DELTA_LINK] = str(delta_link)
            if baseline_drain:
                # First run: the whole historical inbox was drained UNPROCESSED and
                # this stamp is the floor no older message may ever cross.
                cfg[_CFG_POLLING_SINCE] = now.isoformat()
                logger.info("mailbox_poll_baseline_established admin=%s", admin_id)
        cfg[_CFG_SEEN_IDS] = seen[-SEEN_IDS_KEPT:]
        conn.non_secret_config = cfg
        return processed


async def _handle_message(
    adapter: ChannelAdapter,
    message: dict[str, Any],
    *,
    own_address: str,
    seen: list[str],
    cfg: dict[str, Any],
) -> int:
    """Admit one Graph message into the shared pipeline. Returns 1 if processed."""
    graph_id = str(message.get("id", ""))
    if not graph_id or graph_id in seen:
        return 0
    if message.get("isDraft"):
        return 0
    sender = (
        str(((message.get("from") or {}).get("emailAddress") or {}).get("address", ""))
        .strip()
        .lower()
    )
    if not sender or sender == own_address:
        # Own sent mail shows up in delta too; answering yourself is a loop.
        return 0
    since_raw = cfg.get(_CFG_POLLING_SINCE)
    received_raw = str(message.get("receivedDateTime", ""))
    if isinstance(since_raw, str) and since_raw and received_raw:
        try:
            received = datetime.fromisoformat(received_raw.replace("Z", "+00:00"))
            since = datetime.fromisoformat(since_raw)
            if received < since:
                # Belt-and-braces under the baseline rule: historical mail is never
                # conversation backlog, even if a delta replay resurfaces it.
                return 0
        except ValueError:
            pass

    seen.append(graph_id)
    request: dict[str, Any] = {
        "from_address": sender,
        "subject": message.get("subject"),
        "text": _plain_text(message),
        "graph_message_id": graph_id,
        "internet_message_id": message.get("internetMessageId"),
    }
    # dispatch_inbound owns rejection/admission handling (audited, never raised);
    # a rate-limited or opted-out message still counts as handled for this run.
    await inbound_dispatch.dispatch_inbound(adapter, request)
    return 1
