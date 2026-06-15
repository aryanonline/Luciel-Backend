"""Deterministic, domain-agnostic graph extraction (Architecture §3.2.1).

The extractor must read entity_type / relationship_type FROM the text and impose NO
vertical ontology. These tests feed it two unrelated verticals (recruiting + retail) and
assert it produces the right typed nodes/edges for BOTH with the identical code path —
proving there is no hardcoded vertical keyword table (§3.2.1 domain-agnostic commitment).
"""

from __future__ import annotations

from app.knowledge.graph_extract import (
    DeterministicGraphExtractor,
    canonicalize,
)

RECRUITING = """
Senior Engineer (Role): department=Platform
Python (Skill)
Senior Engineer -REQUIRES-> Python
Platform (Department)
Platform -HAS-> Senior Engineer
"""

RETAIL = """
Trail Runner (Product): price=120
Footwear (Category)
Trail Runner -IN-> Footwear
Gore-Tex (Feature)
Trail Runner -HAS-> Gore-Tex
"""


def test_extracts_typed_nodes_and_directed_edges():
    res = DeterministicGraphExtractor().extract(RECRUITING)
    by_canon = {e.canonical: e for e in res.entities}
    assert by_canon["senior engineer"].entity_type == "Role"
    assert by_canon["senior engineer"].attributes == {"department": "Platform"}
    assert by_canon["python"].entity_type == "Skill"
    rels = {(e.src_name, e.relationship_type, e.dst_name) for e in res.edges}
    assert ("Senior Engineer", "REQUIRES", "Python") in rels
    assert ("Platform", "HAS", "Senior Engineer") in rels


def test_domain_agnostic_same_codepath_other_vertical():
    # Same extractor, a totally different vertical — types/relations come from the TEXT,
    # not a real-estate/recruiting keyword table (§3.2.1). No ontology is imposed.
    res = DeterministicGraphExtractor().extract(RETAIL)
    by_canon = {e.canonical: e for e in res.entities}
    assert by_canon["trail runner"].entity_type == "Product"
    assert by_canon["footwear"].entity_type == "Category"
    assert by_canon["gore-tex"].entity_type == "Feature"
    rels = {(e.relationship_type) for e in res.edges}
    # Relationship labels are whatever the source said — IN / HAS here, REQUIRES above.
    assert rels == {"IN", "HAS"}


def test_edge_endpoint_without_node_line_is_materialized():
    # An arrow endpoint that never appeared as a node line still becomes an entity so
    # traversal never dangles (entity_type defaults to 'unknown').
    res = DeterministicGraphExtractor().extract("Alpha -LINKS-> Beta")
    canon = {e.canonical: e.entity_type for e in res.entities}
    assert canon == {"alpha": "unknown", "beta": "unknown"}
    assert len(res.edges) == 1


def test_canonicalize_normalizes_case_and_space():
    assert canonicalize("  Senior   Engineer ") == "senior engineer"


def test_duplicate_edges_deduped():
    res = DeterministicGraphExtractor().extract("A -REL-> B\nA -REL-> B")
    assert len(res.edges) == 1
