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
    # ONE Meta grant powers WhatsApp, Instagram and Messenger (§3.8.1).
    assert [p.provider for p in registry.providers_for("channel_auth")] == ["meta"]
    # SMS is BYO (§3.1.4): the customer's own Twilio, never a platform-provisioned number.
    assert [p.provider for p in registry.providers_for("sms_sender")] == ["twilio"]


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
