"""Deferred-action fail-safe retry queue (Architecture §3.8.7 rule D).

The deliberate queue-vs-fail-fast split (§3.8.7 rule D):
  * Synchronous, in-conversation actions FAIL FAST — they are NOT queued (handled
    by the broker / agentic loop returning a structured error; see app/tools/broker.py).
  * Deferred / outbound actions ARE queued here with a bounded 24h max-age TTL,
    retried with exponential backoff when the delivery connection recovers, and
    abandoned-with-owner-notification + audit once past TTL — never silently dropped.

This module owns the queue mechanics: ``enqueue`` (called when a deferred action's
delivery connection is unhealthy at the moment of use) and ``run_sweep`` (the
one-shot worker pass that retries due items and abandons expired ones). On each
retry the delivery connection's health is re-checked via the universal gate
(§3.8.7 rule A.2 / rule D). Delivery itself is behind a pluggable ``Deliverer``
seam so tests drive it deterministically with no real provider call.

The real owner-notification push (escalation channel, §3.5) binds later; here
abandonment emits the audit event the spec mandates and the notification is recorded as
an audit row (flagged).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log as audit
from app.channels.outbound import send_outbound_reply
from app.connections import gate
from app.models.audit import ActorType
from app.models.deferred import DeferredAction
from app.models.lead import Lead

_log = logging.getLogger(__name__)

# §3.8.7 rule D: every queued item carries a 24h max-age TTL (configurable per
# action class; the platform default is 24h).
MAX_AGE = timedelta(hours=24)
# Exponential backoff base (§3.8.7 rule D / §3.8.6 discipline): delay = BASE * 2**attempts,
# capped at BACKOFF_CAP. Deterministic so the sweep is testable without wall-clock sleeps.
BACKOFF_BASE = timedelta(minutes=1)
BACKOFF_CAP = timedelta(hours=1)


class Deliverer(Protocol):
    """Attempts delivery of one queued action. Returns True on success.

    Deterministic test impls drive the queue without real provider calls; the real
    provider deliverer binds later behind this same seam (§3.8.7 rule D)."""

    async def deliver(self, action: DeferredAction) -> bool: ...


class ChannelDeliverer:
    """The scheduled sweep's ``Deliverer``: push the queued message on the lead's channel.

    Everything the queue holds today is a ``schedule_callback`` — the lead asked to be
    contacted later — so delivery is exactly the outbound reply the runtime would have
    sent live, through the same ``send_outbound_reply`` that owns each channel's quirks
    and each tenant's own credentials.

    It returns the wire result rather than True-on-attempt. ``send_outbound_reply``
    reports an unprovisioned channel or a refused send as ``delivered=False`` instead of
    raising, and a Deliverer that treated "we tried" as success would mark the row
    delivered and drop the lead's callback silently — precisely the outcome rule D
    exists to prevent. False keeps the item queued for its backoff, and if the channel
    never comes back the TTL abandons it loudly.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def deliver(self, action: DeferredAction) -> bool:
        payload = action.payload or {}
        channel = payload.get("channel")
        body = payload.get("message")
        if not isinstance(channel, str) or not isinstance(body, str) or not body:
            _log.warning("deferred action %s has no deliverable payload", action.id)
            return False

        to = await self._recipient(action)
        if not to:
            # The lead never left a reachable identifier on this channel. Retrying will
            # not conjure one, but abandoning early would discard the record; the TTL
            # handles it and the audit row on abandon says why.
            return False

        result = await send_outbound_reply(
            self._session,
            admin_id=action.admin_id,
            instance_id=action.instance_id,
            channel=channel,
            to=to,
            body=body,
        )
        return result.delivered

    async def _recipient(self, action: DeferredAction) -> str | None:
        if action.resolved_lead_id is None:
            return None
        lead = (
            await self._session.execute(
                select(Lead).where(
                    Lead.admin_id == action.admin_id, Lead.id == action.resolved_lead_id
                )
            )
        ).scalar_one_or_none()
        return lead.contact_identifier if lead is not None else None


def _backoff(attempts: int) -> timedelta:
    delay: timedelta = BACKOFF_BASE * (2**attempts)
    return min(delay, BACKOFF_CAP)


def _not_before(payload: dict[str, object], now: datetime) -> datetime:
    """The earliest moment this item may be attempted.

    A ``schedule_callback`` carries the time the lead asked to be contacted. While the
    queue had no scheduled worker this was inert — nothing ever read the row — but the
    moment a sweep runs on a cadence, a first attempt at ``now`` means every callback
    fires the instant it is booked and then expires against a TTL measured from booking
    rather than from the promised time. So the wait is expressed where the queue can see
    it: the item is simply not due until then.

    An unparseable value falls back to ``now``. Delivering a callback early is a smaller
    failure than holding it forever on a timestamp the model phrased loosely.
    """
    raw = payload.get("fire_at")
    if not isinstance(raw, str):
        return now
    try:
        fire_at = datetime.fromisoformat(raw)
    except ValueError:
        return now
    if fire_at.tzinfo is None:
        fire_at = fire_at.replace(tzinfo=UTC)
    return max(now, fire_at)


async def enqueue(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    action_type: str,
    connection_type: str,
    payload: dict[str, object],
    resolved_lead_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> DeferredAction:
    """Queue a deferred action whose delivery connection was unhealthy (§3.8.7 D).

    The item is eligible for its first attempt as soon as it is due — immediately for an
    action with no promised time, at ``payload['fire_at']`` for one that has it — and the
    24h TTL runs from that moment rather than from creation, so a callback booked for
    Friday gets its full retry window on Friday. It is NEVER dropped — that is the whole
    point of the rule-D queue. ``resolved_lead_id`` (when known) is stored so
    schedule_callback can count pending callbacks per lead (§3.3.5)."""
    now = now or datetime.now(UTC)
    due_at = _not_before(payload, now)
    action = DeferredAction(
        id=uuid.uuid4(),
        admin_id=admin_id,
        instance_id=instance_id,
        action_type=action_type,
        connection_type=connection_type,
        resolved_lead_id=resolved_lead_id,
        payload=payload,
        status="queued",
        attempts=0,
        expires_at=due_at + MAX_AGE,
        next_attempt_at=due_at,
    )
    session.add(action)
    await session.flush()
    return action


async def run_sweep(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    deliverer: Deliverer,
    now: datetime | None = None,
) -> dict[str, int]:
    """One-shot worker pass over due queued items for this tenant (§3.8.7 rule D).

    For each queued item whose next_attempt_at <= now:
      * if past its 24h TTL → abandon: status='abandoned', notify owner + audit
        (``connection_status_changed`` carrying the abandon reason — never silently
        dropped, §3.8.7 rule D);
      * else re-check the delivery connection health via the universal gate
        (§3.8.7 A.2); if connected, attempt delivery via the pluggable Deliverer —
        success → status='delivered'; failure → bump attempts + backoff;
      * if the connection is still unhealthy → bump attempts + backoff (do not
        deliver blind, §3.8.7 rule C).

    Returns counts for observability/tests. Invocable directly as a one-shot sweep —
    a real background loop simply calls this on the worker cadence (no loop in CI)."""
    now = now or datetime.now(UTC)
    counts = {"delivered": 0, "abandoned": 0, "retried": 0}

    due = (
        (
            await session.execute(
                select(DeferredAction).where(
                    DeferredAction.admin_id == admin_id,
                    DeferredAction.status == "queued",
                    DeferredAction.next_attempt_at <= now,
                )
            )
        )
        .scalars()
        .all()
    )

    for action in due:
        # §3.3.5: an opted-out lead's queued schedule_callback is refused/skipped at
        # delivery time regardless of caps or TTL — checked before the TTL/abandon
        # branch so an opted-out callback is never delivered even at its last retry.
        # Only schedule_callback carries this rule (other deferred action types have
        # no lead opt-out semantics); resolved_lead_id absent (anonymous) skips it.
        if action.action_type == "schedule_callback" and action.resolved_lead_id is not None:
            lead = (
                await session.execute(
                    select(Lead).where(
                        Lead.admin_id == admin_id,
                        Lead.id == action.resolved_lead_id,
                    )
                )
            ).scalar_one_or_none()
            if lead is not None and lead.opted_out:
                action.status = "abandoned"
                action.last_error = "opted_out"
                await audit.emit(
                    session,
                    admin_id=action.admin_id,
                    instance_id=action.instance_id,
                    actor_type=ActorType.platform,
                    event_type="connection_status_changed",
                    payload={
                        "action": "deferred_abandoned",
                        "deferred_action_id": str(action.id),
                        "action_type": action.action_type,
                        "connection_type": action.connection_type,
                        "reason": "lead_opted_out",
                    },
                )
                counts["abandoned"] += 1
                continue

        # TTL check FIRST: a customer's follow-up that has been undeliverable > 24h is
        # abandoned with an owner notification, not retried forever (§3.8.7 rule D).
        if now >= action.expires_at:
            action.status = "abandoned"
            action.last_error = "ttl_exceeded"
            await _notify_owner_abandoned(session, action)
            counts["abandoned"] += 1
            continue

        healthy = await gate.connection_healthy(
            session,
            admin_id=admin_id,
            instance_id=action.instance_id,
            connection_type=action.connection_type,
        )
        if healthy:
            delivered = await deliverer.deliver(action)
            if delivered:
                action.status = "delivered"
                counts["delivered"] += 1
                continue
            action.last_error = "delivery_failed"
        else:
            # Connection still down — re-checked health, did not call blind (§3.8.7 C).
            action.last_error = "connection_unhealthy"

        action.attempts += 1
        action.next_attempt_at = now + _backoff(action.attempts)
        counts["retried"] += 1

    await session.flush()
    return counts


async def _notify_owner_abandoned(session: AsyncSession, action: DeferredAction) -> None:
    """On TTL-abandon, notify the owner + write the audit row (§3.8.7 rule D).

    The audit event is the durable, tamper-evident record the spec mandates
    ("with the event written to admin_audit_log"). The owner-facing PUSH to the
    escalation channel (§3.5) binds when notifications lands its real adapters —
    flagged. The payload carries NO secret material (§3.8.3 rule 4)."""
    await audit.emit(
        session,
        admin_id=action.admin_id,
        instance_id=action.instance_id,
        actor_type=ActorType.platform,
        event_type="connection_status_changed",
        payload={
            "action": "deferred_abandoned",
            "deferred_action_id": str(action.id),
            "action_type": action.action_type,
            "connection_type": action.connection_type,
            "reason": "undeliverable > 24h TTL",
        },
    )
