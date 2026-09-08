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
    # Server-originated signals (§3.5.2): fired by the platform, not the judgment
    # gates. Added (audit round 3, C4) so the LoopResult decision names the SAME
    # signal the escalation_events row records — the at-cap path used to persist
    # "budget_exhausted" while returning cannot_answer to the caller.
    budget_exhausted = "budget_exhausted"
    llm_unavailable = "llm_unavailable"


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
    # WHY it fired, in the owner's words (round 6 WP-G, F124): short deterministic
    # phrases written to the escalation row so the dashboard can say "budget figure +
    # this week" instead of a bare score. Empty when nothing fired.
    reasons: tuple[str, ...] = ()


# --- pluggable classifiers (deterministic heuristics for dev/test) ---------- #
_HUMAN_PATTERNS = re.compile(
    r"\b(speak|talk|connect)\b.{0,20}\b(human|person|someone|representative|agent|real)\b"
    r"|\bhuman\b|\breal person\b|\bspeak to a\b"
    # Round 5C item 8: a data-deletion/right-to-be-forgotten request is a request
    # only a HUMAN can honor (erasure is the tenant's dashboard action, §3.4.11)
    # — Luciel must hand it off with context, never answer around it or fabricate
    # a deletion it cannot perform.
    r"|\b(delete|erase|remove|forget)\b.{0,20}"
    r"\bmy (data|info|information|details|records?|number)\b"
    r"|\bforget me\b|\bright to be forgotten\b|\bstop (storing|keeping) my\b",
    re.IGNORECASE,
)
# Round 6 WP-G (F124): a real complaint vocabulary (~80 terms) instead of 13 words, and
# negation-aware scoring below ("not bad" / "never disappointed" are not complaints).
# Still deterministic and hermetic; still platform-owned, never admin-configurable.
_NEGATIVE_WORDS = {
    # anger
    "angry",
    "furious",
    "livid",
    "irate",
    "enraged",
    "outraged",
    "fuming",
    "pissed",
    "mad",
    "seething",
    "infuriated",
    "infuriating",
    # disappointment / frustration
    "frustrated",
    "frustrating",
    "disappointed",
    "disappointing",
    "disgusted",
    "disgusting",
    "annoyed",
    "annoying",
    "irritated",
    "irritating",
    "upset",
    "exasperated",
    # quality judgments
    "terrible",
    "awful",
    "horrible",
    "horrendous",
    "atrocious",
    "appalling",
    "dreadful",
    "abysmal",
    "pathetic",
    "useless",
    "worthless",
    "garbage",
    "trash",
    "rubbish",
    "worst",
    "ridiculous",
    "absurd",
    "unacceptable",
    "incompetent",
    "unprofessional",
    "shoddy",
    "sloppy",
    "shameful",
    "disgrace",
    "disgraceful",
    "joke",
    "nightmare",
    # trust / money
    "scam",
    "scammed",
    "scammers",
    "fraud",
    "fraudulent",
    "ripoff",
    "rip-off",
    "ripped",
    "cheated",
    "cheat",
    "liar",
    "lied",
    "lying",
    "dishonest",
    "stolen",
    "stole",
    "robbed",
    # escalation / threat (NOT "refund", "cancel", "review": a polite cancellation or a
    # refund question is a request, not a complaint — see test_judgment_wp6)
    "lawyer",
    "lawsuit",
    "sue",
    "suing",
    "complaint",
    "chargeback",
    "bbb",
    # hostility
    "hate",
    "hated",
    "hateful",
    "loathe",
    "despise",
    "crap",
    "bullshit",
    "wtf",
    "idiot",
    "idiots",
    "stupid",
    "clueless",
    "ignored",
    "ignoring",
    "rude",
}
# Words that flip the polarity of the next few tokens ("not bad", "never disappointed").
_NEGATORS = {
    "not",
    "no",
    "never",
    "hardly",
    "isnt",
    "wasnt",
    "dont",
    "didnt",
    "cant",
    "wont",
    "without",
    "nothing",
    "neither",
    "nor",
    "n't",
}
_NEGATION_WINDOW = 2


# "Are you a human?" asks WHAT Luciel is; it does not ask FOR a human (2026-09-05
# audit F118). The disclosure answers it — handing the whole conversation to the
# owner because a visitor was curious made Luciel look evasive and cost a takeover.
_ASKS_WHAT_LUCIEL_IS = re.compile(
    r"\b(are|is|r) (you|u|this|that) (a |an )?"
    r"(human|real person|person|bot|robot|ai|machine|computer)\b"
    r"|\bam i (talking|speaking|chatting|texting) (to|with) a "
    r"(human|real person|person|bot|robot|machine)\b"
    r"|\b(is this|are you) (an? )?(ai|automated|real)\b",
    re.IGNORECASE,
)
_ASKS_FOR_HUMAN = re.compile(
    r"\b(speak|talk|connect|transfer|put me|get me)\b.{0,20}"
    r"\b(human|person|someone|representative|agent|real)\b"
    r"|\bi (want|need|would like) (a|an|to speak to a|to talk to a) "
    r"(human|person|real person|agent)\b",
    re.IGNORECASE,
)


def classify_request_human(text: str) -> float:
    """Return confidence in [0,1] that the message requests a human."""
    body = text or ""
    if _ASKS_WHAT_LUCIEL_IS.search(body) and not _ASKS_FOR_HUMAN.search(body):
        return 0.0
    return 0.9 if _HUMAN_PATTERNS.search(body) else 0.0


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
    r"|^(no|nope|nah)\b.{0,40}"
    r"\b(alright|all right|okay|ok|fine|good|thanks|thank you|thankyou)\b"
    r"|^(no thanks|no thank you|nothing else)\b"
    r"|^that('s| is| was)? (all|it|everything)\b"
    r"|^(ok(ay)?|sure|great|perfect|sounds good|got it|understood)\b"
    r"|^(alright|all right|will do|makes sense)\b"
    r"|^(yes|yep|yeah|yes please|please do)\b"
    r"|\b(bye|goodbye|good ?night|take care|talk (soon|later)|see you)\b"
    r"|\bhave a (good|great|nice) (day|night|one)\b"
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


# A contact hand-over: the visitor is giving an email address or a phone number
# and asking nothing — usually the answer to Luciel's OWN contact ask. The reply
# is an acknowledgement with no factual claim to ground. 2026-09-06 E2E walk:
# "I'm Walk Lead, you can reach me at walk.lead@example.com" retrieved nothing,
# scored 0.0, and the floor swapped the model's thank-you for the hand-off line a
# second time. Digits are substantive for is_conversational_turn (a budget, a
# date); here they are the phone number itself, so this classifier stands alone.
_CONTACT_MAX_LEN = 200
_EMAIL_RE = re.compile(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", re.IGNORECASE)
_PHONE_RE = re.compile(r"(?<![a-z0-9])\+?\(?\d[\d\s().-]{6,}\d(?![a-z0-9])", re.IGNORECASE)
_CONTACT_SEEKING_PATTERNS = re.compile(
    r"\?"
    r"|\b(what|how|when|where|why|which|who)\b"
    r"|^(can|could|would|will|do|does|did|is|are|should|may|might)\b"
    r"|\b(need|want|looking for|book|schedule|quote|price|cost|help me|tell me|explain)\b"
    r"|[$£€]",
    re.IGNORECASE,
)


def is_contact_details_turn(text: str) -> bool:
    """True when the visitor is handing over contact details and asking nothing.

    Skips ONLY the gate-2 ``cannot_answer`` floor, exactly like a social turn:
    lead capture still stores the details, gate 1 and ``high_value_lead`` still see
    the message. A question in the same breath keeps the floor."""
    stripped = (text or "").strip()
    if not stripped or len(stripped) > _CONTACT_MAX_LEN:
        return False
    if _CONTACT_SEEKING_PATTERNS.search(stripped):
        return False
    return bool(_EMAIL_RE.search(stripped) or _PHONE_RE.search(stripped))


_WORD_RE = re.compile(r"[a-z0-9']+")
# Below this Jaccard overlap two questions count as different asks; at or above
# it, the model is re-asking the same thing in fresh clothes. Conservative on
# purpose: a missed repeat costs one more polite turn, a false positive hands a
# productive gather to a human mid-flow.
_REPEATED_QUESTION_OVERLAP = 0.75


def _last_question(text: str) -> frozenset[str] | None:
    """Token set of the LAST question sentence in ``text``; None if no question."""
    questions = re.findall(r"[^?]*\?", text or "")
    if not questions:
        return None
    tokens = frozenset(_WORD_RE.findall(questions[-1].lower()))
    return tokens or None


def is_repeated_question(reply: str, previous_luciel_reply: str | None) -> bool:
    """True when this reply re-asks (near-)the-same question as the previous one.

    §3.3.7: "repeated re-asking of the same input is an explicitly prohibited
    failure mode." The broker-side ask-bound only counts turns where a proposed
    tool call was rejected — a model that asks in PROSE, gets a non-answer, and
    asks again never incremented it (round 5C item 18). This deterministic check
    closes that: the last question sentence of two consecutive replies matching
    at ≥ 0.75 token overlap is the same ask, and counts against the bound.
    Distinct follow-up questions in a healthy gather ("which day?" → "what
    time?") share almost no tokens and never trip it.
    """
    current = _last_question(reply)
    previous = _last_question(previous_luciel_reply or "")
    if current is None or previous is None:
        return False
    union = current | previous
    if not union:
        return False
    overlap = len(current & previous) / len(union)
    return overlap >= _REPEATED_QUESTION_OVERLAP


def negative_terms(text: str) -> list[str]:
    """The complaint terms in ``text`` that are NOT negated (round 6 WP-G, F124).

    A term within two tokens after a negator ("not bad", "never disappointed", "no
    complaint") is skipped; contractions are normalised so "isn't terrible" negates."""
    normalised = (text or "").lower().replace("’", "'")
    tokens = re.findall(r"[a-z]+(?:'[a-z]+)?", normalised)
    hits: list[str] = []
    for i, tok in enumerate(tokens):
        word = tok.replace("'", "")
        if word not in _NEGATIVE_WORDS:
            continue
        window = tokens[max(0, i - _NEGATION_WINDOW) : i]
        if any(w.replace("'", "") in _NEGATORS or w.endswith("n't") for w in window):
            continue
        hits.append(word)
    return hits


def sentiment_score(text: str) -> float:
    """Crude polarity in [-1, 1]; un-negated complaint terms push toward -1."""
    neg = len(negative_terms(text))
    if neg == 0:
        return 0.0
    return max(-1.0, -0.4 - 0.3 * neg)


def high_value_lead_reasons(
    text: str, custom_rules_hit: bool | str = False, context: str = ""
) -> tuple[float, tuple[str, ...]]:
    """Domain-agnostic heuristic (Architecture §3.4.5 weights), capped at 1.0, with the
    reasons that scored (round 6 WP-G, F124).

    Scored over the message AND the conversation so far (``context``, the lead's
    recent turns — 2026-09-05 audit F106): the flagship scene is a budget stated in
    one turn and "can we start this week?" in the next, which no single message ever
    scored above the line. ``custom_rules_hit`` is the owner's phrase that matched
    (F102) — or a bare True for callers that only know THAT one did.
    """
    t = f"{context or ''} {text or ''}".lower()
    score = 0.0
    reasons: list[str] = []
    # explicit budget figure
    if re.search(
        r"(\$|usd|budget|/month|per month|k\b)\s*\d|\d+\s*(k|dollars|/mo)", t
    ) or re.search(r"\$\s?\d", t):
        score += 0.5
        reasons.append("named a budget")
    # time-constrained decision
    if re.search(r"\b(today|this week|tomorrow|urgent|urgently|asap|right now)\b", t):
        score += 0.3
        reasons.append("wants it soon")
    # explicit purchase/booking intent
    if re.search(r"\b(book|buy|purchase|sign up|get started|schedule|hire|engage)\b", t):
        score += 0.4
        reasons.append("ready to buy or book")
    if custom_rules_hit:
        score += 0.3
        reasons.append(
            f'said "{custom_rules_hit}"'
            if isinstance(custom_rules_hit, str)
            else "matched your phrase"
        )
    return min(1.0, score), tuple(reasons)


def high_value_lead_score(
    text: str, custom_rules_hit: bool | str = False, context: str = ""
) -> float:
    return high_value_lead_reasons(text, custom_rules_hit, context)[0]


# --- the two gates ---------------------------------------------------------- #
def evaluate_intake(trailing_messages: list[str]) -> EscalationDecision:
    """Gate 1 — signals knowable from the inbound message alone (pre-PLAN)."""
    latest = trailing_messages[-1] if trailing_messages else ""
    conf = classify_request_human(latest)
    if conf >= REQUEST_HUMAN_CONF:
        return EscalationDecision(
            True, Signal.explicit_human_request, Gate.intake, conf, ("asked for a person",)
        )

    # strong negative sentiment: <= -0.7 across >= 2 of the trailing 3 messages.
    last3 = trailing_messages[-3:]
    negatives = [m for m in last3 if sentiment_score(m) <= NEGATIVE_SENTIMENT_THRESHOLD]
    if len(negatives) >= 2:
        worst = min(sentiment_score(m) for m in last3)
        terms: list[str] = []
        for m in negatives:
            for term in negative_terms(m):
                if term not in terms:
                    terms.append(term)
        return EscalationDecision(
            True,
            Signal.strong_negative_sentiment,
            Gate.intake,
            worst,
            (
                f"upset in {len(negatives)} of the last {len(last3)} messages",
                "words: " + ", ".join(terms[:5]),
            ),
        )
    return EscalationDecision(False)


def evaluate_outcome(
    *,
    message: str,
    plan_confidence: float,
    grounding_score: float,
    custom_rules_hit: bool | str = False,
    resolving_tool_succeeded: bool = False,
    context: str = "",
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
    lead_score, lead_reasons = high_value_lead_reasons(message, custom_rules_hit, context=context)
    if lead_score >= 0.5:
        return EscalationDecision(
            True, Signal.high_value_lead, Gate.outcome, lead_score, lead_reasons
        )

    # A conversational/social turn (thanks, declines, goodbyes — see
    # is_conversational_turn) carries no factual claim, so the grounding floor
    # does not apply to it. This narrows WHICH turns count as answers; for real
    # questions the OR-gate below is byte-for-byte unchanged and must never
    # become an AND (harmony fix above still binds).
    if (
        not resolving_tool_succeeded
        and not is_conversational_turn(message)
        and not is_contact_details_turn(message)
        and (grounding_score < GROUNDING_FLOOR or plan_confidence < CANNOT_ANSWER_CONFIDENCE)
    ):
        score = min(plan_confidence, grounding_score)
        why: list[str] = []
        if grounding_score < GROUNDING_FLOOR:
            why.append(f"answer only {grounding_score:.0%} backed by your knowledge")
        if plan_confidence < CANNOT_ANSWER_CONFIDENCE:
            why.append(f"the model was {plan_confidence:.0%} sure")
        return EscalationDecision(True, Signal.cannot_answer, Gate.outcome, score, tuple(why))

    return EscalationDecision(False)
