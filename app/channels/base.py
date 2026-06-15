"""Channel adapter contract + unified InboundMessage envelope (Architecture §3.1.1).

Every channel adapter normalizes a channel-specific payload into one envelope, so
the runtime is channel-agnostic by the time it reasons (§3.4.2). Adapters also
own inbound authentication: verify_inbound resolves the request to a tenant
(admin_id, instance_id) — and the inbound signature is verified BEFORE the tenant
lookup (§3.1.3, §3.7.2). An unsigned/invalid inbound is rejected + audit-logged,
never processed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass
class InboundMessage:
    admin_id: uuid.UUID
    instance_id: uuid.UUID
    channel: str  # widget|email|sms|voice|whatsapp|instagram_messenger
    session_id: uuid.UUID | None
    customer_identifier: str  # channel-specific (email, E.164, fingerprint, ...)
    body: str
    channel_metadata: dict[str, Any] = field(default_factory=dict)
    received_at: datetime | None = None


@dataclass
class OutboundMessage:
    channel: str
    to: str
    body: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class DeliveryReceipt:
    accepted: bool
    provider_message_id: str | None = None
    detail: str | None = None


@dataclass
class InstanceContext:
    admin_id: uuid.UUID
    instance_id: uuid.UUID


class InboundRejected(Exception):
    """Signature-invalid / unresolvable inbound — rejected, never routed (§3.1.3)."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class ChannelAdapter(Protocol):
    channel_id: str

    async def verify_inbound(self, request: dict[str, Any]) -> InstanceContext:
        """Authenticate the inbound request → resolve to (admin_id, instance_id).

        Raises InboundRejected on signature failure or unresolvable destination.
        """
        ...

    async def receive(self, request: dict[str, Any], ctx: InstanceContext) -> InboundMessage:
        """Normalize a channel-specific payload into the unified envelope."""
        ...

    async def send(self, message: OutboundMessage) -> DeliveryReceipt:
        """Send via the channel; return delivery confirmation."""
        ...
