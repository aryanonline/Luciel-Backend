"""Query-intent classifier — the §3.2.1 retrieval-router gate.

§3.2.1: the graph is invoked ONLY for structured-filter-intent queries (multi-attribute
intersection / relationship traversal / membership). Pure-semantic queries must classify
as semantic so the hybrid retriever does NOT engage the graph (the no-noise rule).
"""

from __future__ import annotations

import pytest
from app.knowledge.intent import QueryIntent, classify_intent, seed_terms


@pytest.mark.parametrize(
    "query",
    [
        "What are your opening hours?",
        "Tell me about your refund policy",
        "How do I reset my password",
        "",
    ],
)
def test_pure_semantic_queries_classified_semantic(query):
    assert classify_intent(query) is QueryIntent.semantic


@pytest.mark.parametrize(
    "query",
    [
        "Which roles require Python?",  # relationship traversal
        "What services are offered by Dr. Lee?",  # traversal
        "List all products in the Footwear category",  # membership
        "Show me a Listing with a Garden and a Garage",  # multi-attribute intersection
    ],
)
def test_structured_queries_engage_graph(query):
    assert classify_intent(query) is QueryIntent.structured_filter_intent


def test_seed_terms_extracts_capitalized_and_quoted():
    terms = seed_terms('Which roles require "Python" and Kubernetes?')
    lowered = {t.lower() for t in terms}
    assert "python" in lowered
    assert "kubernetes" in lowered
