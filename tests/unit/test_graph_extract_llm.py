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
from app.runtime.llm_router import LLMResponse, LLMUsage, ModelClass, ProviderError

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


# --- Per-ingest cost bound (round 7 WP-4c #10, D10 turns the pass on for live) -------


async def test_a_source_under_the_minimum_size_never_reaches_the_model():
    router = _FakeRouter(_GOOD)
    llm = ge.LlmGraphExtractor(router_factory=lambda: router)
    tiny = "Trail Runner (Product): price=120"
    assert len(tiny) < ge.MIN_LLM_TEXT_CHARS
    out = await llm.aextract(tiny)
    assert router.calls == []  # no call, no cost
    assert {e.name for e in out.entities} == {"Trail Runner"}  # the grammar still ran


async def test_a_large_source_is_read_as_a_bounded_number_of_chunks():
    from app.knowledge.chunking import DEFAULT_CHUNK_CHARS

    router = _FakeRouter(_GOOD)
    llm = ge.LlmGraphExtractor(router_factory=lambda: router)
    # 30 paragraphs of ~one chunk each: far more than the cap, and more than the
    # character clip alone would admit.
    paragraphs = [
        f"Paragraph {i}. " + ("lorem ipsum " * (DEFAULT_CHUNK_CHARS // 12)) for i in range(30)
    ]
    text = "\n\n".join(paragraphs)
    await llm.aextract(text)
    sent = str(router.calls[0]["messages"][0]["content"])
    assert len(sent) <= ge.MAX_LLM_TEXT_CHARS
    assert sent.count("Paragraph ") <= ge.MAX_LLM_CHUNKS
    assert "Paragraph 0." in sent and "Paragraph 29." not in sent


def test_llm_input_for_is_the_single_bound():
    assert ge.llm_input_for("") is None
    assert ge.llm_input_for("x" * (ge.MIN_LLM_TEXT_CHARS - 1)) is None
    body = ge.llm_input_for("word " * 5_000)
    assert body is not None and len(body) <= ge.MAX_LLM_TEXT_CHARS


# --- Ingest headroom (round 7 Phase 2 review, knowledge #1) --------------------------
#
# The ingest pass is admitted under the SAME per-tenant minute cap as the tenant's
# customer turns. A sync ingests page after page, so without headroom it could spend the
# whole minute and push a customer into the at-cap reply. Above INGEST_HEADROOM_FRACTION
# of the cap the pass is skipped (grammar only, flagged for re-extraction) — and the
# check is a READ, so skipping costs the tenant nothing.


def _fresh_cap_store(monkeypatch: pytest.MonkeyPatch):
    import app.runtime.llm_cap as llm_cap

    store = llm_cap.InMemoryLLMCapStore()
    monkeypatch.setattr(llm_cap, "_store", store)
    return store


def _headroom_threshold() -> int:
    from app.runtime.llm_cap import LLM_CAP_PER_MINUTE

    return int(LLM_CAP_PER_MINUTE * ge.INGEST_HEADROOM_FRACTION)


async def test_ingest_skips_the_llm_pass_once_half_the_tenants_cap_is_spent(monkeypatch):
    import uuid

    store = _fresh_cap_store(monkeypatch)
    admin = uuid.uuid4()
    for _ in range(_headroom_threshold()):
        assert store.admit(str(admin)) is True  # customer turns / earlier ingests
    before = store.current_count(str(admin))

    router = _FakeRouter(_GOOD)
    out = await ge.extract_async(
        ge.LlmGraphExtractor(router_factory=lambda: router), PROSE, admin_id=admin
    )

    assert router.calls == []  # the model was never called
    grammar = ge.DeterministicGraphExtractor().extract(PROSE)
    assert (out.entities, out.edges) == (grammar.entities, grammar.edges)
    assert out.llm_skipped is True  # the sync re-extracts it next pass
    assert store.current_count(str(admin)) == before  # the skip was free (no INCR)
    # Another tenant's spend is not this tenant's: its pass still runs.
    other = uuid.uuid4()
    assert (
        await ge.extract_async(ge.LlmGraphExtractor(lambda: router), PROSE, admin_id=other)
    ).llm_skipped is False
    assert router.calls[0]["admin_id"] == other


async def test_ingest_below_the_headroom_still_reaches_the_model_under_the_tenant(monkeypatch):
    import uuid

    store = _fresh_cap_store(monkeypatch)
    admin = uuid.uuid4()
    for _ in range(_headroom_threshold() - 1):
        assert store.admit(str(admin)) is True

    router = _FakeRouter(_GOOD)
    out = await ge.extract_async(
        ge.LlmGraphExtractor(router_factory=lambda: router), PROSE, admin_id=admin
    )

    assert router.calls[0]["admin_id"] == admin
    assert out.llm_skipped is False
    assert any(e.entity_type == "Skill" for e in out.entities)


async def test_a_cap_refusal_is_a_budget_skip_but_a_vendor_failure_is_not(monkeypatch):
    import uuid

    from app.runtime.llm_cap import LLMCapExceeded

    _fresh_cap_store(monkeypatch)
    admin = uuid.uuid4()
    refused = await ge.LlmGraphExtractor(
        router_factory=lambda: _FakeRouter(error=LLMCapExceeded(admin))
    ).aextract(PROSE, admin_id=admin)
    assert refused.llm_skipped is True
    assert {e.name for e in refused.entities} == {"Trail Runner"}
    failed = await ge.LlmGraphExtractor(
        router_factory=lambda: _FakeRouter(error=ProviderError("boom"))
    ).aextract(PROSE, admin_id=admin)
    # A broken vendor is not retried every pass: the grammar-only graph stands.
    assert failed.llm_skipped is False
    # A pass that was never owed (a short source) is never "skipped" either.
    tiny = await ge.LlmGraphExtractor(router_factory=lambda: _FakeRouter(_GOOD)).aextract(
        "Trail Runner (Product): price=120", admin_id=admin
    )
    assert tiny.llm_skipped is False


# --- round 7 walk run 8: the output ceiling, and an unusable answer ----------------- #
# Four of one crawl's eight graph calls logged ``graph_extract.llm_unparseable``. A probe
# against the live model found the cause: the long pages' JSON stopped at the
# providers' 1024-token default ceiling, mid-object.


class _TruncatedRouter(_FakeRouter):
    def __init__(self, reply: str, *, output_tokens: int = 1024) -> None:
        super().__init__(reply)
        self.output_tokens = output_tokens

    async def complete(self, system_prompt: str, messages: list[dict[str, object]], **kw: object):
        self.calls.append({"system": system_prompt, "messages": messages, **kw})
        return LLMResponse(
            text=self.reply or "",
            provider="fake",
            model_class=ModelClass.fast,
            confidence=1.0,
            truncated=True,
            usage=LLMUsage(provider="fake", model="m", output_tokens=self.output_tokens),
        )


async def test_the_graph_call_asks_for_a_4096_ceiling_and_compact_json():
    router = _FakeRouter(_GOOD)
    await ge.LlmGraphExtractor(router_factory=lambda: router).aextract(PROSE)
    call = router.calls[0]
    assert call["max_output_tokens"] == ge.GRAPH_MAX_OUTPUT_TOKENS == 4096
    assert "single line" in str(call["system"]) and "no indentation" in str(call["system"])


_PRETTY = json.dumps(json.loads(_GOOD), indent=2)
_ACCEPTED_REPLIES = [
    pytest.param(_GOOD, id="compact"),
    pytest.param(_PRETTY, id="pretty-printed"),
    pytest.param("```json\n" + _PRETTY + "\n```", id="fenced-pretty"),
    pytest.param("```JSON\n" + _GOOD + "\n```\n", id="upper-fence-trailing-newline"),
    pytest.param("Here is the graph you asked for:\n" + _GOOD, id="leading-prose"),
]


@pytest.mark.parametrize("reply", _ACCEPTED_REPLIES)
def test_the_compact_prompt_parser_still_accepts_pretty_and_fenced_answers(reply: str):
    out = ge.parse_llm_graph(reply)
    assert out is not None
    assert {e.canonical: e.entity_type for e in out.entities} == {
        "senior engineer": "Role",
        "python": "Skill",
        "platform": "Team",
    }
    assert {(e.src_name, e.relationship_type, e.dst_name) for e in out.edges} == {
        ("Senior Engineer", "REQUIRES", "Python"),
        ("Platform", "HIRING_FOR", "Senior Engineer"),
    }


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        pytest.param(_GOOD[: len(_GOOD) // 2], "json_error", id="truncated-object"),
        pytest.param("```json\n" + _GOOD[:-10] + "\n```", "json_error", id="truncated-fence"),
        pytest.param("I could not find any entities.", "no_json_object", id="prose-only"),
        pytest.param("[1, 2]", "no_json_object", id="a-list-not-an-object"),
        pytest.param("", "no_json_object", id="empty"),
    ],
)
def test_an_unusable_answer_says_why(reply: str, reason: str):
    # Nothing is guessed: a truncated object decodes nowhere.
    assert ge.parse_llm_graph(reply) is None
    assert ge.graph_json(reply) == (None, reason)


async def test_an_unparseable_answer_is_a_skip_so_the_sync_re_extracts(caplog):
    import logging

    from app.knowledge import sync

    # Braces, but not JSON (unquoted keys) — and the provider did NOT truncate it.
    router = _FakeRouter("Here you go: {entities: [Senior Engineer], edges: []}")
    with caplog.at_level(logging.WARNING, logger="app.knowledge.graph_extract"):
        out = await ge.LlmGraphExtractor(router_factory=lambda: router).aextract(PROSE)
    assert out.llm_skipped is True
    assert {e.name for e in out.entities} == {"Trail Runner"}  # the grammar still serves
    record = next(r for r in caplog.records if r.getMessage() == "graph_extract.llm_unparseable")
    assert getattr(record, "reason", None) == "json_error"
    # The sync's per-item pass reads the flag (it leaves the revision marker unbumped).
    graph = sync._GraphPass(ge.LlmGraphExtractor(router_factory=lambda: router))
    await graph.aextract(PROSE)
    assert graph.llm_skipped is True


async def test_a_prose_only_answer_logs_no_json_object(caplog):
    import logging

    router = _FakeRouter("I could not find any entities in this text.")
    with caplog.at_level(logging.WARNING, logger="app.knowledge.graph_extract"):
        out = await ge.LlmGraphExtractor(router_factory=lambda: router).aextract(PROSE)
    assert out.llm_skipped is True
    record = next(r for r in caplog.records if r.getMessage() == "graph_extract.llm_unparseable")
    assert getattr(record, "reason", None) == "no_json_object"


async def test_a_truncated_answer_keeps_the_grammar_and_is_not_deferred(caplog):
    import logging

    from app.knowledge import sync

    router = _TruncatedRouter(_GOOD[: len(_GOOD) // 2], output_tokens=4096)
    with caplog.at_level(logging.WARNING, logger="app.knowledge.graph_extract"):
        out = await ge.LlmGraphExtractor(router_factory=lambda: router).aextract(PROSE)
    # The same page would hit the ceiling again next pass: NOT a skip, so the revision
    # marker bumps and the sync does not re-extract it on every pass.
    assert out.llm_skipped is False
    assert {e.name for e in out.entities} == {"Trail Runner"}
    assert "graph_extract.llm_unparseable" not in [r.getMessage() for r in caplog.records]
    record = next(r for r in caplog.records if r.getMessage() == "graph_extract.llm_truncated")
    assert getattr(record, "output_tokens", None) == 4096
    graph = sync._GraphPass(ge.LlmGraphExtractor(router_factory=lambda: router))
    await graph.aextract(PROSE)
    assert graph.llm_skipped is False


async def test_a_truncated_flag_on_a_complete_answer_still_merges():
    router = _TruncatedRouter(_GOOD)
    out = await ge.LlmGraphExtractor(router_factory=lambda: router).aextract(PROSE)
    assert out.llm_skipped is False
    assert {e.canonical for e in out.entities} >= {"senior engineer", "python", "trail runner"}
