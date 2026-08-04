"""LIVE-VERIFIED (Harmony wave 2, item 1): cross-channel speaker-name composition.

app.channels.runtime_entry._names is the SMS/email/WhatsApp/Instagram-Messenger
analogue of app.api.v1.chat_widget._names. Both must fall back to the business
name VERBATIM (never "{business}'s assistant") when no assistantName override is
configured, or a business/Luciel name already ending in the word "Assistant"
(e.g. "Aryan's Front-Desk Assistant") doubles into "...Assistant's assistant"
once the greeting template appends its own disclosure clause.
"""

from __future__ import annotations

from app.channels.runtime_entry import _names as runtime_entry_names
from app.models.instance import Instance, LucielState


def _instance(name: str, personality: dict | None = None) -> Instance:
    return Instance(
        name=name,
        state=LucielState.active,
        channels=[],
        tools=[],
        escalation={},
        personality=personality or {},
    )


def test_fallback_uses_business_name_verbatim_not_suffixed():
    inst = _instance("Aryan's Front-Desk Assistant")
    business, assistant = runtime_entry_names(inst)
    assert business == "Aryan's Front-Desk Assistant"
    # Never suffixed with "'s assistant" -- that suffix is what doubles the word
    # "Assistant" when the configured name already ends with it.
    assert assistant == "Aryan's Front-Desk Assistant"
    assert "assistant's assistant" not in assistant.lower()


def test_explicit_assistant_name_override_still_wins():
    inst = _instance("Aryan's Front-Desk Assistant", personality={"assistantName": "Nova"})
    business, assistant = runtime_entry_names(inst)
    assert business == "Aryan's Front-Desk Assistant"
    assert assistant == "Nova"
