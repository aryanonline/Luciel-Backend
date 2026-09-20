"""LLM-backed graph extraction behind the same seam (round 6 WP-G, F122).

Prose has no node lines; the LLM extractor infers typed nodes/edges through the fast
model under strict, capped JSON, merges the deterministic grammar in, and falls back to
the deterministic result on ANY failure. Domain-agnostic: every type comes from the
model's reading of the text, never from a keyword table.
"""

from __future__ import annotations

import json

import pytest
from app.config import get_settings
from app.knowledge import graph_extract as ge
from app.runtime.llm_router import LLMResponse, ModelClass, ProviderError

PROSE = """
Our Platform team is hiring a Senior Engineer who must know Python.
Trail Runner (Product): price=120
"""


class _FakeRouter:
    def __init__(self, reply: str | None = None, *, error: Exception | None = None) -> None:
        self.reply = reply
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def complete(self, system_prompt: str, messages: list[dict[str, object]], **kw: object):
        self.calls.append({"system": system_prompt, "messages": messages, **kw})
        if self.error is not None:
            raise self.error
        return LLMResponse(
            text=self.reply or "", provider="fake", model_class=ModelClass.fast, confidence=1.0
        )


_GOOD = json.dumps(
    {
        "entities": [
            {"name": "Senior Engineer", "type": "Role", "attributes": {"team": "Platform"}},
            {"name": "Python", "type": "Skill"},
            {"name": "Platform", "type": "Team"},
        ],
        "edges": [
            {"source": "Senior Engineer", "target": "Python", "type": "requires"},
            {"source": "Platform", "target": "Senior Engineer", "type": "hiring for"},
            {"source": "Platform", "target": "Senior Engineer", "type": "hiring for"},
        ],
    }
)


async def test_prose_becomes_typed_nodes_and_edges_merged_with_the_grammar():
    router = _FakeRouter("```json\n" + _GOOD + "\n```")
    out = await ge.LlmGraphExtractor(router_factory=lambda: router).aextract(PROSE)
    by_name = {e.canonical: e for e in out.entities}
    assert by_name["senior engineer"].entity_type == "Role"
    assert by_name["senior engineer"].attributes == {"team": "Platform"}
    assert by_name["python"].entity_type == "Skill"
    # The explicit grammar line survives the merge untouched.
    assert by_name["trail runner"].entity_type == "Product"
    assert by_name["trail runner"].attributes == {"price": "120"}
    rels = {(e.src_name, e.relationship_type, e.dst_name) for e in out.edges}
    assert rels == {
        ("Senior Engineer", "REQUIRES", "Python"),
        ("Platform", "HIRING_FOR", "Senior Engineer"),
    }
    # Routed as a fast, tool-free call.
    call = router.calls[0]
    assert call["requires_tools"] is False and call["complexity"] == 0.0
    assert "no fixed list of types" in str(call["system"])


@pytest.mark.parametrize("reply", ["not json at all", "[1, 2]", '{"entities": "x"}', ""])
async def test_unusable_model_output_falls_back_to_the_deterministic_result(reply: str):
    router = _FakeRouter(reply)
    out = await ge.LlmGraphExtractor(router_factory=lambda: router).aextract(PROSE)
    assert {e.name for e in out.entities} == {"Trail Runner"}
    assert out.edges == []


async def test_a_provider_failure_never_breaks_ingest():
    router = _FakeRouter(error=ProviderError("boom"))
    out = await ge.LlmGraphExtractor(router_factory=lambda: router).aextract(PROSE)
    assert {e.name for e in out.entities} == {"Trail Runner"}


def test_caps_hold_even_through_edge_endpoints():
    many = {
        "entities": [{"name": f"E{i}", "type": "T"} for i in range(100)],
        "edges": [{"source": "E0", "target": "Ghost", "type": "knows"}]
        + [{"source": f"E{i}", "target": f"E{i + 1}", "type": "next"} for i in range(200)],
    }
    out = ge.parse_llm_graph(json.dumps(many))
    assert out is not None
    names = {e.name for e in out.entities}
    assert len(names) == ge.MAX_LLM_ENTITIES
    assert "Ghost" not in names  # past the cap, a dangling endpoint is dropped with its edge
    assert len(out.edges) == ge.MAX_LLM_ENTITIES - 1  # E0→E1 … E38→E39 only
    assert all(e.dst_name in names for e in out.edges)
    assert ge.parse_llm_graph('{"entities": [{"name": "  "}], "edges": [{"source": "a"}]}') == (
        ge.ExtractionResult(entities=[], edges=[])
    )


def test_a_dangling_endpoint_under_the_cap_becomes_an_unknown_node():
    out = ge.parse_llm_graph(
        '{"entities": [{"name": "A", "type": "T"}],'
        ' "edges": [{"source": "A", "target": "Ghost", "type": "knows"}]}'
    )
    assert out is not None
    ghost = next(e for e in out.entities if e.name == "Ghost")
    assert ghost.entity_type == "unknown"
    assert [(e.src_name, e.relationship_type, e.dst_name) for e in out.edges] == [
        ("A", "KNOWS", "Ghost")
    ]


async def test_extract_async_serves_both_extractor_kinds():
    det = ge.DeterministicGraphExtractor()
    assert (await ge.extract_async(det, "A (Thing)")).entities[0].entity_type == "Thing"
    llm = ge.LlmGraphExtractor(router_factory=lambda: _FakeRouter(_GOOD))
    assert any(e.entity_type == "Skill" for e in (await ge.extract_async(llm, PROSE)).entities)
    # The sync Protocol on the LLM extractor is the deterministic grammar only.
    assert {e.name for e in llm.extract(PROSE).entities} == {"Trail Runner"}


def test_the_seam_binds_the_llm_extractor_only_in_live_mode(monkeypatch: pytest.MonkeyPatch):
    ge.reset_extractor()
    monkeypatch.setenv("LUCIEL_GRAPH_EXTRACTOR", "llm")
    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        assert isinstance(ge.get_extractor(), ge.DeterministicGraphExtractor)  # mock mode
        ge.reset_extractor()
        monkeypatch.setattr("app.config.live_mode", lambda: True)
        assert isinstance(ge.get_extractor(), ge.LlmGraphExtractor)
    finally:
        ge.reset_extractor()
        monkeypatch.delenv("LUCIEL_GRAPH_EXTRACTOR", raising=False)
        get_settings.cache_clear()  # type: ignore[attr-defined]


async def test_ingest_time_extraction_is_capped_under_the_ingesting_tenant():
    """Round 7 WP-2, item 4: the extractor is a process singleton built without a
    tenant, so the ingest call itself carries admin_id to the router's cap."""
    import uuid

    admin = uuid.uuid4()
    router = _FakeRouter("```json\n" + _GOOD + "\n```")
    llm = ge.LlmGraphExtractor(router_factory=lambda: router)
    await ge.extract_async(llm, PROSE, admin_id=admin)
    assert router.calls[0]["admin_id"] == admin
    # Direct use without a tenant stays uncapped, exactly as before.
    await llm.aextract(PROSE)
    assert router.calls[1]["admin_id"] is None
    # The deterministic extractor ignores the tenant.
    det = ge.DeterministicGraphExtractor()
    assert (await ge.extract_async(det, "A (Thing)", admin_id=admin)).entities[0].name == "A"
