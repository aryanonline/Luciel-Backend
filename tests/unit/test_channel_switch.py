"""Round 6 WP-H (F060): the channel-switch classifier and its deterministic copy."""

from __future__ import annotations

import pytest
from app.runtime.channel_switch import (
    DECLINED_LINE,
    PendingOutbound,
    ask_line,
    detect_requested_channel,
    identifier_for,
    is_confirmation,
    is_decline,
    new_pending,
    sent_line,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Can you email me the details?", "email"),
        ("send it to my email please", "email"),
        ("Could you send that by e-mail", "email"),
        ("Text me the address", "sms"),
        ("can you send me a text with the price", "sms"),
        ("send it to my phone", "sms"),
        ("What are your opening hours?", None),
        ("my email is dana@lead.example", None),  # an address is not a request
        ("", None),
    ],
)
def test_requests_for_another_channel_are_detected(text, expected):
    assert detect_requested_channel(text) == expected


def test_confirmations_and_declines_are_short_leading_words_only():
    for yes in ("yes", "Yes please", "yep, that's right", "sure", "go ahead", "ok"):
        assert is_confirmation(yes), yes
        assert not is_decline(yes), yes
    for no in ("no", "No thanks", "never mind", "not now", "don't"):
        assert is_decline(no), no
        assert not is_confirmation(no), no
    # A sentence that merely contains the word is neither.
    assert not is_confirmation("I said no to the yes option earlier")
    assert not is_decline("I know, yes")


def test_identifiers_are_read_and_normalised_by_kind():
    assert identifier_for("email", "sure, Dana.Lee@Lead.Example works") == "dana.lee@lead.example"
    assert identifier_for("phone", "text me at (604) 555-0123") == "+16045550123"
    assert identifier_for("email", "text me at (604) 555-0123") is None
    assert identifier_for("phone", "dana@lead.example") is None


def test_pending_round_trips_and_the_copy_never_claims_real_time():
    pending = new_pending("email", "The full price list…", None)
    again = PendingOutbound.from_json(pending.as_json())
    assert again == pending
    assert PendingOutbound.from_json({"channel": "carrier_pigeon"}) is None
    assert (
        ask_line("email", None)
        == "Happy to email you this. What's the best email address to send it to?"
    )
    assert ask_line("sms", "+16045550123").startswith(
        "Happy to text you this. Should I send it to +16045550123?"
    )
    assert (
        sent_line("email", "dana@lead.example") == "Sent — check your email at dana@lead.example."
    )
    assert "right now" not in DECLINED_LINE
