"""The production tool resolver covers the catalog and nothing else (Arch §3.3.2).

Pure mapping assertions — no DB. The end-to-end dispatch behaviour lives in
tests/integration/test_tool_registry_execution.py.
"""

from __future__ import annotations

from app.tools.registry import (
    CONNECTION_GATED_TOOL_IDS,
    FUNCTIONAL_TOOL_IDS,
    build_tool,
    make_tool_resolver,
)

ALL_TOOL_IDS = FUNCTIONAL_TOOL_IDS | CONNECTION_GATED_TOOL_IDS


def test_resolver_builds_every_catalog_tool_and_nothing_else() -> None:
    for tool_id in ALL_TOOL_IDS:
        tool = build_tool(tool_id)
        assert tool is not None, tool_id
        assert tool.tool_id == tool_id
    # Cognition (lead capture / escalate / handoff / summarize) is the always-on
    # substrate, NOT a tool (Vision §3.2) — it must never resolve to one.
    for not_a_tool in ("capture_leads", "escalate", "handoff", "summarize", ""):
        assert build_tool(not_a_tool) is None


def test_make_tool_resolver_returns_a_working_resolver() -> None:
    resolve = make_tool_resolver()
    assert resolve("schedule_callback") is not None
    assert resolve("nope") is None


def test_both_production_entrypoints_supply_the_resolver() -> None:
    """The register's finding was that the real paths passed snapshot_tool_ids but no
    resolver, so tools could never execute outside tests. Both must import it."""
    from app.api.v1 import chat_widget
    from app.channels import runtime_entry

    assert chat_widget.make_tool_resolver is make_tool_resolver
    assert runtime_entry.make_tool_resolver is make_tool_resolver
