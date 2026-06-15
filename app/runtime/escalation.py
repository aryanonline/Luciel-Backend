"""Escalation judgment module — the four fixed signals at two gates (Architecture §3.4.5).

Luciel decides WHEN to escalate; the admin never configures triggers (only who/how
is routed). The four signals are fixed and never admin-configurable (Locked
Decision §7 #16 only allows extending the high-value-lead heuristic via the
business-context field, never adding/removing signals).

Gate 1 — intake (pre-PLAN), knowable from the inbound message alone:
  - explicit_human_request  (intent_class == request_human, conf >= 0.85)
  - strong_negative_sentiment (<= -0.7 across >= 2 of trailing 3 messages)
Gate 2 — outcome (post-REFLECT), needs the loop's output:
  - cannot_answer (plan confidence < 0.6 AND grounding < floor 0.50)
  - high_value_lead (normalized lead-score; budget 0.5 + time-pressure 0.3 +
    purchase/booking intent 0.4, capped at 1.0)

Classifiers (intent/sentiment) are pluggable; deterministic heuristics here exercise
the gates without an LLM. The thresholds are platform constants (never admin-set).
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass

from app.runtime.grounding import GROUNDING_FLOOR


class Signal(str, enum.Enum):
    explicit_human_request = "explicit_human_request"
    strong_negative_sentiment = "strong_negative_sentiment"
    cannot_answer = "cannot_answer"
    high_value_lead = "high_value_lead"


class Gate(str, enum.Enum):
    intake = "intake"
    outcome = "outcome"


REQUEST_HUMAN_CONF = 0.85
NEGATIVE_SENTIMENT_THRESHOLD = -0.7
CANNOT_ANSWER_CONFIDENCE = 0.6


@dataclass
class EscalationDecision:
    fired: bool
    signal: Signal | None = None
    gate: Gate | None = None
    score_or_confidence: float | None = None


# --- pluggable classifiers (deterministic heuristics for dev/test) ---------- #
_HUMAN_PATTERNS = re.compile(
    r"\b(speak|talk|connect)\b.{0,20}\b(human|person|someone|representative|agent|real)\b"
    r"|\bhuman\b|\breal person\b|\bspeak to a\b",
    re.IGNORECASE,
)
_NEGATIVE_WORDS = {
    "angry",
    "furious",
    "terrible",
    "awful",
    "useless",
    "hate",
    "ridiculous",
    "unacceptable",
    "worst",
    "frustrated",
    "disgusted",
    "outraged",
    "scam",
}


def classify_request_human(text: str) -> float:
    """Return confidence in [0,1] that the message requests a human."""
    return 0.9 if _HUMAN_PATTERNS.search(text or "") else 0.0


def sentiment_score(text: str) -> float:
    """Crude polarity in [-1, 1]; negative words push toward -1."""
    words = re.findall(r"[a-z']+", (text or "").lower())
    if not words:
        return 0.0
    neg = sum(1 for w in words if w in _NEGATIVE_WORDS)
    if neg == 0:
        return 0.0
    return max(-1.0, -0.4 - 0.3 * neg)


def high_value_lead_score(text: str, custom_rules_hit: bool = False) -> float:
    """Domain-agnostic heuristic (Architecture §3.4.5 weights), capped at 1.0."""
    t = (text or "").lower()
    score = 0.0
    # explicit budget figure
    if re.search(
        r"(\$|usd|budget|/month|per month|k\b)\s*\d|\d+\s*(k|dollars|/mo)", t
    ) or re.search(r"\$\s?\d", t):
        score += 0.5
    # time-constrained decision
    if re.search(r"\b(today|this week|tomorrow|urgent|urgently|asap|right now)\b", t):
        score += 0.3
    # explicit purchase/booking intent
    if re.search(r"\b(book|buy|purchase|sign up|get started|schedule|hire|engage)\b", t):
        score += 0.4
    if custom_rules_hit:
        score += 0.3
    return min(1.0, score)


# --- the two gates ---------------------------------------------------------- #
def evaluate_intake(trailing_messages: list[str]) -> EscalationDecision:
    """Gate 1 — signals knowable from the inbound message alone (pre-PLAN)."""
    latest = trailing_messages[-1] if trailing_messages else ""
    conf = classify_request_human(latest)
    if conf >= REQUEST_HUMAN_CONF:
        return EscalationDecision(True, Signal.explicit_human_request, Gate.intake, conf)

    # strong negative sentiment: <= -0.7 across >= 2 of the trailing 3 messages.
    last3 = trailing_messages[-3:]
    negatives = [m for m in last3 if sentiment_score(m) <= NEGATIVE_SENTIMENT_THRESHOLD]
    if len(negatives) >= 2:
        worst = min(sentiment_score(m) for m in last3)
        return EscalationDecision(True, Signal.strong_negative_sentiment, Gate.intake, worst)
    return EscalationDecision(False)


def evaluate_outcome(
    *,
    message: str,
    plan_confidence: float,
    grounding_score: float,
    custom_rules_hit: bool = False,
    resolving_tool_succeeded: bool = False,
) -> EscalationDecision:
    """Gate 2 — signals that need the loop's output (post-REFLECT).

    cannot_answer fires when confidence < 0.6 AND grounding below the 0.50 floor,
    UNLESS a resolving tool succeeded (then the answer is grounded in the tool result).
    high_value_lead fires on the scoring heuristic.
    """
    if (
        not resolving_tool_succeeded
        and plan_confidence < CANNOT_ANSWER_CONFIDENCE
        and grounding_score < GROUNDING_FLOOR
    ):
        return EscalationDecision(True, Signal.cannot_answer, Gate.outcome, plan_confidence)

    lead_score = high_value_lead_score(message, custom_rules_hit)
    if lead_score >= 0.5:
        return EscalationDecision(True, Signal.high_value_lead, Gate.outcome, lead_score)
    return EscalationDecision(False)
