"""Unit pins for round 7 WP-3c items 10–14 — the helpers whose shape the contract
and integration suites rely on.

  * item 10: the SES parser case-folds the recipient (the routing claim is stored
    lower-case, and the resolver compares byte-exactly);
  * item 12: the rate-limited OAuth claim is still an ``OAuthExchangeRejected`` for the
    browser callback (lands on ``exchange_failed``, nothing about the holder in a URL);
  * item 13: the shared-portal note is INFORMATIONAL — the helper returns a note and
    audits ``crm_portal_shared``; it never refuses, never names the other account;
  * item 14: the mint shape and the conflict detector.
"""

from __future__ import annotations

import json
import uuid

from app.api.v1.admin import connections as connections_api
from app.api.v1.admin.connections import (
    PORTAL_SHARED_NOTE,
    OAuthClaimRateLimited,
    OAuthExchangeRejected,
    _portal_sharing_note,
    _with_note,
    portal_identity,
)
from app.api.v1.webhooks.channels import _parse_ses_inbound
from app.audit.events import EVENT_TYPES
from app.lifecycle import embed_keys
from sqlalchemy.exc import IntegrityError

# --- item 10 ----------------------------------------------------------------------- #


def test_the_ses_recipient_is_case_folded_before_routing() -> None:
    parsed = _parse_ses_inbound(
        {
            "Message": json.dumps(
                {
                    "mail": {
                        "destination": [" Luciel@Acme.Vantagemind.Example "],
                        "source": "Dana <dana@lead.example>",
                        "commonHeaders": {"from": ["Dana <dana@lead.example>"], "subject": "Hi"},
                    },
                    "receipt": {},
                }
            )
        }
    )
    assert parsed["recipient"] == "luciel@acme.vantagemind.example"
    assert parsed["from_address"] == "dana@lead.example"  # the lead's key is untouched


# --- item 12 ----------------------------------------------------------------------- #


def test_a_rate_limited_oauth_claim_is_still_an_exchange_failure_for_the_browser() -> None:
    assert issubclass(OAuthClaimRateLimited, OAuthExchangeRejected)


# --- item 13 ----------------------------------------------------------------------- #


def test_portal_identity_names_the_crm_portal_field_per_provider() -> None:
    assert portal_identity("hubspot", {"hubId": " 424242 "}) == (
        "hubId",
        "424242",
        "HubSpot portal",
    )
    assert portal_identity("salesforce", {"orgId": "00D000000000001"}) == (
        "orgId",
        "00D000000000001",
        "Salesforce org",
    )
    assert portal_identity("hubspot", {"hubDomain": "acme.hubspot.com"}) is None  # no id
    assert portal_identity("outlook", {"destination": "owner@fabrikam.example"}) is None


def test_the_note_is_appended_once_and_never_clobbers_other_detail() -> None:
    note = PORTAL_SHARED_NOTE.format(label="HubSpot portal")
    assert _with_note(None, note) == note
    assert _with_note("Reconnect needed.", note) == f"Reconnect needed. {note}"
    assert _with_note(note, note) == note  # idempotent across re-reads


class _Emitted:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def emit(self, _session, **kwargs) -> None:
        self.events.append(kwargs)


async def test_a_portal_another_account_holds_yields_a_note_and_an_event_never_a_refusal(
    monkeypatch,
) -> None:
    me, other = uuid.uuid4(), uuid.uuid4()
    emitted = _Emitted()

    async def _holders(_session, connection_type, key, value):
        assert (connection_type, key, value) == ("crm", "hubId", "424242")
        return [other, me]

    monkeypatch.setattr(connections_api.routing, "holders_of_connection_config", _holders)
    monkeypatch.setattr(connections_api.audit, "emit", emitted.emit)

    note = await _portal_sharing_note(
        object(),
        admin_id=me,
        instance_id=uuid.uuid4(),
        actor_user_id=me,
        connection_type="crm",
        provider="hubspot",
        verify_updates={"hubId": "424242", "hubDomain": "acme.hubspot.com"},
    )
    assert note == PORTAL_SHARED_NOTE.format(label="HubSpot portal")
    assert "both push leads into the same CRM" in note  # informational wording
    assert len(emitted.events) == 1
    event = emitted.events[0]
    assert event["event_type"] == "crm_portal_shared"
    assert "crm_portal_shared" in EVENT_TYPES
    assert event["admin_id"] == me
    assert event["payload"] == {
        "provider": "hubspot",
        "portal_key": "hubId",
        "portal_id_suffix": "4242",
        "other_account_count": 1,
    }
    assert str(other) not in json.dumps(event["payload"])  # never WHICH account


async def test_a_portal_only_this_account_holds_yields_nothing(monkeypatch) -> None:
    me = uuid.uuid4()
    emitted = _Emitted()

    async def _holders(_session, connection_type, key, value):
        return [me]

    monkeypatch.setattr(connections_api.routing, "holders_of_connection_config", _holders)
    monkeypatch.setattr(connections_api.audit, "emit", emitted.emit)
    note = await _portal_sharing_note(
        object(),
        admin_id=me,
        instance_id=uuid.uuid4(),
        actor_user_id=None,
        connection_type="crm",
        provider="salesforce",
        verify_updates={"orgId": "00D000000000001"},
    )
    assert note is None
    assert emitted.events == []


async def test_a_provider_with_no_portal_identity_never_looks_anything_up(monkeypatch) -> None:
    async def _never(*_a, **_k):
        raise AssertionError("looked up holders for a provider with no portal identity")

    monkeypatch.setattr(connections_api.routing, "holders_of_connection_config", _never)
    assert (
        await _portal_sharing_note(
            object(),
            admin_id=uuid.uuid4(),
            instance_id=uuid.uuid4(),
            actor_user_id=None,
            connection_type="email_sender",
            provider="outlook",
            verify_updates={"destination": "owner@fabrikam.example"},
        )
        is None
    )


# --- item 14 ----------------------------------------------------------------------- #


def test_the_mint_shape_is_the_public_key_prefix_plus_twelve_hex() -> None:
    key = embed_keys.mint_embed_key()
    assert key.startswith(embed_keys.EMBED_KEY_PREFIX)
    suffix = key[len(embed_keys.EMBED_KEY_PREFIX) :]
    assert len(suffix) == 12 and int(suffix, 16) >= 0
    assert embed_keys.mint_embed_key() != key


def test_only_the_embed_key_index_counts_as_a_mint_conflict() -> None:
    ours = IntegrityError(
        "INSERT", {}, Exception('duplicate key value violates "ix_instances_embed_key"')
    )
    theirs = IntegrityError("INSERT", {}, Exception('violates "uq_sms_destination"'))
    assert embed_keys._is_embed_key_conflict(ours) is True
    assert embed_keys._is_embed_key_conflict(theirs) is False
