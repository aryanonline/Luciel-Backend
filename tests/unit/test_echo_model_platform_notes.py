"""EchoModel echoes the CUSTOMER's last message, never a platform note (round 7 WP-2,
item 4): the knowledge-search status note the orchestrator appends after the history
is flagged ``platform_note`` so the deterministic double keeps every grounding and
readback test meaningful."""

from __future__ import annotations

import pytest
from app.runtime.orchestrator import KNOWLEDGE_NONE_MATCHED, PLATFORM_NOTE_KEY
from app.runtime.testing import EchoModel


@pytest.mark.asyncio
async def test_echo_skips_platform_notes_and_echoes_the_customer() -> None:
    resp = await EchoModel().complete(
        "system",
        [
            {"role": "user", "content": "do you open Saturdays?"},
            {"role": "user", "content": KNOWLEDGE_NONE_MATCHED, PLATFORM_NOTE_KEY: True},
        ],
    )
    assert resp.text == "Echo: do you open Saturdays?"


@pytest.mark.asyncio
async def test_echo_still_echoes_the_last_customer_message_without_notes() -> None:
    resp = await EchoModel().complete(
        "system",
        [{"role": "user", "content": "hi"}, {"role": "user", "content": "Knowledge:\nx"}],
    )
    assert resp.text == "Echo: Knowledge:\nx"
