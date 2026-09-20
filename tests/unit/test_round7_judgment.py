"""Luciel's judgment as an employee — deterministic half (round 7 WP-2, D2 2026-09-14).

Gate 2 judges the answer AND the opportunity on every turn: a hot lead whose draft is
below the floor carries both verdicts on one decision (item 1). It used to return the
lead signal first and never look at the answer, so a fabricated reply to the hottest
lead of the week shipped verbatim.
"""

from __future__ import annotations

from app.runtime.escalation import Signal, evaluate_outcome

HOT_LEAD = "I have a $5000 budget and want to book today"


def test_a_hot_lead_with_an_ungrounded_answer_carries_both_verdicts() -> None:
    d = evaluate_outcome(message=HOT_LEAD, plan_confidence=1.0, grounding_score=0.1)
    assert d.fired and d.signal is Signal.high_value_lead
    # The answer-quality verdict is explicit, not inferred from the signal.
    assert d.answer_ungrounded is True
    assert d.answer_reasons == ("answer only 10% backed by your knowledge",)
    # The row's reasons carry both: why the lead is hot AND why the draft never left.
    assert d.reasons == (
        "named a budget",
        "wants it soon",
        "ready to buy or book",
        "answer only 10% backed by your knowledge",
    )


def test_a_grounded_hot_lead_has_a_clean_answer_verdict() -> None:
    d = evaluate_outcome(message=HOT_LEAD, plan_confidence=1.0, grounding_score=0.9)
    assert d.fired and d.signal is Signal.high_value_lead
    assert d.answer_ungrounded is False
    assert d.answer_reasons == ()
    assert d.reasons == ("named a budget", "wants it soon", "ready to buy or book")


def test_a_plain_ungrounded_answer_is_still_cannot_answer_with_the_verdict_set() -> None:
    d = evaluate_outcome(
        message="Do you offer a warranty?", plan_confidence=1.0, grounding_score=0.0
    )
    assert d.fired and d.signal is Signal.cannot_answer
    assert d.answer_ungrounded is True
    assert d.reasons == d.answer_reasons == ("answer only 0% backed by your knowledge",)
    # A resolving tool clears the verdict for the lead too (the OR-gate is untouched).
    tool_backed = evaluate_outcome(
        message=HOT_LEAD, plan_confidence=1.0, grounding_score=0.0, resolving_tool_succeeded=True
    )
    assert tool_backed.signal is Signal.high_value_lead
    assert tool_backed.answer_ungrounded is False


def test_a_low_confidence_hot_lead_is_judged_the_same_way() -> None:
    d = evaluate_outcome(message=HOT_LEAD, plan_confidence=0.3, grounding_score=0.9)
    assert d.signal is Signal.high_value_lead
    assert d.answer_ungrounded is True
    assert d.answer_reasons == ("the model was 30% sure",)
