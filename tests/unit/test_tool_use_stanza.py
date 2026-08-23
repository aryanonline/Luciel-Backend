"""Prompt composition with the tool-use stanza (§3.3.7 / audit round 3, C3).

Pins: (a) the stanza appears iff a tool catalog is passed — the no-tool prompt is
byte-identical to the pre-tool-use composition; (b) the stanza sits ABOVE the
personality/style stanzas and the untrusted admin field (accuracy doctrine rides
with the invariants); (c) the four numbered tool rules and every catalog tool name
are present; (d) Defense 6 ordering is unchanged — invariants first, admin context
last and wrapped.
"""

from __future__ import annotations

from app.runtime.input_safety import build_system_prompt, compose_tool_use_stanza
from app.tools.schemas import build_tool_defs

CATALOG = build_tool_defs({"check_availability", "book_appointment", "lookup_record"})


def test_no_tool_catalog_keeps_prompt_byte_identical() -> None:
    assert build_system_prompt("ctx") == build_system_prompt("ctx", tool_catalog=None)
    assert build_system_prompt("ctx") == build_system_prompt("ctx", tool_catalog=[])
    assert "--- Tools" not in build_system_prompt("ctx")


def test_stanza_lists_rules_and_every_tool() -> None:
    prompt = build_system_prompt(None, tool_catalog=CATALOG)
    for marker in (
        "T1. GATHER FIRST",
        "T2. CONFIRM BEFORE WRITING",
        "T3. READ RESULTS",
        "T4. CHECK BEFORE BOOKING",
    ):
        assert marker in prompt
    for tool_id in ("check_availability", "book_appointment", "lookup_record"):
        assert f"- {tool_id}:" in prompt


def test_stanza_precedes_style_and_admin_context() -> None:
    prompt = build_system_prompt("my business context", tool_catalog=CATALOG)
    tool_at = prompt.index("--- Tools")
    style_at = prompt.index("--- Answer style")
    ctx_at = prompt.index("--- Business context")
    invariants_at = prompt.index("1. AI IDENTITY")
    assert invariants_at < tool_at < style_at < ctx_at


def test_compose_tool_use_stanza_empty_for_no_tools() -> None:
    assert compose_tool_use_stanza(None) == ""
    assert compose_tool_use_stanza([]) == ""
