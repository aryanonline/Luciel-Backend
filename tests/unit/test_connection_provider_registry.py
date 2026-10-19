"""The provider registry's shape (connection_model_correction, Item D).

Every provider list the UI renders comes from ``providers_for``, so a wrong entry here is
a wrong choice offered to a paying owner: a record-source picker showing Google Calendar,
or two Meta entries that evict each other because they share a single-active type. The
HTTP-level bleed had a different cause (the ``connectionType`` filter was being dropped —
see tests/contract/test_connection_model_correction.py), but nothing pinned the underlying
table, so these are the floor that keeps it from coming back through the registry instead.
"""

from __future__ import annotations

from app.connections import registry


def test_provider_lists_are_exactly_what_each_connection_type_supports():
    """record_source is BYO data — a live connector or a CSV — and calendar is the two
    OAuth vendors. Anything else in these lists is an option the product cannot honour."""
    assert [p.provider for p in registry.providers_for("record_source")] == [
        "live_connector",
        "csv",
    ]
    assert [p.auth_kind for p in registry.providers_for("record_source")] == [
        registry.AUTH_CREDENTIAL_FORM,
        registry.AUTH_PROVISIONED,
    ]
    assert [p.provider for p in registry.providers_for("calendar")] == [
        "google_calendar",
        "calendly",
    ]
    # ONE Meta grant powers the WhatsApp and Messenger channels (§3.1.2 split);
    # Instagram rides its own Business Login connection type.
    assert [p.provider for p in registry.providers_for("channel_auth")] == ["meta"]
    assert registry.DEPENDENT_CHANNELS["channel_auth"] == ("whatsapp", "messenger")
    assert registry.DEPENDENT_CHANNELS["instagram_auth"] == ("instagram",)
    # SMS is BYO (§3.1.4): the customer's own Twilio, never a platform-provisioned number.
    assert [p.provider for p in registry.providers_for("sms_sender")] == ["twilio"]
    # Email's one CHOOSABLE option is the BYO Outlook mailbox (§3.1.6a). The platform
    # SES sender is provisioned via /connections/email, never offered as a pick —
    # but its rows must keep validating under the now-choiceful type.
    assert [p.provider for p in registry.providers_for("email_sender")] == ["outlook"]
    assert registry.is_valid_provider("email_sender", "ses")
    assert registry.DEPENDENT_CHANNELS["email_sender"] == ("email",)


def test_no_provider_is_ever_offered_for_a_connection_type_that_is_not_its_home():
    """Whatever a caller asks for, every option it gets back must belong to that type."""
    for connection_type in registry.CHOICE_TYPES:
        for spec in registry.providers_for(connection_type):
            assert spec.connection_type == connection_type, (connection_type, spec.provider)

    # An unknown type yields nothing rather than falling through to the whole catalog.
    assert registry.providers_for("not_a_connection_type") == []


def test_superseded_meta_provider_ids_still_resolve_to_the_single_grant():
    """Rows written before WhatsApp and Instagram collapsed into one connection still say
    ``meta_whatsapp`` / ``meta_instagram``. They must keep resolving, or an existing
    tenant's connected channel would fail to read back."""
    for legacy in ("meta_whatsapp", "meta_instagram"):
        spec = registry.get_provider(legacy)
        assert spec is not None, legacy
        assert spec.provider == "meta"


def test_provider_available_requires_redirect_uri_for_oauth_providers(monkeypatch):
    """GAP-N3 (round 4): id+secret without a redirect URI cannot START a consent
    (build_authorize_start refuses), so advertising the provider as available
    renders a Connect button that dead-ends on the placeholder URL. Availability
    must key on authorize_ready (id+secret+redirect); the credential_fallback
    provider (twilio) stays available regardless, because the customer's own API
    keys need nothing from the platform."""
    from app.config import Settings

    partial = Settings(
        notion_client_id="ncid",
        notion_client_secret="ncsecret",  # noqa: S106 — test fixture value  # nosec B106
        # notion_redirect_uri deliberately empty — the config trap this test pins.
    )
    monkeypatch.setattr(registry, "get_settings", lambda: partial)
    assert registry.provider_available("notion") is False
    assert registry.provider_available("twilio") is True  # credential fallback

    complete = Settings(
        notion_client_id="ncid",
        notion_client_secret="ncsecret",  # noqa: S106 — test fixture value  # nosec B106
        notion_redirect_uri="https://api.example.com/api/v1/connections/notion/callback",
    )
    monkeypatch.setattr(registry, "get_settings", lambda: complete)
    assert registry.provider_available("notion") is True


def test_every_oauth_authorize_host_is_mirrored_in_the_frontend_allowlist():
    """This set must mirror PROVIDER_AUTHORIZE_HOSTS in Luciel-Frontend
    apps/web/src/lib/oauth-connect.ts — the frontend refuses to navigate the
    browser to any authorize host outside its allowlist, so adding a provider
    here without adding its consent host there ships a Connect button that
    always answers "We could not start a secure sign-in" (GAP-N1, round 4:
    Notion's api.notion.com was missing and the flow was dead on arrival).
    When this test fails, update BOTH this frozenset and the frontend list
    (plus its oauth-connect-hosts.test.ts) in the same change."""
    from urllib.parse import urlparse

    registry_hosts = {
        urlparse(spec.authorize.endpoint).netloc
        for spec in registry.PROVIDERS.values()
        if spec.is_oauth
    }
    # Round 7 WP-7: a provider with login-host choices consents on every one of them
    # (a Salesforce sandbox signs in at test.salesforce.com), so each must be allowed.
    registry_hosts |= {
        host for spec in registry.PROVIDERS.values() for _choice, host in spec.login_hosts
    }
    assert registry_hosts == frozenset(
        {
            "accounts.google.com",
            "login.salesforce.com",
            "test.salesforce.com",
            "app.hubspot.com",
            "auth.calendly.com",
            "www.facebook.com",
            "www.instagram.com",
            "login.microsoftonline.com",
            "oauth.twilio.com",
            "api.notion.com",
        }
    )
