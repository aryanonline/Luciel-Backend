"""Answer grounding & anti-hallucination (DOCTRINE ANCHOR — Architecture §3.4.13).

Grounding floor is 0.50, uniform across all accounts (Architecture §9 #18).
Below floor with no resolving tool → cannot_answer escalation. Faithful-readback:
Luciel may only state what a tool/result actually contains. Lands in Arc 14.

Platform-owned framing (SeedTest deploy gate 2026-11-13, T6 channel parity): the
first-message AI-identity disclosure the prompt requires, and an email's greeting and
sign-off, carry words no knowledge chunk can support, so the same grounded fact scored
0.67 on the widget and 0.40 on email. Rule: given the configured names, the scorer
removes only text the PLATFORM authored (the exact disclosure sentences of
``input_safety.platform_disclosure_sentences``) or a closed, claim-free shape (a
greeting alone on the first line, a closed sign-off over lines that are only the
configured names). It never interprets a disclosure the model worded itself.

Accepted residual: a model that paraphrases the disclosure or signs with unconfigured
words keeps that text in the score, as does a greeting to a name the tokenizer reads as
more than two words ("Hi Jean-François,"), which can hand off an honest answer. That is the
conservative direction, never an escape; the strict SeedTest gate is the live check
that the model follows the prompt's exact sentence.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Sequence
from dataclasses import dataclass

from app.runtime.input_safety import platform_disclosure_sentences

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

# Round 6 dev walk 2026-09-13 (R6-6): "Could you email me your Saturday opening hours?"
# retrieved the right chunk and still handed off at 4/11 = 0.36. The hours matched; the
# delivery offer the operating-context stanza asks the model to make ("I can send a copy
# to your email — what's the best address?") is claim-free in the same way a pleasantry
# is: nothing about the business is asserted, so nothing can be "supported". A fabricated
# fact behind the same offer still fails — its substantive tokens are unchanged.
_DELIVERY_AND_COURTESY_WORDS = frozenset(
    {
        "send",
        "sent",
        "sending",
        "copy",
        "email",
        "emailed",
        "text",
        "texted",
        "address",
        "best",
        "message",
        "happy",
        "glad",
        "course",
        "certainly",
        "absolutely",
        "definitely",
        "right",
        "away",
        "confirm",
        "confirmed",
        "details",
        "provide",
        "share",
        "reach",
        "contact",
        "like",
        "want",
        "need",
        "get",
        "give",
        "receive",
        "along",
        "over",
        "via",
        "way",
        "well",
        "great",
        "perfect",
        "sounds",
        "good",
        "welcome",
        "assist",
        "follow",
        "use",
        "note",
        "info",
        "information",
        "moment",
        "shortly",
        "soon",
        "once",
        "when",
        "where",
        "how",
        "why",
        "again",
        "below",
        "above",
        "full",
        "quick",
        "summary",
    }
)


# --- Platform-owned framing (2026-11-13 T6) ---------------------------------------
# A greeting alone on the first line, in one of two closed shapes: a greeting word with
# an optional closed address ("Hi there", "Good morning, all!"), or a greeting word, at
# most two name words and a closing "," or "!" ("Hi Jordan,", "Dear Mary Ann,"). The
# name must also hold at most two words as the scorer's tokenizer counts them
# (``_scored_word_count``): any letter outside a-z is a word break there, so a joined
# "Free<U+3164>Valet<U+3164>Parking" or "Jean-François" (jean, fran, ois) stays scored. A
# heading with no closing mark ("Hello Kitty Plushies") is content, not a greeting.
_GREETING = r"(?:hi|hello|hey|dear|greetings|good\s+(?:morning|afternoon|evening))"
_GREETING_LINE = re.compile(
    rf"{_GREETING}(?:\s+(?:there|all|everyone|folks|team))?\s*[,!]?"
    rf"|{_GREETING}\s+(?P<name>[^\W\d_]+(?:(?:\s+|['’.-])[^\W\d_]+)?)\s*[,!]",
    re.IGNORECASE,
)


def _scored_word_count(text: str) -> int:
    """Words the scorer could count in ``text``: score_grounding's tokenizer before its
    stopword filter (runs of [a-z0-9] longer than two characters in ``text.lower()``)."""
    return sum(len(w) > 2 for w in re.findall(r"[a-z0-9]+", text.lower()))


# A closed sign-off phrase, alone on its line, with its punctuation.
_SIGNOFF_LINE = re.compile(
    r"(?:best|best regards|kind regards|warm regards|warmest regards|regards|"
    r"best wishes|all the best|many thanks|thanks|thanks again|thank you|cheers|"
    r"sincerely|yours sincerely|yours truly|warmly|respectfully|take care|talk soon)"
    r"\s*[,.!]?",
    re.IGNORECASE,
)


def _norm_line(line: str) -> str:
    return " ".join(line.replace("’", "'").casefold().split())


# Typographic twins of a separator mark, so "Joe’s" reads as "Joe's" and "Hi - I'm" as
# "Hi — I'm".
_MARK_TWINS = {"'": "'’", "’": "'’", "-": "-–—", "–": "-–—", "—": "-–—"}


def _template_regex(sentence: str) -> str:
    """The exact words of a platform sentence in order, as a regex. Between two words
    the separator may differ only in its whitespace and in the typographic twin of the
    sentence's own marks: a space matches only spaces, so "for Acme; Free returns" or
    "for Acme… Free returns" never reads as the name "Acme Free Returns", and a line
    break matches only where the sentence ends a sentence (. ! ?). Escaped literals and
    single-character separator classes: linear time."""
    parts = re.split(r"(\w+)", sentence)
    words, gaps = parts[1::2], parts[2:-1:2]
    regex = re.escape(words[0])
    for gap, word in zip(gaps, words[1:], strict=True):
        space = r"\s" if re.search(r"[.!?]", gap) else r"[^\S\n]"
        marks = "".join(sorted({t for c in gap.strip() for t in _MARK_TWINS.get(c, c)}))
        sep = f"(?:{space}|[{re.escape(marks)}])+" if marks else f"{space}+"
        regex += sep + re.escape(word)
    return rf"\b{regex}\b"


@functools.lru_cache(maxsize=256)
def _disclosure_pattern(assistant_name: str, business_names: tuple[str, ...]) -> re.Pattern[str]:
    sentences = {
        variant
        for business in business_names
        for sentence in platform_disclosure_sentences(
            assistant_name=assistant_name, business_name=business
        )
        for variant in (sentence, sentence.replace("I'm", "I am"))
    }
    # Longest first, so the offer-bearing wording wins over its bare prefix.
    alternatives = sorted(sentences, key=len, reverse=True)
    return re.compile("|".join(_template_regex(s) for s in alternatives), re.IGNORECASE)


def strip_platform_framing(
    answer: str, *, assistant_name: str | None, business_names: Sequence[str | None] = ()
) -> str:
    """Remove the platform-owned framing from an answer before it is scored.

    1. Every occurrence of a platform disclosure sentence for the configured names
       (``input_safety.platform_disclosure_sentences``, plus "I am" for "I'm"), as a
       closed template: the exact words in order, case-insensitive.
    2. A greeting that is the whole first line (``_GREETING_LINE``), its name at most
       two words as the scorer counts them.
    3. A trailing sign-off block: a closed sign-off phrase line followed only by
       signature lines that are exactly a configured name or a closed role line built
       from them ("AI assistant for {business}", "{assistant}, AI assistant for
       {business}", "The {business} team"), blank lines allowed. At least one
       signature line is required — a bare "Sincerely" ending a list is a list item.
       Any other line keeps the whole block in the score.

    Everything else is untouched. No names → the answer unchanged (base behaviour)."""
    if not assistant_name:
        return answer
    businesses = tuple(dict.fromkeys(b.strip() for b in business_names if b and b.strip()))
    lines = answer.strip().splitlines()
    greeting = _GREETING_LINE.fullmatch(lines[0].strip()) if lines else None
    if greeting and _scored_word_count(greeting["name"] or "") <= 2:
        lines = lines[1:]
    signature = {_norm_line(n) for n in (assistant_name, *businesses)}
    for business in businesses:
        role = f"ai assistant for {business}"
        signature |= {_norm_line(r) for r in (role, f"{assistant_name}, {role}")}
        signature.add(_norm_line(f"the {business} team"))
    end, signed = len(lines), False
    while end and (not lines[end - 1].strip() or _norm_line(lines[end - 1]) in signature):
        signed = signed or bool(lines[end - 1].strip())
        end -= 1
    if signed and end and _SIGNOFF_LINE.fullmatch(lines[end - 1].strip()):
        lines = lines[: end - 1]
    text = "\n".join(lines)
    if businesses:
        text = _disclosure_pattern(assistant_name, businesses).sub(" ", text)
    return text


@dataclass
class GroundingResult:
    score: float
    grounded: bool
    reason: str


def score_grounding(
    answer: str,
    retrieved_chunks: list[str],
    tool_succeeded: bool,
    *,
    question: str | None = None,
    assistant_name: str | None = None,
    business_names: Sequence[str | None] = (),
) -> GroundingResult:
    """Estimate how well an answer is grounded in retrieved knowledge / tool results.

    A tool that returned `ok` grounds the answer in a live result (score 1.0). For
    knowledge-only answers, grounding is the fraction of answer tokens supported by
    the retrieved chunks (a deterministic, dependency-free proxy for the production
    grounding model). Below the 0.50 floor with no resolving tool => not grounded
    => cannot_answer escalation (§3.4.13 / §3.4.5).

    Claim-free tokens never count against the answer: stopwords and pleasantries
    (Harmony wave 2), delivery/courtesy words (round 6 R6-6), and — when ``question``
    is given — a word the customer used that no chunk supports, which the model is
    restating, not asserting ("opening hours" in "Our opening hours are 9am-2pm").

    Given the configured ``assistant_name`` and ``business_names``, platform-owned
    framing is removed from the answer first (``strip_platform_framing``; see the
    module docstring for the rule and its accepted residual). Without names the
    answer is scored exactly as before.
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
            return {
                w
                for w in words
                if w not in _GROUNDING_STOPWORDS and w not in _DELIVERY_AND_COURTESY_WORDS
            }
        return set(words)

    # Only the answer side drops stopwords/filler -- claim-free connective tissue
    # a real LLM wraps around a quoted fact must not dilute the score. The support
    # side (retrieved chunks) keeps every token so a stopword that happens to be
    # part of the substantive claim (rare, but harmless either way) still counts.
    answer = strip_platform_framing(
        answer, assistant_name=assistant_name, business_names=business_names
    )
    answer_tokens = toks(answer, drop_stopwords=True)
    if not answer_tokens:
        return GroundingResult(0.0, False, "no_content_tokens")
    support = set()
    for chunk in retrieved_chunks:
        support |= toks(chunk, drop_stopwords=False)
    if question:
        # Words the customer used that nothing supports are the question restated,
        # not a claim ("your opening hours" -> "our opening hours are 9am-2pm").
        answer_tokens -= toks(question, drop_stopwords=True) - support
        if not answer_tokens:
            return GroundingResult(0.0, False, "question_echo_only")
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

    number_pattern = r"\b\d[\d,\.:]*"
    claimed_numbers = set(re.findall(number_pattern, answer))
    allowed_numbers: set[str] = set()
    for v in allowed_fields.values():
        allowed_numbers |= set(re.findall(number_pattern, str(v)))
    # Any claimed number not present in the allowed fields is a faithful-readback breach.
    return claimed_numbers.issubset(allowed_numbers) if claimed_numbers else True
