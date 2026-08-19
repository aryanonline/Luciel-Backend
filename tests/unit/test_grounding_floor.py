"""Unit tests for the grounding floor gate (Architecture §3.4.13 / §3.4.5).

Harmony fix 2026-08-04, item 3: ``evaluate_outcome`` used to require BOTH
``plan_confidence < 0.6`` AND ``grounding_score < 0.50`` to fire cannot_answer.
The live Anthropic/OpenAI providers (app/runtime/providers.py) hardcode
``confidence=1.0`` for every real response -- there is no actual self-reported
confidence signal in production -- so the AND was permanently unsatisfiable on
the confidence side: a confidently fabricated, completely ungrounded answer
(grounding_score == 0.0) would never escalate. These tests pin the fixed
behaviour: grounding below floor is sufficient on its own (unless a resolving
tool succeeded), and a genuinely low confidence signal is still an independent
trigger.
"""

from __future__ import annotations

from app.runtime.escalation import Signal, evaluate_outcome
from app.runtime.grounding import GROUNDING_FLOOR, score_grounding


def test_confident_but_ungrounded_answer_still_escalates() -> None:
    """The exact bug: confidence=1.0 (what every live provider call reports today)
    with a completely ungrounded answer (no retrieval, no tool) must escalate. Before
    the fix, plan_confidence=1.0 short-circuited the AND and this fired False."""
    decision = evaluate_outcome(
        message="What's your return policy for electronics?",
        plan_confidence=1.0,
        grounding_score=0.0,
        resolving_tool_succeeded=False,
    )
    assert decision.fired is True
    assert decision.signal is Signal.cannot_answer


def test_grounded_tool_backed_answer_does_not_escalate() -> None:
    decision = evaluate_outcome(
        message="What's my appointment time?",
        plan_confidence=1.0,
        grounding_score=1.0,
        resolving_tool_succeeded=True,
    )
    assert decision.fired is False


def test_grounded_knowledge_answer_above_floor_does_not_escalate() -> None:
    decision = evaluate_outcome(
        message="What are your hours?",
        plan_confidence=1.0,
        grounding_score=0.75,
        resolving_tool_succeeded=False,
    )
    assert decision.fired is False


def test_low_confidence_alone_still_escalates_even_if_grounded() -> None:
    """Confidence remains an independent OR trigger: if a provider ever supplies a
    real low-confidence signal (or a test injects one via EchoModel), that alone must
    still be able to escalate even when grounding happens to clear the floor."""
    decision = evaluate_outcome(
        message="Some question",
        plan_confidence=0.2,
        grounding_score=0.9,
        resolving_tool_succeeded=False,
    )
    assert decision.fired is True
    assert decision.signal is Signal.cannot_answer


def test_resolving_tool_success_overrides_everything() -> None:
    """A resolving tool success is grounds enough on its own, regardless of how low
    confidence or grounding_score happen to be (e.g. a tool response containing terms
    the token-overlap proxy doesn't recognize)."""
    decision = evaluate_outcome(
        message="Some question",
        plan_confidence=0.0,
        grounding_score=0.0,
        resolving_tool_succeeded=True,
    )
    assert decision.fired is False


def test_score_grounding_no_retrieval_scores_far_below_floor() -> None:
    """A no-KB answer (retriever found nothing) must not land at some ambiguous
    near-floor value -- it's a hard 0.0, unambiguously below GROUNDING_FLOOR."""
    result = score_grounding("Sure, we offer a lifetime warranty on all products.", [], False)
    assert result.score == 0.0
    assert result.grounded is False
    assert result.reason == "no_retrieval"
    assert result.score < GROUNDING_FLOOR


def test_score_grounding_kb_backed_answer_scores_above_floor() -> None:
    """A knowledge-backed answer whose tokens are actually supported by the retrieved
    chunks must clear the floor."""
    chunks = [
        "Our return policy allows returns within 30 days of purchase with a receipt.",
        "Electronics have a 15 day return window due to manufacturer restrictions.",
    ]
    answer = "Electronics have a 15 day return window per our policy."
    result = score_grounding(answer, chunks, False)
    assert result.score >= GROUNDING_FLOOR
    assert result.grounded is True


def test_score_grounding_tool_result_is_always_fully_grounded() -> None:
    result = score_grounding("Your appointment is at 3pm Tuesday.", [], True)
    assert result.score == 1.0
    assert result.grounded is True
    assert result.reason == "tool_result"


def test_verbatim_kb_answer_with_live_conversational_wrapper_scores_above_floor() -> None:
    """LIVE-VERIFIED (Harmony wave 2, item 2b): a REAL production LLM answer was
    observed scoring 0.36 -- below the 0.50 floor -- despite the factual claim
    being copied VERBATIM from a retrieved KB chunk. The previous fix's test
    (test_score_grounding_kb_backed_answer_scores_above_floor, above) only ever
    exercised a bare, unwrapped factual sentence and never modeled how a real
    LLM actually talks: it wraps the quoted fact in ordinary conversational
    connective tissue ("Hi there!", "Thanks for reaching out.", "Is there
    anything else I can help you with today?"). None of that filler carries a
    factual claim the KB could ever "support", so normalizing by EVERY answer
    token (the old behavior) diluted a fully-grounded answer's score below the
    floor. This is the live condition that broke the scorer/floor agreement:
    reproduced here with the same shape of wrapper that caused the 0.36 score.
    """
    chunk = "Standard shipping takes 5 to 7 business days."
    answer = (
        "Hi there! Thanks for reaching out. To answer your question: standard "
        "shipping takes 5 to 7 business days. Is there anything else I can help "
        "you with today?"
    )
    result = score_grounding(answer, [chunk], False)
    assert result.score >= GROUNDING_FLOOR
    assert result.grounded is True


def test_fabricated_answer_still_scores_at_or_near_zero_despite_wrapper() -> None:
    """The stopword/filler exclusion (item 2b fix) must not become a loophole: a
    genuinely fabricated claim wrapped in the SAME kind of conversational filler
    must still score far below the floor, because its substantive claim tokens
    still have no support in the retrieved chunks."""
    irrelevant_chunk = "Our store hours are 9am to 6pm Monday through Friday."
    answer = (
        "Hi there! Thanks for reaching out. To answer your question: we offer "
        "a lifetime warranty on all products. Is there anything else I can help "
        "you with today?"
    )
    result = score_grounding(answer, [irrelevant_chunk], False)
    assert result.score < GROUNDING_FLOOR
    assert result.grounded is False


def test_conversational_pleasantry_with_zero_grounding_does_not_escalate() -> None:
    """Live-caught 2026-08-18: the caller declining Luciel's follow-up offer ("No its
    alright thankyou") scored 0.0 grounding — a social turn retrieves nothing — and
    escalated as cannot_answer. A conversational turn has no factual claim for the
    floor to gate, so it must not fire."""
    decision = evaluate_outcome(
        message="No its alright thankyou",
        plan_confidence=1.0,
        grounding_score=0.0,
        resolving_tool_succeeded=False,
    )
    assert decision.fired is False


def test_conversational_skip_never_weakens_the_or_gate_for_questions() -> None:
    """The classifier narrows WHICH turns count as answers; for a real question the
    grounding OR-gate is unchanged (doctrine: never an AND). Sits deliberately beside
    test_confident_but_ungrounded_answer_still_escalates."""
    decision = evaluate_outcome(
        message="how much is a water heater",
        plan_confidence=1.0,
        grounding_score=0.0,
        resolving_tool_succeeded=False,
    )
    assert decision.fired is True
    assert decision.signal is Signal.cannot_answer
