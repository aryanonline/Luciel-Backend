"""Round 6 WP-C: the health sweep asks the provider whether the grant still works.

``StoredCredentialProbe`` answers "do we still hold a credential?"; the liveness probe
answers "does the provider still accept it?" — live posture only, one identity call
per row per sweep, and an honest boundary: 401/403 (after one refresh attempt) is a
dead grant, everything else is "could not tell", never an error.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.connections import probes
from app.connections.health import ProbeResult
from app.connections.probes import (
    RATE_LIMIT_BACKOFF_DEFAULT,
    RATE_LIMIT_BACKOFF_MAX,
    RATE_LIMIT_BACKOFF_MIN,
    DispatchingProbe,
    ProviderLivenessProbe,
    liveness_request_for,
)
from app.models.connection import InstanceConnection


def _conn(
    provider: str,
    *,
    connection_type: str = "crm",
    auth_class: str = "oauth_token",
    cfg: dict[str, Any] | None = None,
    secret_ref: str | None = "vm/a/i/crm",
) -> InstanceConnection:
    return InstanceConnection(
        connection_id=uuid.uuid4(),
        admin_id=uuid.uuid4(),
        instance_id=uuid.uuid4(),
        connection_type=connection_type,
        provider=provider,
        auth_class=auth_class,
        status="connected",
        secret_ref=secret_ref,
        non_secret_config=dict(cfg or {}),
    )


class _Store:
    def __init__(self, material: dict[str, str] | None) -> None:
        self.material = material

    async def get(self, ref: str) -> dict[str, str] | None:
        return self.material

    async def put(self, ref: str, material: dict[str, str]) -> None:
        self.material = material

    async def delete(self, ref: str) -> bool:
        self.material = None
        return True


class _Resp:
    def __init__(self, status: int) -> None:
        self.status_code = status


class _Client:
    """Answers the queued statuses in order; records every call."""

    calls: list[dict[str, Any]] = []
    statuses: list[Any] = []

    def __init__(self, *_a: Any, **_k: Any) -> None:
        pass

    async def __aenter__(self) -> _Client:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def get(
        self, url: str, *, headers: Any = None, auth: Any = None, extensions: Any = None
    ) -> _Resp:
        type(self).calls.append(
            {"url": url, "headers": dict(headers or {}), "auth": auth, "extensions": extensions}
        )
        status = type(self).statuses.pop(0)
        if isinstance(status, Exception):
            raise status
        if isinstance(status, _Resp):
            return status  # a prepared response (headers and all)
        return _Resp(status)


def _client(*statuses: Any) -> type[_Client]:
    _Client.calls = []
    _Client.statuses = list(statuses)
    return _Client


class _NoRefresh:
    async def refresh(self, connection: InstanceConnection, store: Any) -> datetime | None:
        return None


class _Refreshes:
    def __init__(self, store: _Store) -> None:
        self._store = store

    async def refresh(self, connection: InstanceConnection, store: Any) -> datetime | None:
        self._store.material = {"access_token": "fresh-tok"}
        return datetime.now(UTC) + timedelta(hours=1)


def test_liveness_requests_name_the_provider_identity_call():
    sf = liveness_request_for(
        _conn("salesforce"), {"access_token": "t", "instance_url": "https://acme.my.salesforce.com"}
    )
    assert sf is not None
    assert sf.url == "https://acme.my.salesforce.com/services/oauth2/userinfo"
    assert sf.headers["Authorization"] == "Bearer t"
    hs = liveness_request_for(_conn("hubspot"), {"access_token": "t"})
    assert hs is not None and hs.url.endswith("/oauth/v1/access-tokens/t")
    g = liveness_request_for(
        _conn("google_calendar", connection_type="calendar"), {"access_token": "t"}
    )
    assert g is not None and g.url.startswith(probes.GOOGLE_TOKENINFO_ENDPOINT)
    notion = liveness_request_for(
        _conn("notion", connection_type="knowledge_source"), {"access_token": "t"}
    )
    assert notion is not None and notion.headers["Notion-Version"]
    # Twilio API keys: HTTP Basic with the key pair against the account resource.
    tw_key = liveness_request_for(
        _conn(
            "twilio",
            connection_type="sms_sender",
            auth_class="api_key",
            cfg={"accountSid": "AC123", "apiKeySid": "SK456"},
        ),
        {"apiKeySecret": "s3cret"},
    )
    assert tw_key is not None
    assert tw_key.url == "https://api.twilio.com/2010-04-01/Accounts/AC123.json"
    assert tw_key.auth == ("SK456", "s3cret")
    tw_token = liveness_request_for(
        _conn(
            "twilio",
            connection_type="sms_sender",
            auth_class="api_key",
            cfg={"accountSid": "AC123"},
        ),
        {"authToken": "auth"},
    )
    assert tw_token is not None and tw_token.auth == ("AC123", "auth")
    # Nothing to ask for a provisioned sender, a webhook row with no URL on file, or a
    # crawl target.
    for provider, ctype in (
        ("csv", "record_source"),
        ("custom_webhook", "crm"),
        ("website_crawl", "knowledge_source"),
        ("ses", "email_sender"),
    ):
        assert liveness_request_for(_conn(provider, connection_type=ctype), {}) is None
    # A Salesforce row with no org URL cannot be probed: skipped, never guessed.
    assert liveness_request_for(_conn("salesforce"), {"access_token": "t"}) is None


# --- Round 7 WP-4a item 6: the URL-shaped providers are probed at their own URL ----- #
def test_url_shaped_rows_are_probed_at_the_stored_url_with_the_stored_header():
    hook = liveness_request_for(
        _conn(
            "custom_webhook",
            auth_class="long_lived_token",
            cfg={"url": "https://hooks.example.com/leads"},
        ),
        {"auth_header": "Bearer tenant-token"},
    )
    assert hook is not None
    assert hook.url == "https://hooks.example.com/leads"
    assert hook.headers["Authorization"] == "Bearer tenant-token"
    assert hook.pin_public_host is True, "an owner URL is pinned like every other admin fetch"
    # No header on file is a legitimate shape (the URL is the whole credential).
    bare = liveness_request_for(
        _conn(
            "outbound_webhook",
            connection_type="outbound_webhook",
            auth_class="long_lived_token",
            cfg={"url": "https://hooks.example.com/events"},
        ),
        {},
    )
    assert bare is not None and "Authorization" not in bare.headers
    # The live connector is asked the same synthetic query as its connect-time proof.
    live = liveness_request_for(
        _conn(
            "live_connector",
            connection_type="record_source",
            auth_class="long_lived_token",
            cfg={"url": "https://records.example.com/lookup"},
        ),
        {"api_key": "k"},
    )
    assert live is not None
    assert live.url == f"https://records.example.com/lookup?q={probes.LIVE_CONNECTOR_PROBE_QUERY}"
    assert live.headers["Authorization"] == "Bearer k" and live.pin_public_host is True
    # A row written under the superseded ``api`` id resolves the same way.
    legacy = liveness_request_for(
        _conn("api", connection_type="record_source", cfg={"url": "https://r.example.com/x"}), {}
    )
    assert legacy is not None and legacy.url.endswith(f"?q={probes.LIVE_CONNECTOR_PROBE_QUERY}")


def _pin_to(monkeypatch, host: str = "hooks.example.com", ip: str = "203.0.113.7") -> None:
    from app.connections import sandbox

    monkeypatch.setattr(sandbox, "pin_public_target", lambda url: (url.replace(host, ip, 1), host))


async def test_a_webhook_endpoint_that_refuses_the_header_is_a_dead_row(monkeypatch):
    _pin_to(monkeypatch)
    probe = ProviderLivenessProbe(
        store=_Store({"auth_header": "Bearer stale"}),
        transport_factory=_client(401),
        refresher=_NoRefresh(),
    )
    result = await probe.probe(
        _conn(
            "custom_webhook",
            auth_class="long_lived_token",
            cfg={"url": "https://hooks.example.com/leads"},
        )
    )
    assert result.ok is False
    assert "Custom webhook rejected the stored credential" in (result.detail or "")
    call = _Client.calls[0]
    # Sent to the validated address with the owner's hostname as Host + SNI (F090).
    assert call["url"] == "https://203.0.113.7/leads"
    assert call["headers"]["Host"] == "hooks.example.com"
    assert call["extensions"] == {"sni_hostname": "hooks.example.com"}
    assert call["headers"]["Authorization"] == "Bearer stale"


@pytest.mark.parametrize("status", [200, 404, 405, 503])
async def test_a_webhook_endpoint_that_does_not_refuse_never_flips_the_row(monkeypatch, status):
    _pin_to(monkeypatch)
    probe = ProviderLivenessProbe(
        store=_Store({}), transport_factory=_client(status), refresher=_NoRefresh()
    )
    result = await probe.probe(
        _conn(
            "outbound_webhook",
            connection_type="outbound_webhook",
            auth_class="long_lived_token",
            cfg={"url": "https://hooks.example.com/events"},
        )
    )
    assert result.ok is True


async def test_an_owner_url_that_now_resolves_privately_is_not_asked(monkeypatch):
    from app.connections import sandbox

    def _refuse(url: str) -> tuple[str, str]:
        raise sandbox.SsrfBlocked("non_public_address")

    monkeypatch.setattr(sandbox, "pin_public_target", _refuse)
    probe = ProviderLivenessProbe(
        store=_Store({}), transport_factory=_client(200), refresher=_NoRefresh()
    )
    result = await probe.probe(
        _conn(
            "custom_webhook",
            auth_class="long_lived_token",
            cfg={"url": "https://hooks.example.com/leads"},
        )
    )
    assert result.ok is True and result.detail == "probe_skipped:transport_error"
    assert _Client.calls == [], "nothing is sent to an address the pin refused"


async def test_a_2xx_is_healthy():
    probe = ProviderLivenessProbe(
        store=_Store({"access_token": "t"}), transport_factory=_client(200), refresher=_NoRefresh()
    )
    assert (await probe.probe(_conn("hubspot"))).ok is True


async def test_a_401_with_no_refresh_is_a_dead_grant():
    probe = ProviderLivenessProbe(
        store=_Store({"access_token": "t", "instance_url": "https://acme.my.salesforce.com"}),
        transport_factory=_client(401),
        refresher=_NoRefresh(),
    )
    result = await probe.probe(_conn("salesforce"))
    assert result.ok is False
    assert "Salesforce rejected the stored credential" in (result.detail or "")
    assert "reconnect" in (result.detail or "")


async def test_a_401_that_a_refresh_repairs_stays_connected():
    store = _Store({"access_token": "stale"})
    probe = ProviderLivenessProbe(
        store=store, transport_factory=_client(401, 200), refresher=_Refreshes(store)
    )
    conn = _conn("hubspot")
    assert (await probe.probe(conn)).ok is True
    assert conn.non_secret_config["token_expires_at"]  # the sweep's next cue, stamped here
    assert _Client.calls[1]["url"].endswith("/fresh-tok")  # re-probed with the rotated token


@pytest.mark.parametrize("status", [503, 429, 404])
async def test_a_provider_outage_or_a_moved_endpoint_is_skipped_not_an_error(status):
    probe = ProviderLivenessProbe(
        store=_Store({"access_token": "t"}),
        transport_factory=_client(status),
        refresher=_NoRefresh(),
    )
    result = await probe.probe(_conn("hubspot"))
    assert result.ok is True
    assert result.detail == f"probe_skipped:http_{status}"


async def test_a_transport_failure_is_skipped_not_an_error():
    probe = ProviderLivenessProbe(
        store=_Store({"access_token": "t"}),
        transport_factory=_client(OSError("no route")),
        refresher=_NoRefresh(),
    )
    result = await probe.probe(_conn("hubspot"))
    assert result.ok is True and result.detail == "probe_skipped:transport_error"


class _Fallback:
    def __init__(self, result: ProbeResult) -> None:
        self.result = result
        self.calls = 0

    async def probe(self, connection: InstanceConnection) -> ProbeResult:
        self.calls += 1
        return self.result


class _NeverAsked(ProviderLivenessProbe):
    async def probe(self, connection: InstanceConnection) -> ProbeResult:  # pragma: no cover
        raise AssertionError("the provider must not be asked here")


async def test_the_dispatching_probe_keeps_the_stored_credential_answer_in_mock(monkeypatch):
    monkeypatch.setattr(probes, "live_mode", lambda: False)
    fallback = _Fallback(ProbeResult(ok=True))
    result = await DispatchingProbe(fallback=fallback, liveness=_NeverAsked()).probe(
        _conn("hubspot")
    )
    assert result.ok is True and fallback.calls == 1


async def test_the_dispatching_probe_asks_the_provider_in_live(monkeypatch):
    monkeypatch.setattr(probes, "live_mode", lambda: True)
    liveness = ProviderLivenessProbe(
        store=_Store({"access_token": "t"}), transport_factory=_client(403), refresher=_NoRefresh()
    )
    result = await DispatchingProbe(
        fallback=_Fallback(ProbeResult(ok=True)), liveness=liveness
    ).probe(_conn("hubspot"))
    assert result.ok is False
    # A missing stored credential is final; the provider is not asked about nothing.
    missing = ProbeResult(ok=False, detail="stored credential is missing — reconnect")
    result = await DispatchingProbe(fallback=_Fallback(missing), liveness=_NeverAsked()).probe(
        _conn("hubspot")
    )
    assert result == missing


# --- Round 7 WP-4a item 4: the connect-time proof for URL-shaped providers --------- #
class _ProofTransport:
    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.calls: list[dict[str, Any]] = []

    async def get(self, url: str, *, headers: dict[str, str]) -> tuple[int, bytes]:
        self.calls.append({"url": url, "headers": dict(headers)})
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer, b""


@pytest.mark.parametrize("status", [200, 204, 404, 405, 410])
async def test_a_reachable_webhook_endpoint_that_accepts_the_header_proves(status):
    transport = _ProofTransport(status)
    await probes.prove_webhook_endpoint(
        url="https://hooks.example.com/x", auth_header="Bearer t", transport=transport
    )
    assert transport.calls[0]["headers"]["Authorization"] == "Bearer t"


@pytest.mark.parametrize("status", [401, 403])
async def test_a_webhook_endpoint_that_refuses_the_header_fails_the_proof(status):
    with pytest.raises(probes.WebhookProofFailed, match="refused the Authorization header"):
        await probes.prove_webhook_endpoint(
            url="https://hooks.example.com/x",
            auth_header="Bearer t",
            transport=_ProofTransport(status),
        )


@pytest.mark.parametrize("answer", [500, 503, OSError("no route"), TimeoutError()])
async def test_a_webhook_endpoint_that_does_not_answer_fails_the_proof(answer):
    with pytest.raises(probes.WebhookProofFailed, match="did not answer"):
        await probes.prove_webhook_endpoint(
            url="https://hooks.example.com/x", auth_header=None, transport=_ProofTransport(answer)
        )


async def test_a_webhook_proof_without_a_header_sends_none():
    transport = _ProofTransport(200)
    await probes.prove_webhook_endpoint(
        url="https://hooks.example.com/x", auth_header=None, transport=transport
    )
    assert "Authorization" not in transport.calls[0]["headers"]


# --------------------------------------------------------------------------- #
# Round 7 WP-4a item 10 — a 429 carries the provider's backoff, bounded
# --------------------------------------------------------------------------- #


class _RateLimited(_Resp):
    def __init__(self, retry_after: str | None) -> None:
        super().__init__(429)
        self.headers = {} if retry_after is None else {"Retry-After": retry_after}


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("120", timedelta(seconds=120)),
        (None, RATE_LIMIT_BACKOFF_DEFAULT),
        ("garbage", RATE_LIMIT_BACKOFF_DEFAULT),
        ("Wed, 21 Oct 2026 07:28:00 GMT", RATE_LIMIT_BACKOFF_DEFAULT),
        ("0", RATE_LIMIT_BACKOFF_MIN),
        ("-5", RATE_LIMIT_BACKOFF_MIN),
        (str(10 * 24 * 3600), RATE_LIMIT_BACKOFF_MAX),
    ],
)
async def test_a_429_is_skipped_with_the_providers_backoff_within_bounds(header, expected):
    """The live condition: HubSpot answers 429 with ``Retry-After: 120``. Before, the
    sweep read it as a plain skip and asked again next pass. The backoff rides the
    result so the sweep can persist ``next_probe_at``; an absent or unparseable
    header (an HTTP-date form is not honoured — a clock skew must not park a row
    for days) falls back to the default, and every value is clamped."""
    probe = ProviderLivenessProbe(
        store=_Store({"access_token": "t"}),
        transport_factory=_client(_RateLimited(header)),
        refresher=_NoRefresh(),
    )
    result = await probe.probe(_conn("hubspot"))
    assert result.ok is True
    assert result.detail == "probe_skipped:http_429"
    assert result.retry_after == expected


@pytest.mark.parametrize("status", [503, 404])
async def test_a_non_429_skip_carries_no_backoff(status):
    probe = ProviderLivenessProbe(
        store=_Store({"access_token": "t"}),
        transport_factory=_client(status),
        refresher=_NoRefresh(),
    )
    result = await probe.probe(_conn("hubspot"))
    assert result.detail == f"probe_skipped:http_{status}"
    assert result.retry_after is None
