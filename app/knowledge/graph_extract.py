"""Entity/edge extraction at ingest (Architecture §3.2.1 — domain-agnostic graph).

Entities and relationships are extracted from admin-ingested knowledge at INGEST time
and stored as typed nodes + directed typed edges (§3.2.1). The platform imposes NO
vertical-specific ontology — entity_type and relationship_type are read from the source
content itself, never matched against a fixed real-estate/medical/etc. keyword list
(§3.2.1 domain-agnostic commitment, Locked Decision §7 #4 corollary).

The extractor is PLUGGABLE behind a Protocol (mirroring the Embedder / LLMProvider
pattern, §3.4.3 ethos): production can bind an LLM-backed extractor that infers nodes
from prose, while tests bind the deterministic line-grammar extractor below so no paid
vendor is called in CI. Both satisfy the same interface and are injected.

Deterministic grammar (the test/contract surface — a stable, vendor-free serialization
of "typed nodes + directed edges"):

  Node line:   ``Name (Type): key=value; key=value``
  Edge line:   ``SourceName -RELATIONSHIP_TYPE-> TargetName``

``Type`` and ``RELATIONSHIP_TYPE`` are whatever the text says — the extractor copies them
through verbatim (uppercased/trimmed for the canonical key) and never consults a vertical
ontology, so the SAME extractor handles recruiting, medical, retail, legal, real-estate,
etc. text identically (proven by the two-vertical extraction test).
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Protocol

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExtractedEntity:
    name: str
    entity_type: str
    attributes: dict[str, object] = field(default_factory=dict)

    @property
    def canonical(self) -> str:
        """Normalized membership/traversal key — case/space-insensitive (§3.2.1)."""
        return canonicalize(self.name)


@dataclass(frozen=True)
class ExtractedEdge:
    src_name: str
    dst_name: str
    relationship_type: str
    attributes: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ExtractionResult:
    entities: list[ExtractedEntity]
    edges: list[ExtractedEdge]
    # True when the LLM pass was owed but deliberately not run (the tenant's per-minute
    # budget was spoken for — see INGEST_HEADROOM_FRACTION — or the cap refused the call):
    # the result is grammar-only and the sync leaves the source's revision marker unbumped
    # so the next pass re-extracts it. A pass that was never owed (a short source, the
    # deterministic extractor) or that the model answered unusably is not "skipped": the
    # same page would get the same answer, at one LLM call and one re-embed per pass.
    llm_skipped: bool = False


def canonicalize(name: str) -> str:
    """Lower-case, collapse whitespace — the normalized node identity (§3.2.1)."""
    return re.sub(r"\s+", " ", name.strip().lower())


class GraphExtractor(Protocol):
    """Pluggable extractor interface (§3.2.1). Production = LLM-backed; tests = deterministic."""

    def extract(self, text: str) -> ExtractionResult: ...


# Node:  ``Name (Type): k=v; k=v``  — the attribute tail is optional.
#
# Tightened in round 7 (WP-4c #10, owner decision D10): with the LLM extractor on in
# live posture the grammar runs over PROSE too, and the old pattern read ANY
# parenthesised aside as a node — "We are open Monday to Friday (except holidays)"
# became an entity named "We are open Monday to Friday" of type "except holidays". A
# node line now has:
#   - a name of at most MAX_NODE_NAME_WORDS words / 80 chars, starting with a letter or
#     digit and free of sentence punctuation (, ; : ! ?);
#   - a type that is ONE word, or TWO Capitalised words, of letters/digits/_/-;
#   - nothing after the closing paren except an optional ``: key=value; ...`` tail.
MAX_NODE_NAME_WORDS = 5
_NODE_NAME = r"[A-Za-z0-9][^(),;:!?\n]{0,79}?"
_NODE_TYPE = r"(?:[A-Z][\w-]{0,31}\s+[A-Z][\w-]{0,31}|[A-Za-z][\w-]{0,31})"
_NODE_RE = re.compile(
    rf"^\s*(?P<name>{_NODE_NAME})\s*\((?P<type>{_NODE_TYPE})\)\s*(?::\s*(?P<attrs>[^=\n]+=.*))?$"
)
# Edge:  ``Source -REL-> Target``
_EDGE_RE = re.compile(r"^\s*(?P<src>.+?)\s*-(?P<rel>[A-Za-z0-9_ ]+?)->\s*(?P<dst>.+?)\s*$")


def _parse_attrs(raw: str | None) -> dict[str, object]:
    """Parse ``k=v; k=v`` attribute tails. Values stay strings (domain-agnostic)."""
    out: dict[str, object] = {}
    if not raw:
        return out
    for part in raw.split(";"):
        if "=" in part:
            k, _, v = part.partition("=")
            k, v = k.strip(), v.strip()
            if k:
                out[k] = v
    return out


class DeterministicGraphExtractor:
    """Vendor-free, fully deterministic extractor over the line grammar (§3.2.1).

    Domain-agnostic by construction: ``entity_type`` and ``relationship_type`` are taken
    verbatim from the parentheses / arrow label in the text. There is NO vertical keyword
    table — feed it recruiting text or retail text and it behaves identically (asserted by
    the two-vertical test). An edge endpoint that did not appear as an explicit node line is
    still recorded as an entity with ``entity_type='unknown'`` so traversal never dangles.
    """

    def extract(self, text: str) -> ExtractionResult:
        entities: dict[str, ExtractedEntity] = {}
        edges: list[ExtractedEdge] = []
        seen_edges: set[tuple[str, str, str]] = set()

        def ensure_entity(name: str, etype: str, attrs: dict[str, object]) -> None:
            key = canonicalize(name)
            if not key:
                return
            existing = entities.get(key)
            if existing is None or (existing.entity_type == "unknown" and etype != "unknown"):
                merged = dict(existing.attributes) if existing else {}
                merged.update(attrs)
                entities[key] = ExtractedEntity(
                    name=name.strip(), entity_type=etype.strip() or "unknown", attributes=merged
                )

        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            # Edge lines are matched FIRST: the arrow token unambiguously marks an edge,
            # and an edge endpoint can otherwise look like a bare name.
            edge_m = _EDGE_RE.match(line)
            if edge_m:
                src = edge_m.group("src").strip()
                dst = edge_m.group("dst").strip()
                rel = edge_m.group("rel").strip().upper().replace(" ", "_")
                ensure_entity(src, "unknown", {})
                ensure_entity(dst, "unknown", {})
                dedupe = (canonicalize(src), rel, canonicalize(dst))
                if dedupe not in seen_edges:
                    seen_edges.add(dedupe)
                    edges.append(ExtractedEdge(src_name=src, dst_name=dst, relationship_type=rel))
                continue
            node_m = _NODE_RE.match(line)
            if node_m and len(node_m.group("name").split()) <= MAX_NODE_NAME_WORDS:
                ensure_entity(
                    node_m.group("name"),
                    node_m.group("type"),
                    _parse_attrs(node_m.group("attrs")),
                )

        return ExtractionResult(entities=list(entities.values()), edges=edges)


# --- LLM-backed extraction (round 6 WP-G, F122) ----------------------------------------
#
# Real prose ("Our Platform team needs senior engineers who know Python") has no node
# lines for the grammar above to read. The LLM extractor infers typed nodes and edges
# from prose through the platform's own fast model, under STRICT JSON, with hard caps;
# the deterministic grammar is always merged in (explicit lines still win), and ANY
# failure — provider error, cap, malformed JSON — falls back to the deterministic
# result so ingest never fails or blocks on a vendor. Live mode only, opt-in by config.

MAX_LLM_ENTITIES = 40
MAX_LLM_EDGES = 80
MAX_LLM_TEXT_CHARS = 12_000
# Per-ingest cost bound (round 7 WP-4c #10, D10 turns the pass on for every live
# ingest): a source under MIN_LLM_TEXT_CHARS (a title, a one-line paste, a nav-only
# crawled page) has nothing a graph call would find that the grammar does not, so it
# costs no call; a large source is read as its first MAX_LLM_CHUNKS retrieval chunks,
# bounded again by MAX_LLM_TEXT_CHARS — one bounded fast-model call per ingest.
MIN_LLM_TEXT_CHARS = 80
MAX_LLM_CHUNKS = 8
# Ingest headroom (round 7 Phase 2 review, knowledge #1): the ingest call is admitted
# under the SAME per-tenant per-minute cap as the tenant's customer turns (§9 C9, item
# 4), and a sync ingests page after page with no pacing — a large sync could spend the
# whole minute and push a customer on the widget into the at-cap reply. So the pass is
# skipped (grammar-only, ``llm_skipped``; the sync re-extracts it next pass) once the
# tenant's current window already holds this fraction of the cap. The check READS the
# counter (never an INCR), so a skipped pass costs the tenant nothing, and ingest alone
# can never take more than this share: customer turns always keep the rest.
INGEST_HEADROOM_FRACTION = 0.5
_MAX_NAME = 200
_MAX_TYPE = 64
_MAX_ATTRS = 20


def llm_input_for(text: str) -> str | None:
    """The slice of an ingest the LLM pass reads, or ``None`` when the pass is skipped."""
    body = (text or "").strip()
    if len(body) < MIN_LLM_TEXT_CHARS:
        return None
    from app.knowledge.chunking import chunk_text  # noqa: PLC0415 — import cycle

    chunks = chunk_text(body)[:MAX_LLM_CHUNKS]
    return "\n\n".join(chunks)[:MAX_LLM_TEXT_CHARS] or None


_LLM_SYSTEM_PROMPT = (
    "You extract a knowledge graph from a business's own text. Reply with ONE JSON object "
    "and nothing else, shaped exactly like "
    '{"entities":[{"name":"...","type":"...","attributes":{"key":"value"}}],'
    '"edges":[{"source":"...","target":"...","type":"..."}]}. '
    "Take every type from the text's own vocabulary; there is no fixed list of types. "
    "Names are short noun phrases as written. Every edge source and target must be an "
    f"entity name from your list. At most {MAX_LLM_ENTITIES} entities and {MAX_LLM_EDGES} "
    "edges — prefer the ones a customer would ask about. No prose, no markdown fences. "
    "Write the JSON compactly on a single line: no indentation, no line breaks, no "
    "whitespace outside string values."
)

#: The graph call's output ceiling (round 7 walk run 8). The providers' 1024 default
#: truncated the JSON of every long page mid-object (a 1943-token /legal/terms stopped
#: at output_tokens=1024) and the answer read as unparseable; 4096 holds the capped
#: graph (``MAX_LLM_ENTITIES`` / ``MAX_LLM_EDGES``) with room to spare.
GRAPH_MAX_OUTPUT_TOKENS = 4096

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
#: Why an answer could not be read (the ``reason`` on ``graph_extract.llm_unparseable``).
UnparseableReason = Literal["no_json_object", "json_error"]


def graph_json(raw: str) -> tuple[dict[str, Any] | None, UnparseableReason | None]:
    """The model's JSON object (fence stripped, outermost ``{`` … ``}``) or why there is
    none: ``no_json_object`` (no braces at all), ``json_error`` (the span does not
    decode — a truncated answer lands here too). A span that decodes is an object: it
    starts at a ``{``."""
    body = _FENCE_RE.sub("", (raw or "").strip())
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        return None, "no_json_object"
    try:
        return json.loads(body[start : end + 1]), None
    except ValueError:
        return None, "json_error"


def parse_llm_graph(raw: str) -> ExtractionResult | None:
    """Strict, capped parse of the model's JSON → ExtractionResult; None when unusable."""
    data, _reason = graph_json(raw)
    if data is None:
        return None

    entities: dict[str, ExtractedEntity] = {}
    raw_entities = data.get("entities")
    for item in (raw_entities if isinstance(raw_entities, list) else [])[:MAX_LLM_ENTITIES]:
        if not isinstance(item, dict):
            continue
        name = " ".join(str(item.get("name") or "").split())[:_MAX_NAME]
        if not name:
            continue
        etype = " ".join(str(item.get("type") or "").split())[:_MAX_TYPE] or "unknown"
        attrs_raw = item.get("attributes")
        attrs: dict[str, object] = {}
        if isinstance(attrs_raw, dict):
            for k, v in list(attrs_raw.items())[:_MAX_ATTRS]:
                key = str(k).strip()[:_MAX_TYPE]
                if key and isinstance(v, str | int | float | bool):
                    attrs[key] = str(v)[:_MAX_NAME]
        entities[canonicalize(name)] = ExtractedEntity(
            name=name, entity_type=etype, attributes=attrs
        )

    edges: list[ExtractedEdge] = []
    seen: set[tuple[str, str, str]] = set()
    raw_edges = data.get("edges")
    for item in (raw_edges if isinstance(raw_edges, list) else [])[:MAX_LLM_EDGES]:
        if not isinstance(item, dict):
            continue
        src = " ".join(str(item.get("source") or "").split())[:_MAX_NAME]
        dst = " ".join(str(item.get("target") or "").split())[:_MAX_NAME]
        rel = " ".join(str(item.get("type") or "").split()).upper().replace(" ", "_")[:_MAX_TYPE]
        if not src or not dst or not rel:
            continue
        # A dangling endpoint is materialised as an ``unknown`` node so traversal never
        # dangles — but only while the entity cap holds; past it the edge is dropped.
        if any(
            canonicalize(endpoint) not in entities and len(entities) >= MAX_LLM_ENTITIES
            for endpoint in (src, dst)
        ):
            continue
        for endpoint in (src, dst):
            entities.setdefault(
                canonicalize(endpoint),
                ExtractedEntity(name=endpoint, entity_type="unknown"),
            )
        edge_key = (canonicalize(src), rel, canonicalize(dst))
        if edge_key in seen:
            continue
        seen.add(edge_key)
        edges.append(ExtractedEdge(src_name=src, dst_name=dst, relationship_type=rel))
    return ExtractionResult(entities=list(entities.values()), edges=edges)


def merge_results(base: ExtractionResult, extra: ExtractionResult) -> ExtractionResult:
    """Union of two extractions by canonical name / (src, rel, dst); ``base`` wins on type."""
    entities: dict[str, ExtractedEntity] = {e.canonical: e for e in base.entities}
    for ent in extra.entities:
        existing = entities.get(ent.canonical)
        if existing is None:
            entities[ent.canonical] = ent
        elif existing.entity_type == "unknown" and ent.entity_type != "unknown":
            entities[ent.canonical] = ExtractedEntity(
                name=existing.name,
                entity_type=ent.entity_type,
                attributes={**ent.attributes, **existing.attributes},
            )
    edges: list[ExtractedEdge] = list(base.edges)
    seen = {
        (canonicalize(e.src_name), e.relationship_type, canonicalize(e.dst_name)) for e in edges
    }
    for edge in extra.edges:
        key = (canonicalize(edge.src_name), edge.relationship_type, canonicalize(edge.dst_name))
        if key not in seen:
            seen.add(key)
            edges.append(edge)
    return ExtractionResult(entities=list(entities.values()), edges=edges)


class LlmGraphExtractor:
    """Prose → typed nodes/edges through the platform fast model (F122).

    ``extract`` (the sync Protocol) stays deterministic; ingest calls ``aextract``
    through ``extract_async``. ``router_factory`` is the seam tests bind a fake to."""

    def __init__(self, router_factory: Callable[[], Any] | None = None) -> None:
        self._router_factory = router_factory
        self._router: Any = None
        self._fallback = DeterministicGraphExtractor()

    def _router_or_build(self) -> Any:
        if self._router is None:
            if self._router_factory is not None:
                self._router = self._router_factory()
            else:
                from app.config import get_settings  # noqa: PLC0415 — import cycle
                from app.runtime.provider_factory import build_llm_router  # noqa: PLC0415

                self._router = build_llm_router(get_settings())
        return self._router

    def extract(self, text: str) -> ExtractionResult:
        return self._fallback.extract(text)

    async def aextract(self, text: str, *, admin_id: uuid.UUID | None = None) -> ExtractionResult:
        """``admin_id`` is the ingesting tenant (round 7 WP-2, item 4): the extractor is
        a process singleton built without one, so the call itself carries it and the
        router admits it against THAT tenant's per-minute cap (§9 C9) — an owner
        pasting a large document is capped exactly like their customers' turns."""
        base = self._fallback.extract(text)
        clipped = llm_input_for(text)
        if clipped is None:
            return base
        from app.runtime.llm_cap import LLMCapExceeded  # noqa: PLC0415 — import cycle

        if admin_id is not None and not ingest_has_headroom(admin_id):
            log.info("graph_extract.llm_skipped_headroom", extra={"admin_id": str(admin_id)})
            return replace(base, llm_skipped=True)
        try:
            resp = await self._router_or_build().complete(
                _LLM_SYSTEM_PROMPT,
                [{"role": "user", "content": clipped}],
                requires_tools=False,
                context_tokens=0,
                complexity=0.0,
                admin_id=admin_id,
                max_output_tokens=GRAPH_MAX_OUTPUT_TOKENS,
            )
        except Exception as exc:  # noqa: BLE001 — never let a vendor break ingest
            log.warning("graph_extract.llm_failed", extra={"error": type(exc).__name__})
            # A cap refusal (customer turns filled the window between the headroom
            # read and the admission) is the same budget skip: re-extract next pass.
            return replace(base, llm_skipped=isinstance(exc, LLMCapExceeded))
        raw = getattr(resp, "text", "") or ""
        parsed = parse_llm_graph(raw)
        if parsed is not None:
            return merge_results(base, parsed)
        # An unusable answer is NOT a skip, truncated or not: the same page would earn it
        # again next pass, so re-extracting would cost an LLM call and a re-embed on every
        # sync and report the page "updated" with nothing changed. The grammar-only graph
        # is kept and the revision marker bumps; the graph catches up when the page changes.
        if getattr(resp, "truncated", False):
            usage = getattr(resp, "usage", None)
            log.warning(
                "graph_extract.llm_truncated",
                extra={"output_tokens": getattr(usage, "output_tokens", None)},
            )
            return base
        _data, reason = graph_json(raw)
        log.warning("graph_extract.llm_unparseable", extra={"reason": reason or "json_error"})
        return base


def ingest_has_headroom(admin_id: uuid.UUID) -> bool:
    """True while the tenant's current cap window leaves the ingest pass its share.

    ``current_count`` is a read (a Redis GET in live), never an admission, so asking
    costs the tenant nothing."""
    from app.runtime.llm_cap import LLM_CAP_PER_MINUTE, get_llm_cap_store  # noqa: PLC0415

    threshold = int(LLM_CAP_PER_MINUTE * INGEST_HEADROOM_FRACTION)
    return get_llm_cap_store().current_count(str(admin_id)) < threshold


async def extract_async(
    extractor: GraphExtractor, text: str, *, admin_id: uuid.UUID | None = None
) -> ExtractionResult:
    """Run an extractor from async ingest: ``aextract`` when it has one, else ``extract``.

    ``admin_id`` (the ingesting tenant) reaches an LLM-backed extractor so its model
    call is capped under that tenant (item 4); the deterministic extractor ignores it."""
    afn: Callable[..., Awaitable[ExtractionResult]] | None = getattr(extractor, "aextract", None)
    if afn is not None:
        return await afn(text, admin_id=admin_id)
    return extractor.extract(text)


_default: GraphExtractor | None = None


def get_extractor() -> GraphExtractor:
    """Return the bound extractor (§3.2.1).

    ``LUCIEL_GRAPH_EXTRACTOR`` decides: ``llm`` binds the LLM-backed extractor — only
    where the LLM is real (live mode, or the local stack's ``llm_posture="live"``), so
    mock/CI never reaches a vendor — behind this same seam. Unset, the config resolves it
    by posture (D10): ``llm`` where the LLM is real, ``deterministic`` elsewhere.
    The ingest path depends only on the GraphExtractor Protocol.
    """
    global _default
    if _default is None:
        from app.config import get_settings, real_ai  # noqa: PLC0415 — import cycle

        if get_settings().graph_extractor == "llm" and real_ai():
            _default = LlmGraphExtractor()
        else:
            _default = DeterministicGraphExtractor()
    return _default


def reset_extractor() -> None:
    """Forget the bound extractor (tests; settings changed)."""
    global _default
    _default = None
