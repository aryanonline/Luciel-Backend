"""Query-intent classifier — the PLAN-phase retrieval-router gate (Architecture §3.2.1).

§3.2.1 routing rule (Locked Decision §7 #6): the graph is invoked ONLY when the query is a
structured-filter-intent query — a **multi-attribute intersection**, a **relationship
traversal**, or a **membership check**. Pure-semantic queries bypass the graph and go to
vector retrieval only; mixing graph into a pure-semantic query adds retrieval noise with no
accuracy benefit. This module is the deterministic gate that decides which path a query takes.

It is intentionally a transparent heuristic (not an LLM call) so the routing decision is
deterministic and unit-testable; production MAY additionally consult the LLM router, but the
deterministic path here is the contract the §3.2.1 test asserts (pure-semantic → no graph).
"""

from __future__ import annotations

import re
from enum import Enum


class QueryIntent(str, Enum):
    semantic = "semantic"
    structured_filter_intent = "structured_filter_intent"


# Surface markers of the three structured shapes named in §3.2.1. These are STRUCTURAL
# connective words (intersection / traversal / membership grammar), NOT a vertical ontology
# — they say nothing about real estate vs. medical vs. retail, so the gate stays
# domain-agnostic (§3.2.1). Multi-attribute intersection: conjunctions joining constraints.
_INTERSECTION = re.compile(
    r"\b(and|both|as well as|with|that (?:also|have|has|are)|which (?:also|have|has))\b",
    re.IGNORECASE,
)
# Relationship traversal: words describing edges/links between things. Both active
# ("requires", "offers") and passive ("offered by", "required by") edge phrasings count —
# they all denote a relationship the graph can traverse (§3.2.1 relationship traversal).
_TRAVERSAL = re.compile(
    r"\b(related to|connected to|associated with|requires?|required by|offers?|offered by|"
    r"provided by|belongs? to|part of|linked to|depends? on|reports? to|leads? to|"
    r"maps? to|treats?|specializes? in)\b",
    re.IGNORECASE,
)
# Membership check: "which X are in / belong to Y", set-membership phrasing.
_MEMBERSHIP = re.compile(
    r"\b(which|what|list|all|any)\b.*\b(in|under|within|among|part of|belong)\b",
    re.IGNORECASE,
)


def classify_intent(query: str) -> QueryIntent:
    """Classify a query as semantic vs structured_filter_intent (§3.2.1 routing gate).

    Returns structured_filter_intent iff the query exhibits at least one of the three
    §3.2.1 structured shapes (multi-attribute intersection / relationship traversal /
    membership). Otherwise semantic — and the hybrid retriever will NOT engage the graph."""
    q = query.strip()
    if not q:
        return QueryIntent.semantic
    if _TRAVERSAL.search(q):
        return QueryIntent.structured_filter_intent
    if _MEMBERSHIP.search(q):
        return QueryIntent.structured_filter_intent
    # Intersection only counts when it joins multiple content terms (a single "and" inside
    # a pure-semantic sentence is not a structured filter). Require >= 2 capitalized/quoted
    # terms or comma-separated constraints alongside the conjunction.
    if _INTERSECTION.search(q):
        terms = _candidate_terms(q)
        if len(terms) >= 2:
            return QueryIntent.structured_filter_intent
    return QueryIntent.semantic


def _candidate_terms(query: str) -> list[str]:
    """Heuristic content terms used both for intersection detection and graph seeding.

    Pulls quoted spans and Capitalized words (proper-noun-ish entity surface forms). This
    is deliberately simple and deterministic — it is a seed hint for the graph, not a
    parser; the graph itself only matches terms that were actually ingested (§3.2)."""
    quoted = re.findall(r'"([^"]+)"', query)
    capitalized = re.findall(r"\b([A-Z][a-zA-Z0-9]+(?:\s+[A-Z][a-zA-Z0-9]+)*)\b", query)
    seen: set[str] = set()
    out: list[str] = []
    for term in [*quoted, *capitalized]:
        key = term.lower()
        if key not in seen:
            seen.add(key)
            out.append(term)
    return out


def seed_terms(query: str) -> list[str]:
    """Public accessor for the graph seed terms derived from a query (§3.2.1)."""
    return _candidate_terms(query)
