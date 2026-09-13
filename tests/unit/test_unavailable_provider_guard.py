"""Honest-disable is enforced everywhere, not just the provider list (Phase 4).

``unavailable_for`` used to be consulted only by ``GET /connections/providers``:
``provider_available`` (the Overview chip truth) ignored it, and a direct POST
could still start a consent/credential flow for a provider whose effect layer
is unbuilt — a connected row with a dead tool behind it. Pinned here: calendly
(calendar) and live_connector (record_source) are honestly unavailable at every
layer. (Salesforce left this list in Phase 6.5, when its CRM push became real.)
"""

from __future__ import annotations

import pytest
from app.connections import registry


def test_availability_follows_the_effect_layer_not_a_hardcoded_list() -> None:
    # Round 6 WP-H (F055 / F062): Calendly and the live record connector are BUILT, so
    # no provider is 'unavailable_for' any surface any more. What still gates a provider
    # is real: an OAuth provider needs its platform client registered (Calendly has none
    # in this environment), a credential-form provider needs nothing from the platform.
    for provider in ("calendly", "live_connector", "csv", "custom_webhook"):
        assert registry.get_provider(provider).unavailable_for == frozenset(), provider
    assert registry.provider_available("calendly") is False  # no OAuth client registered here
    assert registry.provider_available("calendly", "calendar") is False
    assert registry.provider_available("live_connector") is True
    assert registry.provider_available("live_connector", "record_source") is True
    assert registry.provider_available("csv") is True
    assert registry.provider_available("custom_webhook") is True


def test_unavailable_for_still_wins_when_a_spec_declares_it(monkeypatch) -> None:
    """The guard itself is intact for any future provider whose effect layer is unbuilt
    on one surface: ``unavailable_for`` beats everything else in provider_available."""
    from dataclasses import replace

    spec = registry.get_provider("custom_webhook")
    assert spec is not None
    gated = replace(spec, unavailable_for=frozenset({spec.connection_type}))
    original = registry.get_provider
    monkeypatch.setattr(
        registry, "get_provider", lambda p: gated if p == "custom_webhook" else original(p)
    )
    assert registry.provider_available("custom_webhook") is False
    assert registry.provider_available("csv") is True


@pytest.mark.asyncio
async def test_start_connection_offers_the_built_providers_and_still_refuses_an_unknown_one() -> (
    None
):
    """Round 6 WP-H: the live record connector opens its credential form and Calendly
    starts like any other OAuth provider (an unregistered client is the 'Action needed'
    detail, not a refusal — same as Google Calendar); a name the registry does not offer
    for the surface is still refused before any row is written."""
    import uuid

    from app.connections.service import ProviderNotRegistered, start_connection
    from app.db.session import tenant_session

    admin = uuid.uuid4()
    async with tenant_session(admin) as s:
        started = await start_connection(
            s,
            admin_id=admin,
            instance_id=uuid.uuid4(),
            connection_type="record_source",
            provider="live_connector",
            user_id=admin,
        )
        assert started is not None
        with pytest.raises(ProviderNotRegistered):
            await start_connection(
                s,
                admin_id=admin,
                instance_id=uuid.uuid4(),
                connection_type="record_source",
                provider="fax_machine",
                user_id=admin,
            )
