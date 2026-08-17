"""Generic OAuth refresh — every provider, one mechanism (§3.8.5).

Before app/connections/refresh.py exactly one provider could be refreshed
(Outlook). These tests pin the generic refresher's grant shapes per style with
FAKE transports (no network), the rotation-durable-before-return rule, and the
dispatcher's live-gating. The DB-touching call-time helper
(ensure_fresh_access_token) is covered in tests/integration/test_connections.py.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.connections import refresh
from app.connections.secrets import LocalSecretStore
from app.models.connection import InstanceConnection

pytestmark = [pytest.mark.asyncio]


class _Response:
    def __init__(self, status_code: int = 200, payload: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeTransport:
    """Captures the exact grant the refresher sends; replies with a canned payload."""

    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code
        self.posts: list[tuple[str, dict[str, str]]] = []
        self.gets: list[tuple[str, dict[str, str]]] = []

    async def post(self, url: str, *, data: dict[str, str] | None = None, headers=None):  # noqa: ANN001
        self.posts.append((url, dict(data or {})))
        return _Response(self.status_code, self.payload)

    async def get(self, url: str, *, params: dict[str, str] | None = None):  # noqa: ANN001
        self.gets.append((url, dict(params or {})))
        return _Response(self.status_code, self.payload)


def _row(provider: str, secret_ref: str = "vm/t/i/x") -> InstanceConnection:
    return InstanceConnection(
        connection_id=uuid.uuid4(),
        admin_id=uuid.uuid4(),
        instance_id=uuid.uuid4(),
        connection_type="calendar",
        auth_class="oauth_token",
        provider=provider,
        status="connected",
        secret_ref=secret_ref,
    )


def _google_client(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import get_settings

    monkeypatch.setenv("LUCIEL_GOOGLE_CLIENT_ID", "g-cid")
    monkeypatch.setenv("LUCIEL_GOOGLE_CLIENT_SECRET", "g-csecret")
    monkeypatch.setenv("LUCIEL_GOOGLE_REDIRECT_URI", "https://api.example.com/cb")
    get_settings.cache_clear()  # type: ignore[attr-defined]


async def test_refresh_token_post_sends_the_standard_grant_and_persists(monkeypatch):
    _google_client(monkeypatch)
    store = LocalSecretStore()
    await store.put("vm/t/i/x", {"access_token": "old", "refresh_token": "rt-1"})
    transport = _FakeTransport({"access_token": "new", "expires_in": 3600})
    refresher = refresh.RegistryTokenRefresher(transport_factory=lambda: transport)

    expiry = await refresher.refresh(_row("google_calendar"), store)

    assert expiry is not None
    url, grant = transport.posts[0]
    assert url == "https://oauth2.googleapis.com/token"
    assert grant["grant_type"] == "refresh_token"
    assert grant["refresh_token"] == "rt-1"
    assert grant["client_id"] == "g-cid"
    assert "scope" not in grant  # Google must NOT receive a scope on refresh
    material = await store.get("vm/t/i/x")
    assert material["access_token"] == "new"
    # Google returns no rotated refresh token — the stored one must survive.
    assert material["refresh_token"] == "rt-1"


async def test_outlook_rides_the_generic_table_with_scope_and_rotation(monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("LUCIEL_MICROSOFT_CLIENT_ID", "ms-cid")
    monkeypatch.setenv("LUCIEL_MICROSOFT_CLIENT_SECRET", "ms-csecret")
    monkeypatch.setenv("LUCIEL_MICROSOFT_REDIRECT_URI", "https://api.example.com/cb")
    get_settings.cache_clear()  # type: ignore[attr-defined]
    store = LocalSecretStore()
    await store.put("vm/t/i/x", {"access_token": "old", "refresh_token": "rt-old"})
    transport = _FakeTransport(
        {"access_token": "new", "refresh_token": "rt-rotated", "expires_in": 3599}
    )
    refresher = refresh.RegistryTokenRefresher(transport_factory=lambda: transport)

    assert await refresher.refresh(_row("outlook"), store) is not None
    _, grant = transport.posts[0]
    # Microsoft rejects a scope-less refresh for some account types.
    assert grant["scope"] == "offline_access User.Read Mail.Read Mail.Send"
    material = await store.get("vm/t/i/x")
    # Rotation durable BEFORE return: the burnt token must be replaced.
    assert material["refresh_token"] == "rt-rotated"


async def test_instagram_long_lived_token_refreshes_itself_via_get():
    store = LocalSecretStore()
    await store.put("vm/t/i/x", {"access_token": "ig-old"})
    transport = _FakeTransport({"access_token": "ig-new", "expires_in": 5184000})
    refresher = refresh.RegistryTokenRefresher(transport_factory=lambda: transport)

    expiry = await refresher.refresh(_row("instagram"), store)

    assert expiry is not None and expiry > datetime.now(UTC) + timedelta(days=50)
    url, params = transport.gets[0]
    assert url == "https://graph.instagram.com/refresh_access_token"
    assert params == {"grant_type": "ig_refresh_token", "access_token": "ig-old"}
    assert (await store.get("vm/t/i/x"))["access_token"] == "ig-new"


async def test_meta_long_lived_token_re_exchanges_with_app_credentials(monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("LUCIEL_META_APP_ID", "meta-cid")
    monkeypatch.setenv("LUCIEL_META_APP_SECRET", "meta-csecret")
    monkeypatch.setenv("LUCIEL_META_REDIRECT_URI", "https://api.example.com/cb")
    get_settings.cache_clear()  # type: ignore[attr-defined]
    store = LocalSecretStore()
    await store.put("vm/t/i/x", {"access_token": "meta-old"})
    transport = _FakeTransport({"access_token": "meta-new", "expires_in": 5184000})
    refresher = refresh.RegistryTokenRefresher(transport_factory=lambda: transport)

    assert await refresher.refresh(_row("meta"), store) is not None
    url, params = transport.gets[0]
    assert url == "https://graph.facebook.com/v19.0/oauth/access_token"
    assert params["grant_type"] == "fb_exchange_token"
    assert params["fb_exchange_token"] == "meta-old"
    assert params["client_id"] == "meta-cid"


async def test_provider_rejection_returns_none_and_keeps_the_old_material(monkeypatch):
    _google_client(monkeypatch)
    store = LocalSecretStore()
    await store.put("vm/t/i/x", {"access_token": "old", "refresh_token": "rt-1"})
    transport = _FakeTransport({"error": "invalid_grant"}, status_code=400)
    refresher = refresh.RegistryTokenRefresher(transport_factory=lambda: transport)

    assert await refresher.refresh(_row("google_calendar"), store) is None
    assert (await store.get("vm/t/i/x"))["access_token"] == "old"


async def test_unspecced_provider_and_missing_refresh_token_return_none(monkeypatch):
    _google_client(monkeypatch)
    store = LocalSecretStore()
    await store.put("vm/t/i/x", {"access_token": "old"})  # no refresh_token
    refresher = refresh.RegistryTokenRefresher(
        transport_factory=lambda: _FakeTransport({"access_token": "never"})
    )
    assert await refresher.refresh(_row("notion"), store) is None  # no spec
    assert await refresher.refresh(_row("google_calendar"), store) is None  # no rt


async def test_dispatcher_is_live_gated_and_falls_back_in_mock(monkeypatch):
    """Mock must stay hermetic: even a specced provider goes to the fallback's
    honest None → expired path when not in live mode."""

    class _Fallback:
        def __init__(self) -> None:
            self.called = False

        async def refresh(self, connection, store):  # noqa: ANN001
            self.called = True
            return None

    store = LocalSecretStore()
    fallback = _Fallback()
    dispatcher = refresh.DispatchingRefresher(fallback=fallback)
    # Default test env is mock — the specced provider must NOT hit the network.
    assert await dispatcher.refresh(_row("google_calendar"), store) is None
    assert fallback.called is True


async def test_dispatcher_routes_specced_providers_to_the_real_refresher_in_live(monkeypatch):
    class _Fallback:
        async def refresh(self, connection, store):  # noqa: ANN001
            raise AssertionError("fallback must not be used for a specced provider in live")

    class _Registry:
        def __init__(self) -> None:
            self.seen: list[str] = []

        async def refresh(self, connection, store):  # noqa: ANN001
            self.seen.append(connection.provider)
            return datetime.now(UTC) + timedelta(hours=1)

    monkeypatch.setattr(refresh, "live_mode", lambda: True)
    registry_refresher = _Registry()
    dispatcher = refresh.DispatchingRefresher(
        fallback=_Fallback(), registry_refresher=registry_refresher  # type: ignore[arg-type]
    )
    assert await dispatcher.refresh(_row("hubspot"), LocalSecretStore()) is not None
    assert registry_refresher.seen == ["hubspot"]
