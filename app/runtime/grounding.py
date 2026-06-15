"""Answer grounding & anti-hallucination (DOCTRINE ANCHOR — Architecture §3.4.13).

Grounding floor is 0.50, uniform across all accounts (Architecture §9 #18).
Below floor with no resolving tool → cannot_answer escalation. Faithful-readback:
Luciel may only state what a tool/result actually contains. Lands in Arc 14.
"""

from __future__ import annotations

from dataclasses import dataclass

GROUNDING_FLOOR = 0.50


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
    import re

    def toks(s: str) -> set[str]:
        return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if len(w) > 2}

    answer_tokens = toks(answer)
    if not answer_tokens:
        return GroundingResult(0.0, False, "no_content_tokens")
    support = set()
    for chunk in retrieved_chunks:
        support |= toks(chunk)
    overlap = len(answer_tokens & support) / len(answer_tokens)
    return GroundingResult(overlap, overlap >= GROUNDING_FLOOR, "knowledge_overlap")


def faithful_readback_ok(answer: str, allowed_fields: dict[str, object]) -> bool:
    """Faithful-readback (§3.4.13): Luciel may only state fields a tool result actually
    contains. A naive but real guard: every numeric/value token claimed in the answer
    that looks like a confirming field must be present in allowed_fields' values.
    """
    import re

    claimed_numbers = set(re.findall(r"\b\d[\d,\.]*\b", answer))
    allowed_numbers: set[str] = set()
    for v in allowed_fields.values():
        allowed_numbers |= set(re.findall(r"\b\d[\d,\.]*\b", str(v)))
    # Any claimed number not present in the allowed fields is a faithful-readback breach.
    return claimed_numbers.issubset(allowed_numbers) if claimed_numbers else True
