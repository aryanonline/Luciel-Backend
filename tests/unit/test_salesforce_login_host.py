"""Salesforce's login host is a per-connection choice (round 7 WP-7).

A Salesforce SANDBOX org authenticates at test.salesforce.com; login.salesforce.com does
not know its users, so a sandbox could not be connected at all — authorize, exchange and
refresh were all hard-coded to the production host. The choice
(``non_secret_config['login_host']`` ∈ {production, sandbox}, made on the connect
request) is now honoured by every one of the three, and nothing else moves: the org's
own ``instance_url`` still carries every API call, and providers with no choice refuse
one.
"""

from __future__ import annotations

import uuid
from typing import Any
from urllib.parse import urlparse

import pytest
from app.config import Settings
from app.connections import oauth_flow, refresh, registry
from app.connections.secrets import LocalSecretStore
from app.models.connection import InstanceConnection

PROD = "login.salesforce.com"
SANDBOX = "test.salesforce.com"


def _host(url: str | None) -> str:
    return urlparse(url or "").netloc


@pytest.fixture
def salesforce_app(monkeypatch: pytest.MonkeyPatch) -> Settings:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        salesforce_consumer_key="sf-cid",
        salesforce_consumer_secret="sf-secret",  # noqa: S106 — test fixture
        salesforce_redirect_uri="https://api.example.test/api/v1/connections/salesforce/callback",
    )
    monkeypatch.setattr(registry, "get_settings", lambda: settings)
    return settings


def test_the_choice_is_offered_default_first_and_only_by_salesforce() -> None:
    assert registry.login_host_choices("salesforce") == ("production", "sandbox")
    assert registry.login_host_choices("hubspot") == ()
    assert registry.validated_login_host("salesforce", None) == "production"
    assert registry.validated_login_host("salesforce", "sandbox") == "sandbox"
    assert registry.validated_login_host("hubspot", None) is None
    with pytest.raises(registry.LoginHostRejected, match="sandbox"):
        registry.validated_login_host("hubspot", "sandbox")
    with pytest.raises(registry.LoginHostRejected, match="staging"):
        registry.validated_login_host("salesforce", "staging")


def test_every_endpoint_follows_the_choice() -> None:
    sandbox = registry.get_provider("salesforce", login_host="sandbox")
    prod = registry.get_provider("salesforce")
    assert sandbox is not None and sandbox.authorize is not None
    assert prod is not None and prod.authorize is not None
    assert _host(sandbox.authorize.endpoint) == SANDBOX
    assert _host(sandbox.authorize.exchange_endpoint) == SANDBOX
    assert _host(prod.authorize.endpoint) == PROD
    assert _host(prod.authorize.exchange_endpoint) == PROD
    # Only the host moves: the OAuth paths are the same on both.
    assert urlparse(sandbox.authorize.endpoint).path == urlparse(prod.authorize.endpoint).path
    # The table itself keeps the production default (the frontend host allowlist and
    # every caller without a connection read it).
    assert _host(registry.PROVIDERS["salesforce"].authorize.endpoint) == PROD  # type: ignore[union-attr]


def test_the_scope_carries_the_choice_to_reads_without_a_connection() -> None:
    """The token exchange in knowledge.providers asks get_provider(provider) with no row;
    the callback wraps it in the connection's scope."""
    with registry.login_host_scope("sandbox"):
        spec = registry.get_provider("salesforce")
        hubspot = registry.get_provider("hubspot")
    assert spec is not None and _host(spec.authorize.exchange_endpoint) == SANDBOX  # type: ignore[union-attr]
    assert hubspot is not None and _host(hubspot.authorize.endpoint) == "app.hubspot.com"  # type: ignore[union-attr]
    after = registry.get_provider("salesforce")
    assert after is not None and _host(after.authorize.endpoint) == PROD  # type: ignore[union-attr]
    with registry.login_host_scope(None):
        assert _host(registry.get_provider("salesforce").authorize.endpoint) == PROD  # type: ignore[union-attr]


def test_the_consent_url_goes_to_the_chosen_host(salesforce_app: Settings) -> None:
    ids = {"admin_id": uuid.uuid4(), "instance_id": uuid.uuid4(), "connection_id": uuid.uuid4()}
    sandbox = oauth_flow.build_authorize_start(provider="salesforce", login_host="sandbox", **ids)
    prod = oauth_flow.build_authorize_start(provider="salesforce", **ids)
    assert not sandbox.unconfigured
    assert _host(sandbox.authorize_url) == SANDBOX
    assert _host(prod.authorize_url) == PROD


async def test_the_exchange_redeems_at_the_chosen_host(salesforce_app: Settings) -> None:
    from app.knowledge import providers as knowledge_providers

    class _Resp:
        status_code = 200

        @staticmethod
        def json() -> dict[str, Any]:
            return {
                "access_token": "sf-at",
                "instance_url": "https://acme--dev.sandbox.my.salesforce.com",
            }

    posted: list[str] = []

    class _Transport:
        async def post(self, url: str, **_kw: Any) -> _Resp:
            posted.append(url)
            return _Resp()

        async def get(self, url: str, **_kw: Any) -> _Resp:  # pragma: no cover
            raise AssertionError("no GET in a Salesforce exchange")

    with registry.login_host_scope("sandbox"):
        await knowledge_providers.exchange_code_for_token(
            "salesforce", code="c", transport=_Transport(), code_verifier="v"
        )
    await knowledge_providers.exchange_code_for_token(
        "salesforce", code="c", transport=_Transport(), code_verifier="v"
    )
    assert [_host(u) for u in posted] == [SANDBOX, PROD]


def _row(login_host: str | None) -> InstanceConnection:
    cfg: dict[str, object] = {}
    if login_host:
        cfg["login_host"] = login_host
    return InstanceConnection(
        connection_id=uuid.uuid4(),
        admin_id=uuid.uuid4(),
        instance_id=uuid.uuid4(),
        connection_type="crm",
        auth_class="oauth_token",
        provider="salesforce",
        status="connected",
        secret_ref="vm/t/i/sf",
        non_secret_config=cfg,
    )


@pytest.mark.parametrize(("choice", "host"), [("sandbox", SANDBOX), (None, PROD)])
async def test_the_refresh_goes_to_the_row_s_host(
    salesforce_app: Settings, choice: str | None, host: str
) -> None:
    posted: list[str] = []

    class _Resp:
        status_code = 200

        @staticmethod
        def json() -> dict[str, Any]:
            return {"access_token": "new", "expires_in": 7200}

    class _Transport:
        async def post(self, url: str, **_kw: Any) -> _Resp:
            posted.append(url)
            return _Resp()

        async def get(self, url: str, **_kw: Any) -> _Resp:  # pragma: no cover
            raise AssertionError("Salesforce refreshes by POST")

    store = LocalSecretStore()
    await store.put("vm/t/i/sf", {"access_token": "old", "refresh_token": "rt"})
    refresher = refresh.RegistryTokenRefresher(transport_factory=_Transport)
    assert await refresher.refresh(_row(choice), store) is not None
    assert [_host(u) for u in posted] == [host]
    assert urlparse(posted[0]).path == "/services/oauth2/token"
