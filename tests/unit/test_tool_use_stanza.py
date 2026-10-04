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
        "T5. TAKE NO FOR AN ANSWER",
    ):
        assert marker in prompt


def test_the_t5_rule_tells_the_model_to_stop_asking_after_a_decline() -> None:
    """§3.3.7 rule 4 (round 7 WP-2 remainder, item 2): a declined detail is not asked
    for again in other words, is never invented, and a blocked action is said so."""
    prompt = build_system_prompt(None, tool_catalog=CATALOG)
    t5 = prompt[prompt.index("T5. TAKE NO FOR AN ANSWER") : prompt.index("Available tools:")]
    assert "do not ask for it again" in t5
    assert "Never invent the value" in t5
    assert "cannot go ahead without that detail" in t5
    # The rule sits inside the tools stanza, with the other numbered rules.
    assert prompt.index("T4. CHECK BEFORE BOOKING") < prompt.index("T5. TAKE NO FOR AN ANSWER")
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
    # Nothing on file can conjure a stanza without tools (item 10).
    assert compose_tool_use_stanza(None, on_file=("email",)) == ""


def test_the_on_file_line_names_confirmed_inputs_inside_the_stanza() -> None:
    """Round 7 WP-2 remainder, item 10: the model is told WHICH required inputs the
    conversation already holds confirmed (names, never values) so it stops asking
    for them; the broker fills the values. Nothing on file → no line."""
    plain = build_system_prompt(None, tool_catalog=CATALOG)
    assert "On file for this customer" not in plain
    prompt = build_system_prompt(None, tool_catalog=CATALOG, on_file=("email", "phone"))
    assert "On file for this customer, confirmed: email, phone." in prompt
    assert "never ask for it again" in prompt
    assert "(T2)" in prompt
    assert prompt.index("- lookup_record:") < prompt.index("On file for this customer")
    assert prompt.index("On file for this customer") < prompt.index("--- Answer style")
    # Outside the tools stanza the prompt is untouched: no tools, no line.
    assert build_system_prompt("ctx", on_file=("email",)) == build_system_prompt("ctx")
