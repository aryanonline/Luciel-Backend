"""Contract-harness fixtures: an in-process ASGI client against the real app+DB."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def stripe_test_posture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the request in the "test" Stripe posture (round 7 WP-1 #5).

    In the default "mock" posture the deployment's billing is not live: checkout and
    the portal refuse, the webhook receiver ignores every event, and the billing read
    never reconciles against the gateway. A test that drives billing state through
    those routes therefore runs under the posture that has a real Stripe binding —
    with ``FakeStripeGateway`` injected by the test itself, so nothing is ever built
    against real keys and no network call is possible. A test that forgets to inject
    the fake fails loudly (the real gateway refuses to build without keys) rather than
    quietly passing against the wrong seam.
    """
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "stripe_integration_posture", "test")


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    # Import after conftest sets LUCIEL_DATABASE_URL to the test DB.
    from app.db.engine import dispose_engine
    from app.main import create_app

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await dispose_engine()
