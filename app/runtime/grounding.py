"""Answer grounding & anti-hallucination (DOCTRINE ANCHOR — Architecture §3.4.13).

Grounding floor is 0.50, uniform across all accounts (Architecture §9 #18).
Below floor with no resolving tool → cannot_answer escalation. Faithful-readback:
Luciel may only state what a tool/result actually contains. Lands in Arc 14.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

GROUNDING_FLOOR = 0.50

# Harmony wave 2, item 2b (LIVE-VERIFIED): a genuinely verbatim-KB-backed answer
# scored 0.36 -- below the floor -- despite the model having copied the retrieved
# fact word-for-word. Root cause: the token-overlap proxy normalized by EVERY
# answer token, including ordinary conversational connective/filler words a real
# LLM adds around a quoted fact ("Hi there!", "Thanks for reaching out.", "Is
# there anything else I can help you with?"). None of those filler tokens can
# ever be "supported" by a knowledge chunk -- they carry no factual claim -- so
# their mere presence diluted the score of an answer that was, in fact, fully
# grounded. The previous fix's test (test_score_grounding_kb_backed_answer_
# scores_above_floor) only ever exercised a bare, unwrapped factual sentence and
# never modeled a live LLM's actual conversational phrasing, so it never caught
# this. Fix: exclude common stopwords/conversational connectives from the
# ANSWER-side denominator only -- a fabricated claim still scores at or near 0.0
# because its substantive (non-stopword) tokens still have to be found in the
# retrieved chunks; only claim-free filler is no longer penalized.
_GROUNDING_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "but",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "to",
        "of",
        "in",
        "on",
        "at",
        "for",
        "with",
        "about",
        "as",
        "by",
        "from",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "you",
        "your",
        "yours",
        "i",
        "me",
        "my",
        "we",
        "our",
        "us",
        "he",
        "she",
        "they",
        "them",
        "their",
        "there",
        "here",
        "what",
        "which",
        "who",
        "whom",
        "will",
        "would",
        "can",
        "could",
        "should",
        "shall",
        "may",
        "might",
        "do",
        "does",
        "did",
        "have",
        "has",
        "had",
        "not",
        "no",
        "yes",
        "if",
        "then",
        "than",
        "so",
        "just",
        "also",
        "please",
        "thanks",
        "thank",
        "sure",
        "hi",
        "hello",
        "hey",
        "let",
        "know",
        "any",
        "other",
        "questions",
        "question",
        "help",
        "today",
        "else",
        "anything",
        "out",
        "reaching",
        "checked",
    }
)


@dataclass
class GroundingResult:
    score: float
    grounded: bool
    reason: str


def score_grounding(
    answer: str, retrieved_chunks: list[str], tool_succeeded: bool
) -> GroundingResult:
    """Estimate how well an answer is grounded in retrieved knowledge / tool results.

    A tool that returned `ok` grounds the answer in a live result (score 1.0). For
    knowledge-only answers, grounding is the fraction of answer tokens supported by
    the retrieved chunks (a deterministic, dependency-free proxy for the production
    grounding model). Below the 0.50 floor with no resolving tool => not grounded
    => cannot_answer escalation (§3.4.13 / §3.4.5).
    """
    if tool_succeeded:
        return GroundingResult(1.0, True, "tool_result")
    if not answer.strip():
        return GroundingResult(0.0, False, "empty_answer")
    if not retrieved_chunks:
        return GroundingResult(0.0, False, "no_retrieval")

    def toks(s: str, *, drop_stopwords: bool) -> set[str]:
        words = (w for w in re.findall(r"[a-z0-9]+", s.lower()) if len(w) > 2)
        if drop_stopwords:
            return {w for w in words if w not in _GROUNDING_STOPWORDS}
        return set(words)

    # Only the answer side drops stopwords/filler -- claim-free connective tissue
    # a real LLM wraps around a quoted fact must not dilute the score. The support
    # side (retrieved chunks) keeps every token so a stopword that happens to be
    # part of the substantive claim (rare, but harmless either way) still counts.
    answer_tokens = toks(answer, drop_stopwords=True)
    if not answer_tokens:
        return GroundingResult(0.0, False, "no_content_tokens")
    support = set()
    for chunk in retrieved_chunks:
        support |= toks(chunk, drop_stopwords=False)
    overlap = len(answer_tokens & support) / len(answer_tokens)
    return GroundingResult(overlap, overlap >= GROUNDING_FLOOR, "knowledge_overlap")


def faithful_readback_ok(answer: str, allowed_fields: dict[str, object]) -> bool:
    """Faithful-readback (§3.4.13): Luciel may only state fields a tool result actually
    contains. A naive but real guard: every numeric/value token claimed in the answer
    that looks like a confirming field must be present in allowed_fields' values.

    The token pattern deliberately has NO trailing word boundary and includes ":" —
    "booked for 3pm" must surface the claimed "3" (digit→letter is not a regex word
    boundary, so a trailing ``\\b`` silently missed exactly the time-of-day drift this
    guard exists for), and "14:00" reads as one token on both sides. Identifier-embedded
    digits ("ref77") stay unclaimed: no boundary precedes them.
    """
    import re

    token = r"\b\d[\d,\.:]*"
    claimed_numbers = set(re.findall(token, answer))
    allowed_numbers: set[str] = set()
    for v in allowed_fields.values():
        allowed_numbers |= set(re.findall(token, str(v)))
    # Any claimed number not present in the allowed fields is a faithful-readback breach.
    return claimed_numbers.issubset(allowed_numbers) if claimed_numbers else True
