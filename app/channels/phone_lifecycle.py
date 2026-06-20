"""Phone number lifecycle (Architecture §3.1.4).

Phone is opt-in via channel selection. Enabling SMS (or Voice) provisions a
dedicated Twilio number from the pool and records it as an sms_sender connection
(non_secret_config.destination = the E.164 number). Disabling does NOT release the
number — the connection row + number are retained so re-enabling needs no re-setup
(§3.7.1 / §3.8.7 B). If SMS/Voice is not enabled, no number is provisioned and no
recurring cost accrues.

The Twilio provisioning call is abstracted behind an injected provisioner; real
binding in deployment.
"""

from __future__ import annotations

import uuid
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.connection import InstanceConnection


class NumberProvisioner(Protocol):
    async def provision_number(self, admin_id: uuid.UUID, instance_id: uuid.UUID) -> str:
        """Provision a dedicated number; return its E.164 string."""
        ...


async def ensure_sms_number(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    provisioner: NumberProvisioner,
) -> InstanceConnection:
    """Provision (idempotently) the tenant's SMS sender number connection.

    If a non-revoked sms_sender connection exists, reuse it (retained across
    disable). Otherwise provision a new number and create the connection row.
    """
    existing = (
        await session.execute(
            select(InstanceConnection).where(
                InstanceConnection.admin_id == admin_id,
                InstanceConnection.instance_id == instance_id,
                InstanceConnection.connection_type == "sms_sender",
                InstanceConnection.status != "revoked",
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    number = await provisioner.provision_number(admin_id, instance_id)
    conn = InstanceConnection(
        connection_id=uuid.uuid4(),
        admin_id=admin_id,
        instance_id=instance_id,
        connection_type="sms_sender",
        auth_class="provisioned_resource",
        provider="twilio",
        # The number exists immediately, but A2P 10DLC carrier registration is not yet
        # approved, so it cannot send to US recipients (§3.1.6 / Decision §46). Surface
        # an honest pending status — NOT "connected" — until the carrier approves; the
        # registration-status poller flips it to "connected" on approval.
        status="pending_carrier_registration",
        non_secret_config={"destination": number},
        status_detail=(
            "Being activated with the carriers (A2P 10DLC) — usually a few business days."
        ),
    )
    session.add(conn)
    await session.flush()
    return conn
