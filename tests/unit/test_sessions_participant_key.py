"""participant_key_for — the §3.4.8 transport-identifier session key.

The key is what makes "conversation = one billed session" real on the non-widget
channels, so its properties are pinned: deterministic, tenant-salted, channel-scoped,
normalized for email, and absent (None) exactly where the HARD RULE (§3.4.9) demands
— the weak widget identity and empty identifiers.
"""

from __future__ import annotations

import uuid

from app.runtime.sessions import participant_key_for

ADMIN_A = uuid.UUID("00000000-0000-0000-0000-00000000000a")
ADMIN_B = uuid.UUID("00000000-0000-0000-0000-00000000000b")


def test_same_identifier_same_channel_same_tenant_is_stable():
    k1 = participant_key_for(ADMIN_A, "sms", "+14165550143")
    k2 = participant_key_for(ADMIN_A, "sms", "+14165550143")
    assert k1 is not None and k1 == k2
    assert len(k1) == 64  # sha256 hex — fits the String(64) column


def test_key_is_tenant_salted():
    """Equal phone numbers on two tenants must produce unrelated keys, so the resume
    index can never correlate one person across tenants (belt to RLS's suspenders)."""
    assert participant_key_for(ADMIN_A, "sms", "+14165550143") != participant_key_for(
        ADMIN_B, "sms", "+14165550143"
    )


def test_key_is_channel_scoped():
    """The §3.4.8 session key includes the channel: the same person texting and
    WhatsApp-ing concurrently holds two sessions, one per transport."""
    assert participant_key_for(ADMIN_A, "sms", "+14165550143") != participant_key_for(
        ADMIN_A, "whatsapp", "+14165550143"
    )


def test_email_is_case_folded_and_stripped():
    """dana@Lead.example and DANA@lead.example are one mailbox in practice; splitting
    them would split one person's conversation into two billed sessions."""
    a = participant_key_for(ADMIN_A, "email", "  Dana@Lead.Example ")
    b = participant_key_for(ADMIN_A, "email", "dana@lead.example")
    assert a == b


def test_phone_numbers_are_not_case_folded_but_are_used_verbatim():
    assert participant_key_for(ADMIN_A, "sms", "+14165550143") == participant_key_for(
        ADMIN_A, "sms", "+14165550143"
    )


def test_widget_never_gets_a_participant_key():
    """HARD RULE §3.4.9: the weak widget token resumes ONLY by its own session_id.
    A fingerprint-derived participant key would let a shared/recycled identifier
    inherit another visitor's conversation."""
    assert participant_key_for(ADMIN_A, "widget", "fp-abc123") is None


def test_unknown_channel_defaults_to_weak_and_gets_no_key():
    assert participant_key_for(ADMIN_A, "not_a_channel", "someone") is None


def test_empty_identifier_gets_no_key():
    assert participant_key_for(ADMIN_A, "sms", "") is None
    assert participant_key_for(ADMIN_A, "sms", None) is None


def test_split_meta_channels_carry_keys():
    """messenger and instagram are platform-scoped-strong (§3.4.9) — their scoped ids
    are stable within the platform, so their conversations thread across messages."""
    assert participant_key_for(ADMIN_A, "messenger", "PSID_1") is not None
    assert participant_key_for(ADMIN_A, "instagram", "IGSID_1") is not None
