"""Tool-schema invariants (§3.3.1 / audit round 3, C3).

The schemas module is the single source the providers and the prompt stanza read;
these tests pin it to the broker's own catalog so the model can never be told about
a tool the broker doesn't enforce, or be under-told a required input.
"""

from __future__ import annotations

from app.tools.catalog import ALL_TOOL_IDS, TOOL_REQUIRED_INPUTS
from app.tools.schemas import ACTION_TOOLS, TOOL_SCHEMAS, build_tool_defs


def test_every_catalog_tool_has_exactly_one_schema() -> None:
    assert set(TOOL_SCHEMAS) == set(ALL_TOOL_IDS)


def test_schema_required_covers_broker_required_inputs() -> None:
    """The schema may describe MORE than the broker rejects on, never less — an
    under-declared schema would let the model omit a field the broker refuses."""
    for tool_id, spec in TOOL_SCHEMAS.items():
        required = set(spec["input_schema"].get("required", []))
        assert required >= set(TOOL_REQUIRED_INPUTS[tool_id]), tool_id


def test_schema_required_fields_are_all_described_properties() -> None:
    for tool_id, spec in TOOL_SCHEMAS.items():
        props = set(spec["input_schema"].get("properties", {}))
        required = set(spec["input_schema"].get("required", []))
        assert required <= props, tool_id


def test_action_tools_are_catalog_write_tools() -> None:
    """ACTION_TOOLS (the infer-but-confirm set, §3.3.7) is exactly the catalog's
    externally observable writes."""
    assert ACTION_TOOLS == {
        "book_appointment",
        "reschedule_appointment",
        "cancel_appointment",
        "send_email",
        "send_sms",
        "schedule_callback",
        "push_to_crm",
    }
    assert ACTION_TOOLS <= set(ALL_TOOL_IDS)


def test_build_tool_defs_is_sorted_and_skips_unknown_ids() -> None:
    defs = build_tool_defs({"lookup_record", "check_availability", "not_a_tool"})
    assert [d["tool_id"] for d in defs] == ["check_availability", "lookup_record"]
    for d in defs:
        assert d["description"]
        assert d["input_schema"]["type"] == "object"
