"""The website-crawl knowledge source is not an exfiltration channel (P1-4, §3.8.6).

``WebsiteCrawlProvider`` fetched tenant-supplied URLs with no scheme restriction and no IP
validation, and the fetched body becomes knowledge chunks readable at
``GET /sources/{source_id}/chunks``. A verified tenant could point it at
``169.254.169.254`` and read the task-role credentials back out. Self-signup is open, so
"authenticated tenant" is not much of a precondition.

The guard is the one the BYO-webhook envelope already shipped, split so both callers share
it rather than growing a second copy that drifts.
"""

from __future__ import annotations

import pytest
from app.connections.sandbox import is_egress_allowed, resolves_to_public_host
from app.knowledge.providers import ProviderUnavailable, WebsiteCrawlProvider
from app.schemas_api.knowledge import CreateSyncConnectionRequest
from pydantic import ValidationError


def test_the_metadata_endpoint_is_refused_without_needing_an_allowlist():
    """The crawl case has no allowlist to check — an admin may crawl any site they own —
    so the link-local block has to stand on its own."""
    assert resolves_to_public_host("http://169.254.169.254/latest/meta-data/") is False
    assert resolves_to_public_host("http://127.0.0.1:5432/") is False
    assert resolves_to_public_host("http://10.0.0.5/internal") is False
    assert resolves_to_public_host("file:///etc/passwd") is False
    assert resolves_to_public_host("https://example.com/pricing") is True


def test_the_webhook_allowlist_still_applies_on_top():
    """Splitting the check must not have widened the webhook envelope."""
    assert is_egress_allowed("https://example.com/hook", frozenset({"hooks.acme.com"})) is False
    assert is_egress_allowed("https://example.com/hook", frozenset({"example.com"})) is True


async def test_a_crawl_of_the_metadata_endpoint_fails_instead_of_returning_credentials():
    provider = WebsiteCrawlProvider(urls=["http://169.254.169.254/latest/meta-data/iam/"])
    with pytest.raises(ProviderUnavailable):
        await provider.snapshot(connection_id=None)  # type: ignore[arg-type]


async def test_a_crawl_of_a_loopback_service_fails_rather_than_reading_it_back():
    provider = WebsiteCrawlProvider(urls=["http://localhost:8000/admin"])
    with pytest.raises(ProviderUnavailable):
        await provider.snapshot(connection_id=None)  # type: ignore[arg-type]


def test_a_non_http_crawl_url_is_refused_at_the_wire_boundary():
    """Rejected as a 422 at create time rather than surviving in non_secret_config until
    a sync tries it."""
    with pytest.raises(ValidationError):
        CreateSyncConnectionRequest(provider="website_crawl", crawlUrls=["file:///etc/passwd"])
    ok = CreateSyncConnectionRequest(
        provider="website_crawl", crawlUrls=["https://acme.example/pricing"]
    )
    assert [str(u) for u in ok.crawl_urls or []] == ["https://acme.example/pricing"]
