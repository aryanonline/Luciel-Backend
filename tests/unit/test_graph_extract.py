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


# --- Node grammar tightened (round 7 WP-4c #10, D10) --------------------------------
# With the LLM extractor on in live posture the grammar runs over prose too. The old
# pattern read ANY parenthesised aside as a node line.

PROSE_WITH_ASIDES = """
We are open Monday to Friday (except holidays) and Saturdays by appointment.
Call us (we're friendly) or book online.
Prices vary by season (see below).
Monday to Friday (closed Sunday)
Open daily (9-5)
Our team (three dentists and two hygienists) is here to help.
Opening hours (weekdays): 9am to 5pm
"""


def test_parenthesised_prose_is_not_parsed_as_nodes():
    res = DeterministicGraphExtractor().extract(PROSE_WITH_ASIDES)
    assert res.entities == [] and res.edges == []


def test_explicit_node_lines_still_parse_after_the_tightening():
    text = "\n".join(
        [
            "Dr. Alice Smith (Dentist)",
            "Teeth Whitening (Dental Service): price=$200; duration=45min",
            "Gore-Tex (Feature)",
            "Platform (Department)",
            "Widget Pro 3000 (Product): sku=WP-3000",
        ]
    )
    res = DeterministicGraphExtractor().extract(text)
    by_canon = {e.canonical: e for e in res.entities}
    assert by_canon["dr. alice smith"].entity_type == "Dentist"
    assert by_canon["teeth whitening"].entity_type == "Dental Service"
    assert by_canon["teeth whitening"].attributes == {"price": "$200", "duration": "45min"}
    assert by_canon["gore-tex"].entity_type == "Feature"
    assert by_canon["platform"].entity_type == "Department"
    assert by_canon["widget pro 3000"].attributes == {"sku": "WP-3000"}


def test_a_name_longer_than_the_word_cap_is_prose():
    res = DeterministicGraphExtractor().extract("The best clinic in the whole city (Clinic)")
    assert res.entities == []
    res = DeterministicGraphExtractor().extract("Best Clinic In Town (Clinic)")
    assert [e.name for e in res.entities] == ["Best Clinic In Town"]


def test_a_colon_tail_without_key_values_is_prose():
    res = DeterministicGraphExtractor().extract("Opening hours (weekdays): 9am to 5pm")
    assert res.entities == []
