"""Round 5A item 1 — CRM update payloads carry ONLY what changed.

Live-audited 2026-08-24: the shared payload builder re-sent Email + LastName +
Company with "Unknown" fallbacks on EVERY Salesforce update, so a routine
intent-only push overwrote whatever the tenant had typed into their own org —
the exact opposite of "keeps each lead's record current". HubSpot re-sent the
email property on every PATCH, reverting a tenant's in-CRM correction.
"""

from __future__ import annotations

import httpx
import pytest
from app.tools.crm_backend import HubSpotCrmApi, SalesforceCrmApi

pytestmark = [pytest.mark.asyncio]


class _CapturingTransport:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict]] = []

    async def post(self, url, *, headers=None, json=None, params=None):
        self.requests.append(("POST", url, dict(json or {})))
        return httpx.Response(201, json={"id": "rec-1"}, request=httpx.Request("POST", url))

    async def patch(self, url, *, headers=None, json=None):
        self.requests.append(("PATCH", url, dict(json or {})))
        return httpx.Response(200, json={}, request=httpx.Request("PATCH", url))

    async def get(self, url, *, headers=None, params=None):
        self.requests.append(("GET", url, dict(params or {})))
        return httpx.Response(200, json={"records": []}, request=httpx.Request("GET", url))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _sf(transport: _CapturingTransport) -> SalesforceCrmApi:
    return SalesforceCrmApi(
        instance_url="https://org.my.salesforce.com", transport_factory=lambda: transport
    )


def _hs(transport: _CapturingTransport) -> HubSpotCrmApi:
    return HubSpotCrmApi(transport_factory=lambda: transport)


async def test_salesforce_update_never_carries_unknown_or_email():
    transport = _CapturingTransport()
    await _sf(transport).update("tok", "rec-1", "jane@acme.com", {"intent": "quote for a reno"})
    _, _, payload = transport.requests[0]
    assert "LastName" not in payload
    assert "Company" not in payload
    assert "Email" not in payload
    assert "Unknown" not in str(payload.values())
    assert payload["Description"] == "intent: quote for a reno"


async def test_salesforce_update_carries_changed_name_without_fallbacks():
    transport = _CapturingTransport()
    await _sf(transport).update("tok", "rec-1", "jane@acme.com", {"name": "Jane Miller"})
    _, _, payload = transport.requests[0]
    assert payload["LastName"] == "Jane Miller"
    assert "Company" not in payload and "Email" not in payload


async def test_salesforce_update_writes_email_only_when_the_identity_moved():
    """The 'email' field appears in an update's fields exactly when the link's
    last-pushed key differs — the one legitimate Email write."""
    transport = _CapturingTransport()
    await _sf(transport).update("tok", "rec-1", "new@acme.com", {"email": "new@acme.com"})
    _, _, payload = transport.requests[0]
    assert payload["Email"] == "new@acme.com"
    assert "Unknown" not in str(payload.values())


async def test_salesforce_create_keeps_the_required_honest_fallbacks():
    transport = _CapturingTransport()
    await _sf(transport).create("tok", "jane@acme.com", {"intent": "quote"})
    _, _, payload = transport.requests[0]
    # Salesforce REQUIRES LastName + Company on create — the disclosed "Unknown"
    # placeholder belongs here and only here.
    assert payload["Email"] == "jane@acme.com"
    assert payload["LastName"] == "Unknown"
    assert payload["Company"] == "Unknown"


async def test_salesforce_empty_update_makes_no_provider_call():
    transport = _CapturingTransport()
    result = await _sf(transport).update("tok", "rec-1", "jane@acme.com", {})
    assert result is not None and result.record_id == "rec-1"
    assert transport.requests == []


async def test_hubspot_update_never_rewrites_the_email_property():
    transport = _CapturingTransport()
    await _hs(transport).update("tok", "rec-9", "jane@acme.com", {"intent": "site visit"})
    _, _, payload = transport.requests[0]
    # F128: our field names never reach HubSpot; a non-standard fact rides the
    # standard `message` property, and the email property is never re-sent.
    assert payload == {"properties": {"message": "intent: site visit"}}


async def test_hubspot_create_still_keys_on_the_email_property():
    transport = _CapturingTransport()
    await _hs(transport).create("tok", "jane@acme.com", {"name": "Jane"})
    _, _, payload = transport.requests[0]
    assert payload["properties"]["email"] == "jane@acme.com"
