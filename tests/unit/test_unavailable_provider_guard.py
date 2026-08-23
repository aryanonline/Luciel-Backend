"""Honest-disable is enforced everywhere, not just the provider list (Phase 4).

``unavailable_for`` used to be consulted only by ``GET /connections/providers``:
``provider_available`` (the Overview chip truth) ignored it, and a direct POST
could still start a consent/credential flow for a provider whose effect layer
is unbuilt — a connected row with a dead tool behind it. Pinned here: calendly
(calendar) and live_connector (record_source) are honestly unavailable at every
layer, per-surface (Salesforce KB-read stays independently governed while its
CRM push is unbuilt).
"""

from __future__ import annotations

import pytest
from app.connections import registry


def test_unavailable_for_wins_in_provider_available() -> None:
    assert registry.provider_available("calendly") is False
    assert registry.provider_available("calendly", "calendar") is False
    assert registry.provider_available("live_connector") is False
    assert registry.provider_available("live_connector", "record_source") is False
    assert registry.provider_available("salesforce", "crm") is False
    # Non-OAuth providers with no unavailable surface stay available.
    assert registry.provider_available("csv") is True
    assert registry.provider_available("custom_webhook") is True


@pytest.mark.asyncio
async def test_start_connection_refuses_an_unavailable_surface() -> None:
    import uuid

    from app.connections.service import ProviderNotRegistered, start_connection
    from app.db.session import tenant_session

    admin = uuid.uuid4()
    async with tenant_session(admin) as s:
        with pytest.raises(ProviderNotRegistered):
            await start_connection(
                s,
                admin_id=admin,
                instance_id=uuid.uuid4(),
                connection_type="calendar",
                provider="calendly",
                user_id=admin,
            )
        with pytest.raises(ProviderNotRegistered):
            await start_connection(
                s,
                admin_id=admin,
                instance_id=uuid.uuid4(),
                connection_type="record_source",
                provider="live_connector",
                user_id=admin,
            )
