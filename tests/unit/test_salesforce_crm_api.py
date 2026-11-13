"""SalesforceCrmApi transport (Phase 6.5 — owner decision 2026-08-23).

Lead upsert against the TENANT's own org: SOQL find by Email (with injection-
safe quoting), create with Salesforce's required LastName/Company mapped from
captured facts under honest "Unknown" fallbacks (create ONLY — an update never
re-sends the fallbacks or Email, round 5A item 1), PATCH update with 404 → heal
signal, and CrmUnavailable on provider refusals — never a fabricated record.
"""

from __future__ import annotations

import pytest
from app.tools.crm_backend import CrmUnavailable, SalesforceCrmApi, _soql_quote

INSTANCE = "https://acme.my.salesforce.com"


class _Resp:
    def __init__(self, status_code: int, payload: object = None) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        return self._payload


class _Client:
    def __init__(self, *responses: _Resp) -> None:
        self._responses = list(responses)
        self.requests: list[tuple[str, str, object]] = []

    async def __aenter__(self) -> _Client:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def _next(self) -> _Resp:
        return self._responses.pop(0)

    async def get(self, url: str, *, headers=None, params=None) -> _Resp:
        self.requests.append(("GET", url, params))
        assert headers["Authorization"] == "Bearer sf-tok"
        return self._next()

    async def post(self, url: str, *, headers=None, json=None) -> _Resp:
        self.requests.append(("POST", url, json))
        return self._next()

    async def patch(self, url: str, *, headers=None, json=None) -> _Resp:
        self.requests.append(("PATCH", url, json))
        return self._next()


def _api(client: _Client) -> SalesforceCrmApi:
    return SalesforceCrmApi(instance_url=INSTANCE, transport_factory=lambda: client)


def test_soql_quoting_defuses_injection():
    assert _soql_quote("a'; DROP--") == "a\\'; DROP--"
    assert _soql_quote("back\\slash'q") == "back\\\\slash\\'q"


async def test_find_by_key_queries_lead_by_email():
    client = _Client(_Resp(200, {"records": [{"Id": "00Q000000000001"}]}))
    found = await _api(client).find_by_key("sf-tok", "jane@acme.com")
    assert found is not None and found.record_id == "00Q000000000001"
    method, url, params = client.requests[0]
    assert method == "GET" and url.endswith("/services/data/v59.0/query")
    assert params == {"q": "SELECT Id FROM Lead WHERE Email = 'jane@acme.com' LIMIT 1"}

    empty = _Client(_Resp(200, {"records": []}))
    assert await _api(empty).find_by_key("sf-tok", "jane@acme.com") is None


async def test_create_maps_captured_facts_with_unknown_fallbacks():
    client = _Client(_Resp(201, {"id": "00Q000000000002"}))
    created = await _api(client).create(
        "sf-tok", "jane@acme.com", {"intent": "wants a quote", "phone": "604-555-0100"}
    )
    assert created.record_id == "00Q000000000002"
    _, url, payload = client.requests[0]
    assert url.endswith("/sobjects/Lead")
    # Salesforce REQUIRES LastName + Company; absent facts become an honest
    # "Unknown", never an invented name (disclosed in the field mapping).
    assert payload == {
        "Email": "jane@acme.com",
        "LastName": "Unknown",
        "Company": "Unknown",
        "Phone": "604-555-0100",
        "Description": "intent: wants a quote",
    }


async def test_create_uses_real_name_and_business_when_captured():
    client = _Client(_Resp(201, {"id": "00Q000000000003"}))
    await _api(client).create(
        "sf-tok", "jane@acme.com", {"name": "Jane P.", "business": "Acme Plumbing"}
    )
    _, _, payload = client.requests[0]
    assert payload["LastName"] == "Jane P."
    assert payload["Company"] == "Acme Plumbing"


async def test_update_204_succeeds_and_404_signals_heal():
    ok = _Client(_Resp(204))
    updated = await _api(ok).update("sf-tok", "00Q1", "jane@acme.com", {"intent": "x"})
    assert updated is not None and updated.record_id == "00Q1"
    method, url, payload = ok.requests[0]
    assert method == "PATCH" and url.endswith("/sobjects/Lead/00Q1")
    # Round 5A item 1: ONLY the changed facts ride. The old shared payload
    # builder re-sent Email + "Unknown" LastName/Company on every update,
    # overwriting whatever the tenant had typed into their own org.
    assert payload == {"Description": "intent: x"}

    gone = _Client(_Resp(404))
    assert await _api(gone).update("sf-tok", "00Q1", "jane@acme.com", {"intent": "y"}) is None


async def test_update_with_nothing_changed_never_calls_the_provider():
    """An empty update payload is a no-op, not a PATCH: there is nothing to write,
    so the record is affirmed from the link without an API round-trip."""
    idle = _Client()  # no responses queued — any request would pop from empty
    updated = await _api(idle).update("sf-tok", "00Q1", "jane@acme.com", {})
    assert updated is not None and updated.record_id == "00Q1"
    assert idle.requests == []


async def test_provider_refusals_raise_never_fabricate():
    with pytest.raises(CrmUnavailable):
        await _api(_Client(_Resp(401, {}))).find_by_key("sf-tok", "j@a.com")
    with pytest.raises(CrmUnavailable):
        await _api(_Client(_Resp(400, {}))).create("sf-tok", "j@a.com", {})
    with pytest.raises(CrmUnavailable):
        await _api(_Client(_Resp(500))).update("sf-tok", "00Q1", "j@a.com", {"intent": "x"})
