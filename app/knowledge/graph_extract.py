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

import re
from dataclasses import dataclass, field
from typing import Protocol


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


def canonicalize(name: str) -> str:
    """Lower-case, collapse whitespace — the normalized node identity (§3.2.1)."""
    return re.sub(r"\s+", " ", name.strip().lower())


class GraphExtractor(Protocol):
    """Pluggable extractor interface (§3.2.1). Production = LLM-backed; tests = deterministic."""

    def extract(self, text: str) -> ExtractionResult: ...


# Node:  ``Name (Type): k=v; k=v``  — the attribute tail is optional.
_NODE_RE = re.compile(
    r"^\s*(?P<name>[^()\n]+?)\s*\((?P<type>[^()\n]+?)\)\s*(?::\s*(?P<attrs>.*))?$"
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
            if node_m:
                ensure_entity(
                    node_m.group("name"),
                    node_m.group("type"),
                    _parse_attrs(node_m.group("attrs")),
                )

        return ExtractionResult(entities=list(entities.values()), edges=edges)


_default: GraphExtractor | None = None


def get_extractor() -> GraphExtractor:
    """Return the bound extractor. Defaults to the deterministic one (§3.2.1).

    Production may swap in an LLM-backed extractor via config behind this same seam —
    the ingest path depends only on the GraphExtractor Protocol, never the concrete type.
    """
    global _default
    if _default is None:
        _default = DeterministicGraphExtractor()
    return _default
