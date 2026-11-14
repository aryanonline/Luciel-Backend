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
    an AND, because a self-reported confidence is the model's own claim (and reads
    1.0 when the model gives none), so grounding must be sufficient on its own to
    gate a response)
  - high_value_lead (normalized lead-score; budget 0.5 + time-pressure 0.3 +
    purchase/booking intent 0.4, capped at 1.0)
  Both are evaluated on every turn (round 7 WP-2, item 1): the opportunity signal
  names the row, and ``EscalationDecision.answer_ungrounded`` carries the
  answer-quality verdict independently, so a hot lead never ships a fabricated
  answer just because the more business-critical signal won the row.

Classifiers (intent/sentiment) are pluggable; deterministic heuristics here exercise
the gates without an LLM. The thresholds are platform constants (never admin-set).
"""

from __future__ import annotations

import enum
import re
from collections.abc import Iterable
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
    # Luciel's OWN judgment (round 7 WP-2, item 2; Vision §3.4): the model called the
    # always-on ``hand_to_team`` cognition tool with a reason — or promised the
    # customer a hand-off in prose, which the orchestrator turns into this same real
    # escalation rather than letting the promise ship unbacked. The owner is notified
    # with the reason; Luciel keeps answering (D2: only an explicit human request and
    # threat/self-harm moderation take the conversation away).
    luciel_requested_handoff = "luciel_requested_handoff"
    # Moderation that does NOT take the conversation away (round 7 WP-2, item 3; D2):
    # abuse/harassment and an illegal solicitation are recorded and the owner is told
    # what was said, while Luciel stays professional and keeps answering. Threats and
    # self-harm keep their take-over path (explicit_human_request at intake).
    content_flagged = "content_flagged"


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
    # The answer-quality verdict, independent of which signal names the row (round 7
    # WP-2, item 1): True when the draft is below the grounding floor OR the model
    # reported low confidence and no resolving tool backed it — the OR-gate. The
    # orchestrator replaces the draft with the honest hand-off whenever this is set,
    # whether the row says cannot_answer or high_value_lead. ``answer_reasons`` are
    # the answer-quality phrases alone (``reasons`` carries them too, after the
    # signal's own).
    answer_ungrounded: bool = False
    answer_reasons: tuple[str, ...] = ()


# --- pluggable classifiers (deterministic heuristics for dev/test) ---------- #
# A request FOR a person is an ask-for-a-person PHRASE, never a bare word (round 7
# WP-2, item 2): "Can I speak to a hygienist?" and "Do you have human staff?" used to
# hand the whole conversation to the owner on "speak to a" / "human" alone, muting
# Luciel for a visitor who asked an ordinary question.
_HUMAN_PATTERNS = re.compile(
    # the ask itself: speak / talk / connect / transfer / put me ... to a person
    r"\b(speak|talk|connect|transfer|put me|get me)\b.{0,20}"
    r"\b(human|person|someone|somebody|representative|agent|real)\b"
    # "I want / need / 'd like a (real) human / person / agent"
    r"|\bi(?:'d)? ?(?:want|need|would like|like) (?:a|an) (?:real |actual )?"
    r"(?:human|person|agent|representative)\b"
    # "human please", "a real person please"
    r"|\b(?:a |an )?(?:real |actual )?(?:human|person)\b,? please\b"
    # "get me through", "put me in touch"
    r"|\b(?:get|put) me (?:through|in touch)\b"
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
# pleasantry retrieves nothing, scores 0.0, and (with live providers then pinning
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


# --- declining to give a required input (round 7 WP-2 remainder, item 2) ------------ #
# §3.3.7 rule 4 (the T5 rule): when a customer declines to give the detail a tool
# needs, Luciel stops asking. Consulted ONLY while a required-input gather is
# pending (sessions.input_ask_attempts > 0) — outside a gather these phrases are
# ordinary conversation. Deterministic and hermetic like every classifier here.
_DECLINE_VERBS = r"(?:give|share|provide|say|tell|disclose|answer|include|put|type|send)"
_DECLINE_VERBS_ING = (
    r"(?:giving|sharing|providing|saying|telling|disclosing|answering|including|putting"
    r"|typing|sending)"
)
_A_DETAIL = r"(?:my|that|this|it|those|(?:an?|the) (?:e-?mail|number|phone|address|name|details?))"
_DECLINE_PATTERNS = re.compile(
    # "I'd rather not (say)", "I would prefer not to", "rather not, thanks"
    r"\b(?:(?:i'?d|i would|i) )?(?:rather|prefer) not(?: to)?"
    rf"(?: {_DECLINE_VERBS}\b| (?:that|this|it|those)\b|\s*[.!,;]|\s*$)"
    # "I don't want to give (you) my email", "can't share my number", "won't say"
    rf"|\b(?:don'?t|do not|won'?t|will not|not going to|not gonna|can'?t|cannot|shan'?t)"
    rf" (?:want to |wanna |like to |wish to |be )?(?:{_DECLINE_VERBS}|{_DECLINE_VERBS_ING})"
    # …followed by what is withheld, or nothing: "can't say I've been" is an idiom
    r"(?: (?:you |them )?(?:that|this|it|those|my|any|out|more|anything)\b|\s*[.!,;]|\s*$)"
    # "not comfortable sharing / giving out"
    rf"|\bnot comfortable (?:with )?(?:{_DECLINE_VERBS}|{_DECLINE_VERBS_ING})\b"
    # "skip that", "let's skip it", "can we skip the email"
    r"|\b(?:skip|leave out|drop) (?:that|this|it|those|the \w+|my \w+)\b"
    r"|^\s*(?:just )?skip\b"
    # "never mind", "forget it"
    r"|\b(?:never ?mind|forget it)\b"
    # "(book it) without my email", "without giving you my number"
    rf"|\bwithout (?:{_DECLINE_VERBS_ING} (?:you )?)?{_A_DETAIL}\b"
    # "keep that private", "keep my number to myself", "none of your business"
    r"|\bkeep (?:that|this|it|my \w+) (?:private|to myself)\b"
    r"|\bnone of your business\b"
    # "I'm not giving that out"
    rf"|\b(?:i'?m|i am) not {_DECLINE_VERBS_ING}\b",
    re.IGNORECASE,
)
# A bare no to the question Luciel just asked — only the WHOLE message, so "no
# thanks, 2pm works" (a choice) is not a decline.
_BARE_NO = re.compile(
    r"^\s*(?:no|nope|nah|not really)\b[,.!]?\s*(?:thanks|thank you|thankyou|ta)?\s*[.!]*\s*$",
    re.IGNORECASE,
)
# A substitute value in the same breath is an ANSWER, whatever else was said
# ("it's not that I don't want to share — here: jane@example.com").
_ANSWERS_WITH_A_VALUE = re.compile(
    r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}|(?<![a-z0-9])\+?\(?\d[\d\s().-]{6,}\d(?![a-z0-9])",
    re.IGNORECASE,
)
# A CHOICE in the same breath is an answer too (round 7 judgment hardening, finding
# 5): a gather that stalled on an ambiguity asks "which one did you mean?", and the
# customer answers "drop the Tuesday one" or "never mind, Thursday at 3 works". A
# weekday, a clock time, a relative day or an ordinal names what they chose — the
# words "drop" / "never mind" are the choice's framing, not a refusal to say.
_ANSWERS_WITH_A_CHOICE = re.compile(
    r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday"
    r"|mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)\b"
    r"|\b(?:today|tomorrow|tonight|this (?:week|weekend|morning|afternoon|evening)"
    r"|next (?:week|month|monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b"
    r"|\b\d{1,2}(?::\d{2})?\s*(?:am|pm|o'?clock)\b|\bat \d{1,2}(?::\d{2})?\b|\b(?:noon|midday)\b"
    r"|\b(?:the )?(?:first|second|third|last|earlier|later|other|morning|afternoon|evening) one\b",
    re.IGNORECASE,
)


def is_decline_turn(text: str) -> bool:
    """True when the customer declines to GIVE a detail they were asked for.

    Positives: "I'd rather not say", "I don't want to give my email", "no thanks",
    "skip that", "not comfortable sharing my number", "can you book it without my
    email?". Negatives: declining an OPTION ("I don't want the 3pm slot"), a
    preference ("I'd rather not do mornings"), a message that supplies the value
    after all, "no thanks, 2pm works" (a choice, not a refusal) and a choice framed
    as a drop ("drop the Tuesday one", "never mind, Thursday at 3 works").
    """
    body = (text or "").strip()
    if not body or _ANSWERS_WITH_A_VALUE.search(body) or _ANSWERS_WITH_A_CHOICE.search(body):
        return False
    return bool(_BARE_NO.match(body) or _DECLINE_PATTERNS.search(body))


# --- distinct unanswered questions (round 7 WP-2 remainder, item 1) ---------------- #
# The function words every customer question shares; what is left is what the
# question is ABOUT. "do you do implants?" and "what about implants?" both reduce
# to {implants}: the same unanswered question, not news for the owner twice.
_QUESTION_STOPWORDS = frozenset(
    """a an and any are about at be but can could did do does for from have has how
    i if in is it its me my of on or please so that the there these this those to
    tell us was we what when where which who why will with would you your yours
    just also still again like get got know need want one ones some more much many
    thanks thank hi hello hey ok okay sure well u ur pls plz""".split()
)
# Content-token containment at or above this and the later question is the earlier one
# in new clothes ("implants??" after "do you do implants?").
_SAME_QUESTION_CONTAINMENT = 0.6


def question_terms(text: str) -> frozenset[str]:
    """The content tokens of a customer message — words minus the function words."""
    return frozenset(
        tok for tok in _WORD_RE.findall((text or "").lower()) if tok not in _QUESTION_STOPWORDS
    )


def is_same_question(text: str, previous: str) -> bool:
    """True when ``text`` asks what ``previous`` asked: the shared content tokens cover
    at least ``_SAME_QUESTION_CONTAINMENT`` of the shorter question. A message with
    no content tokens at all ("hello??") is never a new question."""
    current, earlier = question_terms(text), question_terms(previous)
    if not current:
        return True
    if not earlier:
        return False
    shared = len(current & earlier)
    return shared / min(len(current), len(earlier)) >= _SAME_QUESTION_CONTAINMENT


def is_new_question(text: str, previous_questions: Iterable[str]) -> bool:
    """A DISTINCT unanswered question (round 7 WP-2 remainder, item 1): the message
    has content of its own and is unlike every question Luciel already could not
    answer this session. The orchestrator re-notifies the owner on it; a repeat of
    an earlier one ("implants??") pages nobody again."""
    if not question_terms(text):
        return False
    return not any(is_same_question(text, earlier) for earlier in previous_questions)


# --- is the WHOLE message a pleasantry / a contact detail? (round 7 hardening) ------- #
# ``is_conversational_turn`` and ``is_contact_details_turn`` read how a message OPENS
# or what it CONTAINS: "Hi, opening hours on Saturday please" opens with a greeting,
# "My number is 416-555-0143. Wondering if you service Oakville" contains a phone
# number. Neither is ONLY that — each carries a request as a noun phrase, with no
# question mark and no interrogative for the seeking guards to catch. Skipping the
# knowledge search on such a turn drops a covered answer (a wrong refusal), so the
# skip asks the stricter question: once the pleasantries and the contact details are
# taken out, is anything left? Any content word left over means "look it up" — a
# wasted embedding call is cheap, a dropped answer is not.
_SOCIAL_PHRASES = re.compile(
    r"\bgood (?:morning|afternoon|evening|night|day)\b"
    r"|\bhave a (?:good|great|nice|lovely|wonderful) (?:day|night|evening|weekend|one)\b"
    r"|\b(?:take care|talk (?:soon|later)|see (?:you|ya)(?: (?:soon|later|then))?)\b"
    r"|\bthat(?:'s| is| was)? (?:all|it|everything)\b"
    r"|\b(?:sounds|looks|all) good\b|\bgot it\b|\ball right\b|\bwill do\b|\bmakes sense\b"
    r"|\bnothing else\b|\bno (?:problem|worries)\b|\byou(?:'re| are) welcome\b"
    r"|\bappreciate (?:it|that|you)\b"
    r"|\b(?:that|this|it) (?:works|helps)(?: for me)?\b"
    r"|\bfor (?:your|the|all your|all the) (?:help|time|patience)\b",
    re.IGNORECASE,
)
# The words a customer wraps AROUND their own address or number: "you can reach me
# at …", "my number is …", "… works best". Removed with the detail itself.
_CONTACT_FRAMING = re.compile(
    r"\b(?:you can |feel free to )?(?:reach|contact|call|text|e-?mail|message|ring|phone) me"
    r"(?: back)?(?: (?:at|on|via|by))?\b"
    r"|\bi can be reached(?: (?:at|on|via|by))?\b"
    # "my" only: "ok and the phone number" asks for the BUSINESS's number.
    r"|\bmy(?: best)? (?:e-?mail|number|phone|cell|mobile|contact|name)"
    r"(?: address| number| details| info)?(?: is|'s)?(?=[\s:,.!-]|$)"
    r"|\bhere(?:'s| is| are) my (?:e-?mail|number|phone|cell|mobile|details|contact)"
    r"(?: address| number| details| info)?\b"
    r"|\b(?:is|works|would be) (?:best|fine|good)\b|\bworks\b",
    re.IGNORECASE,
)
# Words that carry no request of their own beside a greeting, an acknowledgement or
# a contact detail. Deliberately NOT day/time/thing words: "one night please" and
# "Saturday morning" are requests.
_REMAINDER_FILLER = frozenset(
    """hiya greetings howdy yo thankyou thx ty cheers appreciated
    no nope nah yes yep yeah yup k kk alright fine good great perfect understood
    cool nice lovely awesome wonderful brilliant excellent fantastic
    bye goodbye goodnight cya
    very really lot lots all too then now there here
    everyone guys team folks mate sir madam
    oh ah um hmm right exactly indeed absolutely definitely certainly noted
    it's its that's thats i'm im i'll am or name best""".split()
)


def substantive_remainder(text: str) -> frozenset[str]:
    """The content words left in a message once its pleasantries, its contact details
    and the customer's own name are removed. Empty means the WHOLE message was social
    or contact-only — there is nothing to look up. Deterministic and hermetic."""
    from app.runtime.lead_capture import extract_lead_signals  # noqa: PLC0415 — import cycle

    body = (text or "").replace("’", "'")
    # The customer's own name is not a request: a self-introduction ("I'm Jane
    # Miller") and the words of their own address ("Jane Miller, jane.miller@…").
    own_name = set(_WORD_RE.findall((extract_lead_signals(body).name or "").lower()))
    lowered = body.lower()
    for match in _EMAIL_RE.finditer(lowered):
        own_name.update(re.findall(r"[a-z]+", match.group(0).split("@", 1)[0]))
    for pattern in (_EMAIL_RE, _PHONE_RE, _SOCIAL_PHRASES, _CONTACT_FRAMING):
        lowered = pattern.sub(" ", lowered)
    return frozenset(
        tok
        for tok in question_terms(lowered)
        if tok not in _REMAINDER_FILLER and tok not in own_name
    )


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
    (app/runtime/providers.py) then reported a hardcoded confidence=1.0, so the AND
    was permanently unsatisfiable on the confidence side: an ungrounded, fabricated
    answer with grounding_score=0.0 would never escalate, because 1.0 is never
    < 0.6. Since round 6 WP-G (F124) the prompt asks the model for a confidence tag
    (``providers.parse_confidence_tag``; a reply without one still reads 1.0), but a
    self-report is the model's own claim. Grounding is computed by the platform
    (app/runtime/grounding.py), so it must be sufficient on its own to gate a
    response, not merely a necessary condition. Confidence remains an independent
    OR trigger, so a low self-reported confidence (or EchoModel's in tests) still
    escalates even when grounding happens to clear the floor.
    """
    # Answer quality is judged on EVERY turn, before the opportunity signal is even
    # looked at (round 7 WP-2, item 1). It used to be evaluated only when
    # high_value_lead had NOT fired — the lead signal returned first and
    # short-circuited — so a fabricated answer to a hot lead shipped verbatim.
    #
    # A conversational/social turn (thanks, declines, goodbyes — see
    # is_conversational_turn) carries no factual claim, so the grounding floor
    # does not apply to it. This narrows WHICH turns count as answers; for real
    # questions the OR-gate below is byte-for-byte unchanged and must never
    # become an AND (harmony fix above still binds).
    why: list[str] = []
    if (
        not resolving_tool_succeeded
        and not is_conversational_turn(message)
        and not is_contact_details_turn(message)
        and (grounding_score < GROUNDING_FLOOR or plan_confidence < CANNOT_ANSWER_CONFIDENCE)
    ):
        if grounding_score < GROUNDING_FLOOR:
            why.append(f"answer only {grounding_score:.0%} backed by your knowledge")
        if plan_confidence < CANNOT_ANSWER_CONFIDENCE:
            why.append(f"the model was {plan_confidence:.0%} sure")
    answer_ungrounded = bool(why)

    # high_value_lead names the ROW when it fires: it is a content/opportunity
    # signal, entirely independent of whether Luciel's own answer was grounded. A
    # high-value lead message ("$5000 budget, want to book today") deserves urgent
    # human follow-up regardless of how well-grounded the reply happened to be —
    # masking that behind a grounding failure would bury the more business-critical
    # signal. The answer verdict rides alongside it (never behind it): the row's
    # reasons carry both, and the orchestrator reads ``answer_ungrounded`` to keep
    # the draft from leaving.
    lead_score, lead_reasons = high_value_lead_reasons(message, custom_rules_hit, context=context)
    if lead_score >= 0.5:
        return EscalationDecision(
            True,
            Signal.high_value_lead,
            Gate.outcome,
            lead_score,
            (*lead_reasons, *why),
            answer_ungrounded=answer_ungrounded,
            answer_reasons=tuple(why),
        )

    if answer_ungrounded:
        score = min(plan_confidence, grounding_score)
        return EscalationDecision(
            True,
            Signal.cannot_answer,
            Gate.outcome,
            score,
            tuple(why),
            answer_ungrounded=True,
            answer_reasons=tuple(why),
        )

    return EscalationDecision(False)
