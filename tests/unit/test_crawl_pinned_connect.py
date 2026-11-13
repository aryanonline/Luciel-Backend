"""Adversarial review R-WP4-1: the website crawl connects to the address it validated.

``resolves_to_public_host`` checked a NAME and the crawl then connected by name again —
the same rebinding window F090 closed for the webhook POST, left open on the one fetch
whose body becomes tenant-readable knowledge chunks. The crawl now uses the pinned
connect: the resolved address in the URL, the hostname as Host + SNI."""

from __future__ import annotations

from typing import Any

import pytest
from app.connections import sandbox
from app.knowledge import providers
from app.knowledge.providers import ProviderUnavailable, WebsiteCrawlProvider


class _Resp:
    text = "<html><body>Pricing: from $10</body></html>"
    status_code = 200
    headers: dict[str, str] = {}

    def raise_for_status(self) -> None:
        return None


class _Client:
    calls: list[dict[str, Any]] = []

    def __init__(self, *_a: Any, **_k: Any) -> None:
        pass

    async def __aenter__(self) -> _Client:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def get(self, url: str, **kwargs: Any) -> _Resp:
        type(self).calls.append({"url": url, **kwargs})
        return _Resp()


async def test_the_crawl_connects_to_the_validated_address_with_the_name_as_host_and_sni(
    monkeypatch,
):
    _Client.calls = []
    monkeypatch.setattr(sandbox, "_resolve_ips", lambda host: ["93.184.216.34"])
    monkeypatch.setattr(providers.httpx, "AsyncClient", _Client)
    provider = WebsiteCrawlProvider(urls=["https://acme.example/pricing"])
    body = await provider._fetch_one("https://acme.example/pricing")
    assert "Pricing" in body
    assert _Client.calls == [
        {
            "url": "https://93.184.216.34/pricing",
            "headers": {"Host": "acme.example", "User-Agent": providers.CRAWL_USER_AGENT},
            "extensions": {"sni_hostname": "acme.example"},
            # Redirects are followed by hand so every hop is re-pinned (WP-4c #1).
            "follow_redirects": False,
        }
    ]


async def test_a_rebinding_answer_never_reaches_the_socket(monkeypatch):
    """The metadata endpoint as a DNS answer for a public-looking name: refused before
    any client is built, so there is no connect to race."""
    _Client.calls = []
    monkeypatch.setattr(sandbox, "_resolve_ips", lambda host: ["169.254.170.2"])
    monkeypatch.setattr(providers.httpx, "AsyncClient", _Client)
    provider = WebsiteCrawlProvider(urls=["https://acme.example/"])
    with pytest.raises(ProviderUnavailable):
        await provider._fetch_one("https://acme.example/")
    assert _Client.calls == []
