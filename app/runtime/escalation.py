"""Escalation judgment module — the four fixed signals at two gates (Architecture §3.4.5).

Luciel decides WHEN to escalate; the admin never configures triggers (only who/how
is routed). The four signals are fixed and never admin-configurable (Locked
Decision §7 #16 only allows extending the high-value-lead heuristic via the
business-context field, never adding/removing signals).

Gate 1 — intake (pre-PLAN), knowable from the inbound message alone:
  - explicit_human_request  (intent_class == request_human, conf >= 0.85)
  - strong_negative_sentiment (<= -0.7 across >= 2 of trailing 3 messages)
Gate 2 — outcome (post-REFLECT), needs the loop's output:
  - cannot_answer (grounding < floor 0.50 OR plan confidence < 0.6, unless a
    resolving tool succeeded — see evaluate_outcome docstring: this is an OR, not
    an AND, because live providers report a fixed confidence=1.0 today and
    grounding must be sufficient on its own to gate a response)
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


# Conversational (social) turns: greetings, thanks, declines, acknowledgments,
# goodbyes. Live-caught 2026-08-18: a caller answering "No its alright thankyou"
# to Luciel's own follow-up offer tripped the cannot_answer grounding gate — a
# pleasantry retrieves nothing, scores 0.0, and (with live providers pinning
# confidence at 1.0) the grounding floor alone decided. A social turn contains no
# factual claim to ground, so the floor does not apply to it. Deterministic on
# purpose (same doctrine as the other classifiers in this module): regexes over a
# normalized short string, hermetically testable, no model in the loop.
_CONVERSATIONAL_MAX_LEN = 80
_CONVERSATIONAL_PATTERNS = re.compile(
    r"^(hi|hiya|hey|hello|good (morning|afternoon|evening)|greetings)\b"
    r"|\b(thanks|thank you|thankyou|thx|cheers|appreciate (it|that|you))\b"
    r"|^(no|nope|nah)\b.{0,40}\b(alright|all right|okay|ok|fine|good|thanks|thank you|thankyou)\b"
    r"|^(no thanks|no thank you|nothing else|that('s| is| was)? (all|it|everything))\b"
    r"|^(ok(ay)?|sure|great|perfect|sounds good|got it|understood|alright|all right|will do|makes sense)\b"
    r"|^(yes|yep|yeah|yes please|please do)\b"
    r"|\b(bye|goodbye|good ?night|take care|have a (good|great|nice) (day|night|one)|talk (soon|later)|see you)\b"
    r"|^(you'?re welcome|no (problem|worries))\b",
    re.IGNORECASE,
)
# Hard guards: anything that looks like it SEEKS information or action is never
# conversational, whatever else it matches — the grounding floor must keep
# gating real questions (the OR-gate doctrine is untouched by this classifier).
_SEEKING_PATTERNS = re.compile(
    r"\?"
    # Interrogatives ANYWHERE ("ok but what time do you open" hides one mid-turn);
    # auxiliary-verb starters only ANCHORED ("that can work" is not a question).
    r"|\b(what|how|when|where|why|which|who)\b"
    r"|^(can|could|would|will|do|does|did|is|are|should|may|might)\b"
    r"|\b(need|want|looking for|book|schedule|quote|price|cost|help me|tell me|explain)\b"
    r"|[0-9$£€]",
    re.IGNORECASE,
)


def is_conversational_turn(text: str) -> bool:
    """True when the message is a social turn with no factual claim to ground.

    Skips ONLY the gate-2 ``cannot_answer`` check (see ``evaluate_outcome``).
    Gate 1 (human request / sentiment), moderation, lead capture, and
    ``high_value_lead`` all still see every turn.
    """
    stripped = (text or "").strip()
    if not stripped or len(stripped) > _CONVERSATIONAL_MAX_LEN:
        return False
    if _SEEKING_PATTERNS.search(stripped):
        return False
    return bool(_CONVERSATIONAL_PATTERNS.search(stripped))


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

    cannot_answer fires when the answer is below the grounding floor (no resolving
    tool backed it, and retrieval/self-support didn't clear 0.50) OR the model
    self-reports low confidence, UNLESS a resolving tool succeeded (then the answer
    is grounded in the tool result). high_value_lead fires on the scoring heuristic.

    Harmony fix 2026-08-04 (item 3): this was previously an AND of the two signals
    ("confidence < 0.6 AND grounding < floor"). The live Anthropic/OpenAI providers
    (app/runtime/providers.py) report a hardcoded confidence=1.0 — no real
    self-reported confidence signal exists in production — so the AND was
    permanently unsatisfiable on the confidence side: an ungrounded, fabricated
    answer with grounding_score=0.0 would never escalate, because 1.0 is never
    < 0.6. Grounding alone is real and computed (app/runtime/grounding.py); it must
    be sufficient on its own to gate a response, not merely a necessary condition
    alongside a signal that is currently always 1.0. Confidence remains an
    independent OR trigger so a genuinely low-confidence signal (from EchoModel in
    tests today, or a real one if a provider ever supplies it) still escalates even
    when grounding happens to clear the floor.
    """
    # high_value_lead is checked FIRST: it is a content/opportunity signal, entirely
    # independent of whether Luciel's own answer was grounded. A high-value lead
    # message ("$5000 budget, want to book today") deserves urgent human follow-up
    # regardless of how well-grounded the reply happened to be — masking that behind
    # a grounding failure would bury the more business-critical signal.
    lead_score = high_value_lead_score(message, custom_rules_hit)
    if lead_score >= 0.5:
        return EscalationDecision(True, Signal.high_value_lead, Gate.outcome, lead_score)

    # A conversational/social turn (thanks, declines, goodbyes — see
    # is_conversational_turn) carries no factual claim, so the grounding floor
    # does not apply to it. This narrows WHICH turns count as answers; for real
    # questions the OR-gate below is byte-for-byte unchanged and must never
    # become an AND (harmony fix above still binds).
    if not resolving_tool_succeeded and not is_conversational_turn(message) and (
        grounding_score < GROUNDING_FLOOR or plan_confidence < CANNOT_ANSWER_CONFIDENCE
    ):
        score = min(plan_confidence, grounding_score)
        return EscalationDecision(True, Signal.cannot_answer, Gate.outcome, score)

    return EscalationDecision(False)
