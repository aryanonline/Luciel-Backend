"""Business Login for Instagram — the second Meta grant (§3.8.1, Decision #7).

Instagram DMs do NOT ride the Facebook OAuth dialog. On dev with the real app, Facebook
rejected the whole authorize request with "Invalid Scopes: instagram_basic,
instagram_manage_messages": under Meta's use-case app model, Instagram messaging lives in
a SEPARATE app ("Instagram API with Business Login for Instagram") with its own client id,
its own consent host, its own token endpoint and its own Graph host.

So it is its own registry client and its own connection type. Two properties this file
pins that nothing else can:

  * the Facebook client must never request an ``instagram_*`` scope again — that is not a
    degraded grant, it is a hard rejection of Meta connect for every owner, and
  * Instagram's token endpoint hands back a SHORT-lived token that expires in about an
    hour. Storing it would produce a connection that reports "connected" and stops working
    the same afternoon, so the code-for-token exchange has to be followed by the
    ``ig_exchange_token`` upgrade before anything is persisted.
"""

from __future__ import annotations

import uuid
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]

CALLBACK = "/api/v1/connections/instagram/callback"
RETURN_URL = "http://127.0.0.1:3000/dashboard/configure"
SHORT_LIVED = "ig-short-tok"  # noqa: S105 — test fixture, not a real secret
LONG_LIVED = "ig-long-tok"  # noqa: S105 — test fixture, not a real secret
IG_USER_ID = "17841400000000000"


async def _ready_account_with_luciel(client) -> None:
    from app.auth import access

    email = f"ig-{uuid.uuid4().hex[:8]}@x.com"
    await client.post(
        "/api/v1/auth/signup", json={"email": email, "password": "pw-123456", "captchaToken": "t"}
    )
    _a, token = await access.signup(email, "pw-123456")
    await client.post("/api/v1/auth/verify-email", json={"token": token})
    await client.post("/api/v1/auth/login", json={"email": email, "password": "pw-123456"})
    await client.post(
        "/api/v1/admin/luciel", json={"name": "L", "websiteUrl": "x.com", "businessOneLiner": "b"}
    )


def _admin_id(client) -> uuid.UUID:
    from app.auth.tokens import decode_token
    from app.config import get_settings

    return uuid.UUID(decode_token(client.cookies.get(get_settings().session_cookie_name))["sub"])


async def _instance_id(admin_id: uuid.UUID) -> uuid.UUID:
    from app.db.session import tenant_session
    from app.models.instance import Instance
    from sqlalchemy import select

    async with tenant_session(admin_id) as s:
        return (
            await s.execute(select(Instance.id).where(Instance.admin_id == admin_id))
        ).scalar_one()


def _configure_instagram(monkeypatch) -> None:
    """Both Meta apps provisioned, seen by the registry AND the exchange.

    They are configured together on purpose: the point of the split is that they coexist,
    and a test that only ever sees one app cannot catch the two crossing wires."""
    from app.config import Settings
    from app.connections import registry as registry_module
    from app.knowledge import providers as providers_module

    settings = Settings(
        meta_app_id="meta-cid",
        meta_app_secret="meta-csecret",  # noqa: S106 — test fixture, not a real secret
        meta_redirect_uri="https://api.example.com/api/v1/connections/meta/callback",
        instagram_app_id="ig-cid",
        instagram_app_secret="ig-csecret",  # noqa: S106 — test fixture, not a real secret
        instagram_redirect_uri="https://api.example.com/api/v1/connections/instagram/callback",
    )
    monkeypatch.setattr(registry_module, "get_settings", lambda: settings)
    monkeypatch.setattr(providers_module, "get_settings", lambda: settings)


def _fake_instagram_token_server(monkeypatch, *, long_lived_status: int = 200):
    """Stand in for api.instagram.com + graph.instagram.com, recording both legs.

    Both hosts are asserted, because the whole reason this client exists is that an
    Instagram Login token is issued by, and only accepted at, Instagram's own endpoints.
    """
    import app.api.v1.admin.connections as connections_api

    class FakeTokenServer:
        def __init__(self) -> None:
            self.posts: list[tuple[str, dict]] = []
            self.gets: list[tuple[str, dict]] = []

        async def post(self, url, *, data=None, headers=None):
            self.posts.append((url, dict(data or {})))
            assert url == "https://api.instagram.com/oauth/access_token"
            assert data["grant_type"] == "authorization_code"
            assert data["client_id"] == "ig-cid"
            # Instagram's short-lived response carries the account id and NO refresh token.
            return httpx.Response(
                200,
                json={"access_token": SHORT_LIVED, "user_id": IG_USER_ID, "permissions": []},
                request=httpx.Request("POST", url),
            )

        async def get(self, url, *, params=None, headers=None):
            self.gets.append((url, dict(params or {})))
            if url.startswith("https://graph.instagram.com/v23.0/me"):
                # Round 6 WP-C: the verifier proves the LONG-lived token against
                # graph.instagram.com/me and records the @username.
                assert (headers or {}).get("Authorization") == f"Bearer {LONG_LIVED}"
                return httpx.Response(
                    200,
                    json={"id": IG_USER_ID, "username": "acme"},
                    request=httpx.Request("GET", url),
                )
            assert url == "https://graph.instagram.com/access_token"
            if long_lived_status >= 400:
                return httpx.Response(
                    long_lived_status,
                    json={"error": {"message": "Invalid access token"}},
                    request=httpx.Request("GET", url),
                )
            return httpx.Response(
                200,
                json={"access_token": LONG_LIVED, "token_type": "bearer", "expires_in": 5184000},
                request=httpx.Request("GET", url),
            )

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    server = FakeTokenServer()
    monkeypatch.setattr(connections_api, "oauth_transport", lambda: server)
    return server


async def _start_instagram_connect(client) -> tuple[str, str, str]:
    """Begin the Instagram connect; return (authorize_url, state, connection_id)."""
    rs = await client.post(
        "/api/v1/admin/connections",
        json={"connectionType": "instagram_auth", "provider": "instagram"},
    )
    assert rs.status_code == 200, rs.text
    authorize_url = rs.json()["authorizeUrl"]
    assert authorize_url, rs.json()
    state = parse_qs(urlparse(authorize_url).query)["state"][0]
    listed = (await client.get("/api/v1/admin/connections")).json()
    cid = next(c["connectionId"] for c in listed if c["connectionType"] == "instagram_auth")
    return authorize_url, state, cid


def _redirect_params(response) -> dict[str, str]:
    assert response.status_code in (302, 303), f"{response.status_code}: {response.text[:200]}"
    location = response.headers["location"]
    assert location.startswith(f"{RETURN_URL}?"), location
    return {k: v[0] for k, v in parse_qs(urlparse(location).query).items()}


async def _status_of(client, connection_id: str) -> str:
    listed = (await client.get("/api/v1/admin/connections")).json()
    return next(c["status"] for c in listed if c["connectionId"] == connection_id)


# --------------------------------------------------------------------------- #
# The authorize URL
# --------------------------------------------------------------------------- #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_authorize_url_is_instagrams_own_dialog(client, monkeypatch):
    """instagram.com, the Instagram app id, and only Instagram business scopes.

    Sending the owner to facebook.com with these scopes is the live failure this replaces;
    sending them to instagram.com with the FACEBOOK app id fails the same way one step
    later, so the client id is pinned too."""
    _configure_instagram(monkeypatch)
    await _ready_account_with_luciel(client)

    authorize_url, _state, _cid = await _start_instagram_connect(client)

    parsed = urlparse(authorize_url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == (
        "https://www.instagram.com/oauth/authorize"
    )
    q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    assert q["client_id"] == "ig-cid"
    assert q["response_type"] == "code"
    assert q["redirect_uri"] == ("https://api.example.com/api/v1/connections/instagram/callback")
    assert set(q["scope"].split(",")) == {
        "instagram_business_basic",
        "instagram_business_manage_messages",
    }
    # §3.8.3: the app SECRET is never part of a URL the browser follows.
    assert "ig-csecret" not in authorize_url


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_two_meta_apps_get_their_own_callback_paths(client, monkeypatch):
    """The redirect path segment is the OAuth CLIENT label, and the two apps must not
    collapse onto one. A state minted for Instagram arriving on Meta's path is refused as
    a mismatch — which is also what proves the labels are distinct."""
    from app.connections.registry import oauth_client_label

    assert oauth_client_label("meta") == "meta"
    assert oauth_client_label("instagram") == "instagram"

    _configure_instagram(monkeypatch)
    _fake_instagram_token_server(monkeypatch)
    await _ready_account_with_luciel(client)
    _url, state, _cid = await _start_instagram_connect(client)

    r = await client.get("/api/v1/connections/meta/callback", params={"code": "c", "state": state})
    assert _redirect_params(r)["reason"] == "provider_mismatch"


# --------------------------------------------------------------------------- #
# The callback: short-lived code exchange + mandatory long-lived upgrade
# --------------------------------------------------------------------------- #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_callback_upgrades_to_a_long_lived_token_before_storing_it(client, monkeypatch):
    """The whole round trip. What is stored must be the LONG-lived token: the short-lived
    one expires in about an hour, so persisting it would give the owner a connection that
    says "connected" and silently stops answering DMs the same day."""
    from app.connections.secrets import get_secret_store, secret_ref_for

    _configure_instagram(monkeypatch)
    server = _fake_instagram_token_server(monkeypatch)
    await _ready_account_with_luciel(client)
    admin_id = _admin_id(client)
    instance_id = await _instance_id(admin_id)
    _url, state, cid = await _start_instagram_connect(client)
    assert await _status_of(client, cid) == "unconfigured"

    r = await client.get(CALLBACK, params={"code": "ig-code-1", "state": state})

    params = _redirect_params(r)
    assert params["status"] == "connected"
    assert params["provider"] == "instagram"
    assert params["connectionId"] == cid
    assert await _status_of(client, cid) == "connected"

    assert len(server.posts) == 1
    upgrades = [url for url, _ in server.gets if url.endswith("/access_token")]
    assert len(upgrades) == 1, "the long-lived upgrade is not optional"
    assert server.gets[0][1] == {
        "grant_type": "ig_exchange_token",
        "client_secret": "ig-csecret",
        "access_token": SHORT_LIVED,
    }

    stored = await get_secret_store().get(secret_ref_for(admin_id, instance_id, "instagram_auth"))
    assert stored is not None, "the callback must persist the token for the Instagram grant"
    assert stored["access_token"] == LONG_LIVED
    assert SHORT_LIVED not in str(stored), "the hour-long token must not be kept"
    # The account id comes back on the token response and is needed to address the Graph
    # send, so it is kept alongside the token rather than thrown away.
    assert stored["user_id"] == IG_USER_ID
    # §3.8.3: no secret material in the redirect the browser follows.
    assert LONG_LIVED not in r.headers["location"]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_failed_long_lived_upgrade_does_not_report_connected(client, monkeypatch):
    """Falling back to the short-lived token here would be the worst outcome: a green tile
    now and a dead integration in an hour, with nothing on the row to explain it."""
    from app.connections.secrets import get_secret_store, secret_ref_for

    _configure_instagram(monkeypatch)
    _fake_instagram_token_server(monkeypatch, long_lived_status=400)
    await _ready_account_with_luciel(client)
    admin_id = _admin_id(client)
    instance_id = await _instance_id(admin_id)
    _url, state, cid = await _start_instagram_connect(client)

    r = await client.get(CALLBACK, params={"code": "ig-code-2", "state": state})

    assert _redirect_params(r)["reason"] == "exchange_failed"
    assert await _status_of(client, cid) != "connected"
    assert (
        await get_secret_store().get(secret_ref_for(admin_id, instance_id, "instagram_auth"))
        is None
    )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_connecting_instagram_leaves_the_facebook_grant_alone(client, monkeypatch):
    """Separate connection types, separate secret refs. If they shared one, the second
    connect would overwrite the first tenant token — the eviction defect §3.8.1 closed."""
    from app.connections.secrets import get_secret_store, secret_ref_for

    _configure_instagram(monkeypatch)
    await _ready_account_with_luciel(client)
    admin_id = _admin_id(client)
    instance_id = await _instance_id(admin_id)

    import app.api.v1.admin.connections as connections_api

    class _FacebookTokenServer:
        async def post(self, url, *, data=None, headers=None):
            assert url == "https://graph.facebook.com/v19.0/oauth/access_token"
            return httpx.Response(
                200,
                json={"access_token": "fb-tok", "expires_in": 5184000},
                request=httpx.Request("POST", url),
            )

        async def get(self, url, *, params=None, headers=None):
            if url.startswith("https://graph.facebook.com/v19.0/me"):
                # Round 6 WP-C: Graph /me with the long-lived token names who authorized.
                assert (headers or {}).get("Authorization") == "Bearer fb-tok-long"
                return httpx.Response(
                    200,
                    json={"id": "10001", "name": "FB Tester"},
                    request=httpx.Request("GET", url),
                )
            # Facebook's own long-lived upgrade (round 5B item 14) — same endpoint, GET.
            assert url == "https://graph.facebook.com/v19.0/oauth/access_token"
            assert params["grant_type"] == "fb_exchange_token"
            assert params["fb_exchange_token"] == "fb-tok"
            return httpx.Response(
                200,
                json={"access_token": "fb-tok-long", "expires_in": 5184000},
                request=httpx.Request("GET", url),
            )

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(connections_api, "oauth_transport", _FacebookTokenServer)
    rs = await client.post(
        "/api/v1/admin/connections", json={"connectionType": "channel_auth", "provider": "meta"}
    )
    meta_state = parse_qs(urlparse(rs.json()["authorizeUrl"]).query)["state"][0]
    assert (
        await client.get(
            "/api/v1/connections/meta/callback", params={"code": "fb-code", "state": meta_state}
        )
    ).status_code in (302, 303)

    _fake_instagram_token_server(monkeypatch)
    _url, state, _cid = await _start_instagram_connect(client)
    assert (
        await client.get(CALLBACK, params={"code": "ig-code-3", "state": state})
    ).status_code in (302, 303)

    store = get_secret_store()
    facebook = await store.get(secret_ref_for(admin_id, instance_id, "channel_auth"))
    instagram = await store.get(secret_ref_for(admin_id, instance_id, "instagram_auth"))
    assert facebook is not None and facebook["access_token"] == "fb-tok-long"
    assert instagram is not None and instagram["access_token"] == LONG_LIVED

    by_type = {
        c["connectionType"]: c for c in (await client.get("/api/v1/admin/connections")).json()
    }
    assert by_type["channel_auth"]["status"] == "connected"
    assert by_type["instagram_auth"]["status"] == "connected"


# --------------------------------------------------------------------------- #
# Honest-disabled when the platform has no Instagram app
# --------------------------------------------------------------------------- #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_unprovisioned_instagram_app_is_honestly_disabled(client):
    """No Instagram app is a SUPPORTED posture, not a broken one: the owner sees a disabled
    tile instead of a consent screen whose code could never be redeemed. This is the same
    honest-disabled contract Meta and Calendly are held to."""
    await _ready_account_with_luciel(client)

    r = await client.get("/api/v1/admin/connections/providers?connectionType=instagram_auth")
    assert r.status_code == 200, r.text
    providers = r.json()[0]["providers"]
    assert [p["provider"] for p in providers] == ["instagram"]
    assert providers[0]["configured"] is False
    assert providers[0]["authKind"] == "oauth"
    assert providers[0]["helpText"]

    rs = await client.post(
        "/api/v1/admin/connections",
        json={"connectionType": "instagram_auth", "provider": "instagram"},
    )
    assert rs.status_code == 200, rs.text
    # Never instagram.com: an authorize URL built without a client id sends the owner to a
    # real consent screen that errors at Instagram instead of telling them the truth here.
    assert "instagram.com" not in (rs.json().get("authorizeUrl") or "")
    # ...and the row says why, so the tile can read "Not available yet" rather than invite
    # a click that dead-ends.
    row = next(
        c
        for c in (await client.get("/api/v1/admin/connections")).json()
        if c["connectionType"] == "instagram_auth"
    )
    assert row["status"] == "unconfigured"
    assert row["statusDetail"]
