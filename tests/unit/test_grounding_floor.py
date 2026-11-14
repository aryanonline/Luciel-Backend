"""Unit tests for the grounding floor gate (Architecture §3.4.13 / §3.4.5).

Harmony fix 2026-08-04, item 3: ``evaluate_outcome`` used to require BOTH
``plan_confidence < 0.6`` AND ``grounding_score < 0.50`` to fire cannot_answer.
The live Anthropic/OpenAI providers (app/runtime/providers.py) then hardcoded
``confidence=1.0`` for every real response (since round 6 WP-G they parse a
self-reported confidence tag, and a reply without one still reads 1.0), so the
AND was permanently unsatisfiable on the confidence side: a confidently
fabricated, completely ungrounded answer (grounding_score == 0.0) would never
escalate. These tests pin the fixed behaviour: grounding below floor is
sufficient on its own (unless a resolving tool succeeded), and a genuinely low
confidence signal is still an independent trigger.
"""

from __future__ import annotations

import hashlib
import importlib.util
import itertools
import re
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest
from app.runtime import grounding, input_safety
from app.runtime.escalation import Signal, evaluate_outcome
from app.runtime.grounding import (
    GROUNDING_FLOOR,
    GroundingResult,
    score_grounding,
    strip_platform_framing,
)
from app.runtime.input_safety import (
    _compose_greeting,
    build_system_prompt,
    compose_ai_disclosure,
)


def test_confident_but_ungrounded_answer_still_escalates() -> None:
    """The exact bug: confidence=1.0 (what every live provider call reported then,
    and what a reply without a confidence tag still reads) with a completely
    ungrounded answer (no retrieval, no tool) must escalate. Before
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


def test_a_delivery_offer_around_a_grounded_fact_scores_above_floor() -> None:
    """LIVE (round 6 dev walk, 2026-09-13, R6-6): "Could you email me your Saturday opening
    hours?" retrieved the clinic chunk and still handed off — the draft scored 4/11 = 0.36.
    The hours matched; the delivery offer the operating-context stanza asks for and the
    customer's echoed words ("opening hours") carry no claim and diluted the proxy."""
    chunk = (
        "Maple & Main Dental is open Monday to Friday 8am-6pm and Saturday 9am-2pm. "
        "We are closed Sundays. New patient checkup with cleaning costs $180."
    )
    question = "Could you email me your Saturday opening hours?"
    answer = (
        "Our Saturday opening hours are 9am to 2pm. I can send a copy of this to your "
        "email — what's the best address to use?"
    )
    result = score_grounding(answer, [chunk], False, question=question)
    assert result.score >= GROUNDING_FLOOR, result
    assert result.grounded is True


def test_a_fabricated_fact_behind_the_same_delivery_offer_still_fails() -> None:
    """The delivery words are claim-free; the fact behind them is not."""
    chunk = "Our store hours are 9am to 6pm Monday through Friday."
    question = "Could you email me your warranty terms?"
    answer = (
        "We offer a lifetime warranty on every product. I can send a copy of this to "
        "your email — what's the best address to use?"
    )
    result = score_grounding(answer, [chunk], False, question=question)
    assert result.score < GROUNDING_FLOOR, result
    assert result.grounded is False


def test_restating_the_customers_words_is_not_a_claim_but_an_unsupported_yes_is() -> None:
    chunk = "Open Saturday 9am-2pm."
    grounded = score_grounding(
        "Our opening hours are 9am to 2pm on Saturday.",
        [chunk],
        False,
        question="What are your opening hours?",
    )
    assert grounded.grounded is True, grounded
    # Nothing but the question's own words, and none of them supported: a bare "yes"
    # the knowledge never backed.
    fabricated = score_grounding(
        "Yes, we do teeth whitening.", [chunk], False, question="Do you do teeth whitening?"
    )
    assert fabricated.score == 0.0
    assert fabricated.reason == "question_echo_only"
    # Without a question the scorer behaves exactly as before.
    assert score_grounding("Yes, we do teeth whitening.", [chunk], False).score == 0.0


# --- Platform-owned framing (SeedTest deploy gate 2026-11-13, run 43485158726, T6) -------
# The same grounded question scored 0.67 on the widget and 0.40 on email: the first-message
# AI-identity disclosure the prompt requires, and email's greeting and sign-off, carried
# words no chunk can support. The platform now owns that framing: rule 1 gives the model
# the exact disclosure sentence, and the scorer removes only that sentence (and the
# platform's other wordings), a greeting alone on the first line, and a closed sign-off
# over configured-name lines. Everything else scores exactly as the base scorer.


def _load_base_scorer() -> ModuleType:
    """ba97103's grounding.py, verbatim (tests/unit/fixtures/grounding_ba97103.py): the
    scorer as it was before platform-owned framing, the reference every framing-free
    answer must still match."""
    path = Path(__file__).parent / "fixtures" / "grounding_ba97103.py"
    spec = importlib.util.spec_from_file_location("grounding_ba97103", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # @dataclass resolves its module by name
    spec.loader.exec_module(module)
    return module


BASE = _load_base_scorer()

T6_CHUNK = "Our return window is 30 days from the date of delivery."
T6_QUESTION = "How many days do I have to return an item?"
T6_FACT = "You have 30 days from the date of delivery to return an item."
HOURS_CHUNK = (
    "Our store is open 9am to 5pm Monday to Friday. Returns are accepted within 30 days "
    "with a receipt."
)
HOURS_QUESTION = "What are your hours?"
HOURS_FACT = "Our store is open 9am to 5pm Monday to Friday."
DENTAL_CHUNK = "Acme Dental is open Monday to Friday, 9am to 5pm. New patients are welcome."
DENTAL_QUESTION = "Are you open on Fridays?"
# What the orchestrator passes: the assistant, the business display name and its short
# name (in production both are instances.business_short_name, or "this business").
ACME = ("Luciel", ("Acme Store", "Acme Store"))
UNNAMED = ("Luciel", ("this business", None))
DISCLOSURE = compose_ai_disclosure(assistant_name="Luciel", business_name="Acme Store")
T6_EMAIL = f"Hi Jordan,\n\n{DISCLOSURE} {T6_FACT}\n\nBest regards,\nLuciel\nAcme Store"
T6_WIDGET = f"{DISCLOSURE} {T6_FACT}"

Names = tuple[str, tuple[str | None, ...]]


def _framed(
    answer: str,
    names: Names = ACME,
    chunk: str = T6_CHUNK,
    question: str | None = T6_QUESTION,
) -> GroundingResult:
    assistant, businesses = names
    return score_grounding(
        answer,
        [chunk],
        False,
        question=question,
        assistant_name=assistant,
        business_names=businesses,
    )


def _base(
    answer: str, chunk: str = T6_CHUNK, question: str | None = T6_QUESTION
) -> GroundingResult:
    """The base scorer's verdict, as this module's result type (so == compares fields)."""
    base = BASE.score_grounding(answer, [chunk], False, question=question)
    return GroundingResult(base.score, base.grounded, base.reason)


def _words(text: str) -> list[str]:
    return re.findall(r"\w+", text)


def _strip(answer: str, names: Names = ACME) -> str:
    assistant, businesses = names
    return strip_platform_framing(answer, assistant_name=assistant, business_names=businesses)


def test_the_base_scorer_fixture_is_ba97103_verbatim() -> None:
    """The reference is the git blob of app/runtime/grounding.py at ba97103 (line endings
    normalised, as git stores them)."""
    raw = (Path(__file__).parent / "fixtures" / "grounding_ba97103.py").read_bytes()
    body = raw.replace(b"\r\n", b"\n")
    blob = hashlib.sha1(b"blob %d\0" % len(body) + body, usedforsecurity=False).hexdigest()
    assert blob == "ed48fdab4c5f0dbdf0cfb567c59bdec1ff31a358"


def test_t6_email_and_widget_shapes_are_served_at_the_hand_stripped_score() -> None:
    """The T6 reproduction: before, the email shape handed off and the two channels
    disagreed. Both now score exactly the base score of the bare fact."""
    hand_stripped = _base(T6_FACT)
    assert hand_stripped.score == 1.0
    assert _base(T6_EMAIL).grounded is False  # the deploy-gate failure, on the base scorer
    for answer in (
        T6_EMAIL,
        T6_WIDGET,
        T6_EMAIL.replace("\nAcme Store", "\nAI assistant for Acme Store"),
        T6_EMAIL.replace("Luciel\nAcme Store", "Luciel, AI assistant for Acme Store"),
        T6_EMAIL.replace("Best regards,\nLuciel\nAcme Store", "Thanks,\n\nThe Acme Store team"),
        T6_EMAIL.replace("I'm Luciel", "I am Luciel"),
    ):
        result = _framed(answer)
        assert result == hand_stripped, (answer, result)
        assert result.grounded is True
    # An unnamed business: the prompt's placeholder sentence is the one removed.
    unnamed = compose_ai_disclosure(assistant_name="Luciel", business_name="")
    assert unnamed == "I'm Luciel, an AI assistant for this business."
    assert _framed(f"Hi Jordan,\n\n{unnamed} {T6_FACT}\n\nBest,\nLuciel", UNNAMED) == hand_stripped


def test_the_platform_greeting_and_offer_lines_are_removed() -> None:
    greeting = _compose_greeting(business_name="Acme Store", assistant_name="Luciel")
    offer = f"{DISCLOSURE[:-1]}, and {input_safety.PLATFORM_OFFER}."
    for framing in (greeting, offer, greeting.replace("I'm", "I am")):
        answer = f"{framing}\n\n{T6_FACT}"
        assert _words(_strip(answer)) == _words(T6_FACT), _strip(answer)
        assert _framed(answer) == _base(T6_FACT), framing
    # The greeting's own words are not left behind for the score.
    assert "help" not in _strip(f"{greeting} {T6_FACT}")


def test_the_disclosure_is_a_closed_template_not_a_word_bag() -> None:
    """Only the exact words in order go. A separator may differ, a sentence or line
    break may not appear where the sentence has none, and every word around the template
    stays, so a claim riding on the disclosure is scored exactly as the hand-stripped
    text (the parity value)."""
    for answer, parity in (
        (
            f"{DISCLOSURE[:-1]} offering free exchanges. {T6_FACT}",
            f"offering free exchanges. {T6_FACT}",
        ),
        (
            f"{DISCLOSURE[:-1]}: we price match any competitor. {HOURS_FACT}",
            f"we price match any competitor. {HOURS_FACT}",
        ),
        (
            f"{DISCLOSURE[:-1]} (lifetime warranties included). {HOURS_FACT}",
            f"lifetime warranties included. {HOURS_FACT}",
        ),
        (
            f"{DISCLOSURE[:-1]}… every order ships free. {HOURS_FACT}",
            f"every order ships free. {HOURS_FACT}",
        ),
        (f"{DISCLOSURE} We price match. {DISCLOSURE}", "We price match."),
        (
            f"{DISCLOSURE[:-1]}, and I can book in-person or virtual appointments. {HOURS_FACT}",
            f"and I can book in-person or virtual appointments. {HOURS_FACT}",
        ),
    ):
        for chunk, question in ((T6_CHUNK, T6_QUESTION), (HOURS_CHUNK, HOURS_QUESTION)):
            assert _framed(answer, chunk=chunk, question=question) == _base(parity, chunk, question)
    # A business name made of claim words: only the template occurrence goes.
    free = ("Luciel", ("Free Shipping Co", None))
    sentence = compose_ai_disclosure(assistant_name="Luciel", business_name="Free Shipping Co")
    assert _words(_strip(f"{sentence} Free shipping on all orders.", free)) == _words(
        "Free shipping on all orders."
    )
    # A sentence or line break inside the name is not the name.
    for answer in (
        "I'm Luciel, an AI assistant for Acme. Store credit is instant.",
        "I'm Luciel, an AI assistant for Acme\nStore credit is instant.",
        "I'm Luciel. An AI assistant for Acme Store. Store credit is instant.",
    ):
        assert _strip(answer, ("Luciel", ("Acme Store", None))) == answer
    # Paraphrases are not removed (the accepted residual): conservative, never an escape.
    for paraphrase in (
        f"I'm an AI assistant for Acme Store. {T6_FACT}",
        f"Hi! I'm Luciel, Acme Store's AI assistant. {T6_FACT}",
        f"I'm Luciel, an AI assistant for Acme. {T6_FACT}",
    ):
        assert _strip(paraphrase) == paraphrase
        assert _framed(paraphrase) == _base(paraphrase)


def test_greeting_and_signoff_are_closed_shapes() -> None:
    kept = (
        # A greeting with content on the same line, or more than two name words.
        f"Hi Jordan, we offer free shipping. {T6_FACT}",
        f"Hello Free Shipping Daily!\n\n{T6_FACT}",
        f"Hi 24/7!\n\n{T6_FACT}",
        # A sign-off with no configured signature, or a signature line that is not one.
        f"{T6_FACT}\n\nBest regards,\nJordan",
        f"{T6_FACT}\n\nBest regards,\nLuciel\nAcme Store, free shipping",
        f"{T6_FACT}\n\nBest regards,\nLuciel\nAcme Store | Lifetime Warranty",
        f"{T6_FACT}\n\nThanks!",
        f"{T6_FACT}\n\nLuciel\nAcme Store",
        # A sign-off line that carries a claim.
        f"{T6_FACT}\n\nThanks for choosing our lifetime warranty,\nLuciel",
    )
    for answer in kept:
        assert _strip(answer) == answer.strip(), answer
        assert _framed(answer) == _base(answer), answer
    removed = (
        f"Hi Jordan,\n\n{T6_FACT}",
        f"Dear Mary Ann,\n{T6_FACT}",
        f"Hi O'Brien!\n{T6_FACT}",
        f"Good morning,\n\n{T6_FACT}",
        f"{T6_FACT}\n\nKind regards,\nLuciel",
        f"{T6_FACT}\n\nCheers\n\nLuciel\n\nAcme Store",
        f"{T6_FACT}\nThank you!\nLUCIEL\nai assistant for acme store",
    )
    for answer in removed:
        assert _strip(answer).strip() == T6_FACT, answer


BRUNCH_CHUNK = "Brunch is served Saturday and Sunday from 10am to 2pm."
BRUNCH_FACT = "Brunch is served Saturday and Sunday from 10am to 2pm, with live jazz."
TOY_CHUNKS = (
    "All toys are in stock.",
    "Good Morning Special: two eggs, toast and coffee for $9.99, served until 11am. "
    "Hello Kitty plushies are $12.",
)


def test_a_greeting_line_holds_at_most_two_words_as_the_tokenizer_counts_them() -> None:
    """Fixer round 1, finding 1: a hyphen, dot or apostrophe chain made the "two name
    words" limit open-ended ("Hi Free-Valet-Parking-Every-Weekend," went, claim and all,
    0.750 served against the base 0.462). A join now counts as a word break, as the
    scorer's tokenizer counts it, so such a line stays in the score."""
    for greeting, body, chunk in (
        ("Hi Free-Valet-Parking-Every-Weekend,", BRUNCH_FACT, BRUNCH_CHUNK),
        (
            "Hi free-overnight-shipping-lifetime-warranty-included!",
            "Orders ship within 3 business days, returns accepted.",
            "Orders ship within 3 business days.",
        ),
        ("Hello Kids.Eat.Free.Always!", BRUNCH_FACT, BRUNCH_CHUNK),
        ("Good morning Bottomless-Mimosas-Included!", BRUNCH_FACT, BRUNCH_CHUNK),
        ("Hello Free-Shipping-Daily!", BRUNCH_FACT, BRUNCH_CHUNK),
        ("Hello Free'Shipping’Daily!", BRUNCH_FACT, BRUNCH_CHUNK),
        ("Hi Jean-François Dupont,", BRUNCH_FACT, BRUNCH_CHUNK),
    ):
        answer = f"{greeting}\n\n{body}"
        assert _strip(answer) == answer, answer
        assert _framed(answer, chunk=chunk, question=None) == _base(answer, chunk, None), answer
    # Two words, however joined, are still a greeting.
    for greeting in ("Dear Mary-Ann!", "Hi O'Brien!", "Dear Mary Ann,"):
        assert _strip(f"{greeting}\n{BRUNCH_FACT}") == BRUNCH_FACT, greeting


# Fillers (U+3164, U+FFA0, U+1160 render blank), modifier letters, and accented letters:
# every one is a letter to the regex and a word break to the scorer's tokenizer.
_JOINS = [chr(c) for c in (0x3164, 0xFFA0, 0x1160, 0x02BC, 0x02C8, 0xA78F, 0x30FC, 0x01C0)]


@pytest.mark.parametrize("join", [*_JOINS, "é", "ß"])
def test_a_greeting_name_holds_at_most_two_scored_words_whatever_letter_joins_it(
    join: str,
) -> None:
    """Fixer round 2, finding 1: the name word of the greeting line matched any Unicode
    letter, while the scorer's tokenizer reads only a-z0-9, so a filler (U+3164 renders as
    a space), modifier letter or accented letter joined a claim of any length into one
    "name word": "Hi Free<U+3164>Valet<U+3164>Parking<U+3164>Every<U+3164>Weekend," went,
    claim and all, 0.750 served against the base 0.462. The name now also has to hold at
    most two words as the scorer counts them, so the line stays in the score."""
    claim = join.join(("Free", "Valet", "Parking", "Every", "Weekend"))
    answer = f"Hi {claim},\n\n{BRUNCH_FACT}"
    assert _strip(answer) == answer
    result = _framed(answer, chunk=BRUNCH_CHUNK, question="When is brunch?")
    assert result == _base(answer, BRUNCH_CHUNK, "When is brunch?")
    assert result.grounded is False
    warranty = join.join(("Lifetime", "Warranty", "Included", "Free", "Returns"))
    answer = f"Hello {warranty}!\nOrders ship within 3 business days."
    assert _strip(answer) == answer
    chunk = "Orders ship within 3 business days."
    assert _framed(answer, chunk=chunk, question=None) == _base(answer, chunk, None)
    # No length bound escapes either: forty joined claim words stay scored.
    long_claim = join.join(f"Claim{chr(97 + i % 26)}word" for i in range(40))
    answer = f"Hi {long_claim},\n{BRUNCH_FACT}"
    assert _strip(answer) == answer
    # The accepted cost, conservative: a name the tokenizer splits into three words
    # ("Jean-François" is jean, fran, ois) stays scored too.
    answer = f"Hi Jean-François,\n{BRUNCH_FACT}"
    assert _strip(answer) == answer
    # A two-word name with a joining letter the tokenizer drops is still a greeting.
    assert _strip(f"Dear Zoë Smith,\n{BRUNCH_FACT}") == BRUNCH_FACT


def test_a_greeting_shaped_heading_with_no_closing_mark_is_content() -> None:
    """Fixer round 1, finding 5: a first-line heading ("Hello Kitty Plushies") read as a
    greeting, so a fabricated heading was served (0.333 → 1.0) and a supported one was
    handed off (0.667 → 0.0). With name words, a greeting line now needs its closing
    "," or "!"; only a closed address ("there", "all", "everyone", "folks", "team") may
    go without it."""
    for answer, chunks, question in (
        ("Hello Kitty Plushies\nIn stock.", TOY_CHUNKS[:1], None),
        ("Hello Kitty Plushies\nIn stock.", TOY_CHUNKS[:1], "What toys do you have in stock?"),
        ("Hello Kitty Plushies\nPrice: $12", TOY_CHUNKS, None),
        ("Hey Dude Shoes\nIn stock.", TOY_CHUNKS[:1], None),
        (
            "Dear Evan Hansen\nHamilton and Wicked are playing this month.",
            ("Now playing: Hamilton and Wicked. Matinees are Sunday at 2pm.",),
            "What's playing?",
        ),
    ):
        assert _strip(answer) == answer, answer
        base = BASE.score_grounding(answer, list(chunks), False, question=question)
        for assistant, businesses in (ACME, UNNAMED, ("Aurora", ("Glow", None))):
            result = score_grounding(
                answer,
                list(chunks),
                False,
                question=question,
                assistant_name=assistant,
                business_names=businesses,
            )
            assert (result.score, result.grounded) == (base.score, base.grounded), answer
    for greeting in ("Hi there", "Hello all", "Good morning everyone", "Hey team", "Hi"):
        assert _strip(f"{greeting}\n{T6_FACT}") == T6_FACT, greeting


@pytest.mark.parametrize(
    "separator", ["; ", ": ", "… ", " — ", " - ", ", ", " • ", "/", " | ", " & ", " (", "。", "！"]
)
def test_a_clause_break_inside_the_business_name_is_not_the_name(separator: str) -> None:
    """Fixer round 1, finding 2: the template's gap crossed ";", ":", "…", "—" and the
    other clause marks, so "for Acme; Free returns" read as the configured name "Acme
    Free Returns" and the claim "Free returns" was removed (0.556 served against a
    parity of 0.455). A space in the platform sentence now matches only whitespace."""
    names: Names = ("Luciel", ("Acme Free Returns", None))
    chunk = "Orders ship within 3 business days."
    answer = (
        f"I'm Luciel, an AI assistant for Acme{separator}Free returns. "
        "Orders ship within 3 business days, gift wrapped overnight, insured."
    )
    assert _strip(answer, names) == answer
    result = _framed(answer, names, chunk=chunk, question=None)
    assert result == _base(answer, chunk, None)
    assert result.grounded is False
    # The builder's own example, with a Unicode sentence break.
    for mark in ("…", "。", "; ", " — "):
        text = f"I'm Luciel, an AI assistant for Acme{mark} Store credit is instant."
        assert _strip(text, ("Luciel", ("Acme Store", None))) == text
    # The exact sentence, and its typographic twins, still go.
    for sentence in (
        "I'm Luciel, an AI assistant for Acme Free Returns.",
        "I’m Luciel, an AI assistant for Acme  Free Returns.",
        "I am Luciel, an AI assistant for acme free returns.",
    ):
        assert _words(_strip(f"{sentence} {T6_FACT}", names)) == _words(T6_FACT), sentence


def test_a_business_name_ending_in_punctuation_ends_the_disclosure_itself() -> None:
    """Fixer round 1, finding 3: "Acme Inc." made rule 1 quote "...for Acme Inc.." for the
    model to copy word for word. The composer adds the period only when the name has no
    closing mark, and the offer wording is built from the bare clause, so the model's
    natural "Acme Inc., and I can help..." is still the platform's own sentence."""
    for business, sentence in (
        ("Acme Inc.", "I'm Aurora, an AI assistant for Acme Inc."),
        ("Yahoo!", "I'm Aurora, an AI assistant for Yahoo!"),
        ("Joe's Pizza Co.", "I'm Aurora, an AI assistant for Joe's Pizza Co."),
        ("Acme Store", "I'm Aurora, an AI assistant for Acme Store."),
        # Fixer round 2, finding 2: a closing "…" (or "‽", or a full-width mark) ends
        # the sentence too, so rule 1 never quotes "Acme….".
        ("Acme…", "I'm Aurora, an AI assistant for Acme…"),
        ("Acme‽", "I'm Aurora, an AI assistant for Acme‽"),
        ("Acme。", "I'm Aurora, an AI assistant for Acme。"),
    ):
        assert compose_ai_disclosure(assistant_name="Aurora", business_name=business) == sentence
        offer = input_safety.platform_disclosure_sentences(
            assistant_name="Aurora", business_name=business
        )[1]
        assert offer == f"I'm Aurora, an AI assistant for {business}, and " + (
            f"{input_safety.PLATFORM_OFFER}."
        )
        prompt = build_system_prompt(None, assistant_name="Aurora", business_name=business)
        rule_one = next(line for line in prompt.splitlines() if line.startswith("1. AI IDENTITY"))
        assert f'in exactly these words: "{sentence}"' in rule_one
        assert not re.search(r"[.!?…‽。][.]", rule_one), rule_one
        names: Names = ("Aurora", (business, None))
        for framing in (sentence, offer, f"{sentence}."):
            answer = f"{framing} {T6_FACT}"
            assert _framed(answer, names) == _base(T6_FACT), answer


# Claim-escape corpus. Every claim-escape case found by the four review rounds of the
# replaced design (branch fix/grounding-disclosure e81f6fa, tests/unit/test_grounding_floor.py,
# and review result grounding-r3-result.json: R1 F1/F2/F3/F4/F12/F13/F14, R2 F1/F2/F9/F10),
# ported as (id, answer, parity, names, chunk, question). Parity is the answer with only
# the framing that design claimed to remove deleted by hand; a fabricated claim must never
# score above it, and here no answer carries platform framing, so each scores exactly as
# the base scorer on the unchanged answer.
LASH = ("Luciel", ("The Lash Lounge", "Lash Lounge"))
DENTAL = ("Luciel", ("Acme Dental", "Acme"))
OLD_T6 = ("Luciel", ("Acme Store", "Acme"))
NIMBUS = ("Luciel", ("Nimbus Labs", "Nimbus"))
NIMBUS_CHUNK = (
    "Nimbus Labs sells an AI receptionist plan for clinics that answers calls and books "
    "appointments. The plan costs $99 per month."
)
GIFT_CHUNK = (
    "A standard haircut costs $45 and a colour treatment starts at $120. Gift cards are available."
)
OFFER = ", and I can help answer questions, book, or connect you to a person."
CLAIM_ESCAPES: list[tuple[str, str, str, Names, str, str | None]] = [
    (
        "email-framing-fabricated-fact",
        "Hi Jordan,\n\nI'm Luciel. I'm an AI assistant for Acme Store, and I can help answer "
        "questions, book, or connect you to a person. We offer a lifetime warranty and free "
        "overnight shipping on every order.\n\nBest regards,\nLuciel\nAI assistant for Acme Store",
        "We offer a lifetime warranty and free overnight shipping on every order.",
        OLD_T6,
        T6_CHUNK,
        T6_QUESTION,
    ),
    (
        "disclosure-sentence-with-digit",
        "I'm an AI assistant and returns take 45 days.",
        "I'm an AI assistant and returns take 45 days.",
        OLD_T6,
        T6_CHUNK,
        T6_QUESTION,
    ),
    (
        "as-an-ai-assistant-lead-in",
        f"Hi Jordan,\n\nAs an AI assistant for Acme, I can confirm every order ships free "
        f"overnight. {T6_FACT}\n\nBest regards,\nLuciel",
        f"I can confirm every order ships free overnight. {T6_FACT}",
        OLD_T6,
        T6_CHUNK,
        T6_QUESTION,
    ),
    (
        "tagline-under-signoff",
        f"{HOURS_FACT}\n\nBest regards,\nLuciel\nAcme Store | Lifetime Warranty On Every Purchase",
        f"{HOURS_FACT} Lifetime Warranty On Every Purchase",
        ("Luciel", ("Acme Store",)),
        HOURS_CHUNK,
        HOURS_QUESTION,
    ),
    (
        "title-case-lines-under-thanks",
        f"{HOURS_FACT}\n\nThanks!\nAcme Store\nFree Same Day Delivery Citywide\n"
        "Price Match Guaranteed",
        f"{HOURS_FACT} Free Same Day Delivery Citywide Price Match Guaranteed",
        ("Luciel", ("Acme Store",)),
        HOURS_CHUNK,
        HOURS_QUESTION,
    ),
    (
        "claim-line-under-signature",
        f"{T6_FACT}\n\nBest regards,\nLuciel\nFree Overnight Shipping And Lifetime Warranty",
        f"{T6_FACT} Free Overnight Shipping And Lifetime Warranty",
        ("Luciel", ("Acme Store",)),
        T6_CHUNK,
        T6_QUESTION,
    ),
    (
        "ps-under-signoff",
        f"{T6_FACT}\n\nBest,\nLuciel\nP.S. All Sale Items Are Final",
        f"{T6_FACT} P.S. All Sale Items Are Final",
        ("Luciel", ("Acme Store",)),
        T6_CHUNK,
        T6_QUESTION,
    ),
    (
        "offer-words-as-a-service",
        "I'm an AI assistant for Acme Dental, and I can book in-person or virtual appointments. "
        "Yes, we're open Friday.",
        "in-person or virtual appointments. Yes, we're open Friday.",
        DENTAL,
        DENTAL_CHUNK,
        DENTAL_QUESTION,
    ),
    (
        "team-member-service",
        "I'm an AI assistant for Glow Salon, and I can book touch-up appointments with a team "
        "member. Yes, we're open Friday.",
        "touch-up appointments with a team member. Yes, we're open Friday.",
        ("Luciel", ("Glow Salon", "Glow")),
        DENTAL_CHUNK,
        DENTAL_QUESTION,
    ),
    (
        "business-name-as-claim-subject",
        "I'm an AI assistant for Acme Dental, and Acme Dental offers free virtual implant "
        "consults. Yes, we're open Friday.",
        "and Acme Dental offers free virtual implant consults. Yes, we're open Friday.",
        DENTAL,
        DENTAL_CHUNK,
        DENTAL_QUESTION,
    ),
    (
        "r1f1-conjunction-and-name",
        f"I'm an AI assistant and Acme Store offers free exchanges. {T6_FACT}",
        f"and Acme Store offers free exchanges. {T6_FACT}",
        OLD_T6,
        T6_CHUNK,
        T6_QUESTION,
    ),
    (
        "r1f1-so-name-can",
        f"I'm an AI assistant so Acme Store can offer store credit. {T6_FACT}",
        f"so Acme Store can offer store credit. {T6_FACT}",
        OLD_T6,
        T6_CHUNK,
        T6_QUESTION,
    ),
    (
        "r1f1-same-day-dental",
        "I'm an AI assistant offering same day dental implants. Consultations are free.",
        "offering same day dental implants. Consultations are free.",
        ("Ava", ("Same Day Dental", "Same Day")),
        "Implants are available. Consultations are free.",
        "Do you do implants?",
    ),
    (
        "r1f1-free-shipping-co",
        "I'm an AI assistant including free shipping on every order. Orders arrive in 3 to 5 "
        "business days.",
        "including free shipping on every order. Orders arrive in 3 to 5 business days.",
        ("Luciel", ("Free Shipping Co",)),
        "Orders arrive in 3 to 5 business days.",
        "How long does delivery take?",
    ),
    (
        "r1f1-name-then-claim-verb",
        f"I'm an AI assistant for Acme Store offering free exchanges. {T6_FACT}",
        f"for Acme Store offering free exchanges. {T6_FACT}",
        OLD_T6,
        T6_CHUNK,
        T6_QUESTION,
    ),
    (
        "r1f2-this-business-default",
        f"I'm an AI assistant recommending this business. {HOURS_FACT}",
        f"recommending this business. {HOURS_FACT}",
        ("the assistant", ("this business",)),
        HOURS_CHUNK,
        HOURS_QUESTION,
    ),
    (
        "r1f2-the-lash-lounge",
        f"I'm an AI assistant matching the lowest prices. {HOURS_FACT}",
        f"matching the lowest prices. {HOURS_FACT}",
        LASH,
        HOURS_CHUNK,
        HOURS_QUESTION,
    ),
    (
        "r1f2-this-lowest-price",
        "I'm an AI assistant guaranteeing this lowest price. Yes, we're open Friday.",
        "guaranteeing this lowest price. Yes, we're open Friday.",
        ("Luciel", ("this business",)),
        "The Lash Lounge is open Monday to Friday, 9am to 5pm.",
        DENTAL_QUESTION,
    ),
    (
        "r1f4-booking-promise",
        "I'm an AI assistant for Acme Store, and I'll book you an appointment for Saturday. "
        f"{HOURS_FACT}",
        f"and I'll book you an appointment for Saturday. {HOURS_FACT}",
        OLD_T6,
        HOURS_CHUNK,
        HOURS_QUESTION,
    ),
    (
        "r1f4-connect-promise",
        f"I'm an AI assistant for Acme Store, and I'll connect you with our team. {HOURS_FACT}",
        f"and I'll connect you with our team. {HOURS_FACT}",
        OLD_T6,
        HOURS_CHUNK,
        HOURS_QUESTION,
    ),
    (
        "r1f13-personal-trainer",
        "I'm an AI assistant for Acme Fitness, and I can connect you with a personal trainer and "
        "sauna sessions. The pool closes at 9pm.",
        "with a personal trainer and sauna sessions. The pool closes at 9pm.",
        ("Luciel", ("Acme Fitness", "Acme")),
        "Acme Fitness memberships cost $29 per month. The pool closes at 9pm.",
        "When does the pool close?",
    ),
    (
        "r1f14-dear-evan-hansen",
        "Dear Evan Hansen, Hamilton and Wicked are playing this month.",
        "Dear Evan Hansen, Hamilton and Wicked are playing this month.",
        ("Luciel", ()),
        "Now playing: Hamilton and Wicked. Matinees are Sunday at 2pm.",
        "What's playing?",
    ),
    (
        "free-exchanges-daily-unnamed",
        f"I'm an AI assistant for Free Exchanges Daily. {T6_FACT}",
        f"Free Exchanges Daily. {T6_FACT}",
        UNNAMED,
        T6_CHUNK,
        T6_QUESTION,
    ),
    (
        "acme-free-exchanges-daily-short-name",
        f"I'm an AI assistant for Acme Free Exchanges Daily. {T6_FACT}",
        f"Free Exchanges Daily. {T6_FACT}",
        ("Luciel", ("Acme", "Acme")),
        T6_CHUNK,
        T6_QUESTION,
    ),
    (
        "r1f3-r1f12-cards-thank-you-list",
        "We stock:\nBirthday\nThank You\nWedding\nRetirement\nGraduation",
        "We stock:\nBirthday\nThank You\nWedding\nRetirement\nGraduation",
        OLD_T6,
        "Acme Store stocks Birthday and Thank You cards.",
        "What cards do you have?",
    ),
    (
        "r1f12-cheers-list",
        "Cards in stock:\n\nBirthday\nCheers\nAnniversary\nSympathy",
        "Cards in stock:\n\nBirthday\nCheers\nAnniversary\nSympathy",
        OLD_T6,
        "Acme Store stocks Birthday and Thank You cards.",
        "What cards do you have?",
    ),
    (
        "r1f12-cheers-last",
        "We stock:\nBirthday\nGraduation\nCheers",
        "We stock:\nBirthday\nGraduation\nCheers",
        OLD_T6,
        "Acme Store stocks Birthday and Thank You cards.",
        "What cards do you have?",
    ),
    (
        "r1f3-refundable",
        f"{HOURS_FACT} Cheers! Refundable.",
        f"{HOURS_FACT} Cheers! Refundable.",
        OLD_T6,
        HOURS_CHUNK,
        HOURS_QUESTION,
    ),
    (
        "r1f3-guaranteed",
        f"{HOURS_FACT} Thanks, Guaranteed.",
        f"{HOURS_FACT} Thanks, Guaranteed.",
        OLD_T6,
        HOURS_CHUNK,
        HOURS_QUESTION,
    ),
    (
        "r1f3-non-refundable",
        f"{HOURS_FACT}\n\nBest,\nLuciel\nNon-refundable",
        f"{HOURS_FACT}\n\nBest,\nLuciel\nNon-refundable",
        OLD_T6,
        HOURS_CHUNK,
        HOURS_QUESTION,
    ),
    (
        "r1f3-waterproof",
        f"{HOURS_FACT}\n\nThanks!\nWaterproof\nGuaranteed",
        f"{HOURS_FACT}\n\nThanks!\nWaterproof\nGuaranteed",
        OLD_T6,
        HOURS_CHUNK,
        HOURS_QUESTION,
    ),
    (
        "r2f1-ai-receptionist-after-disclosure",
        "I'm Luciel, an AI assistant for Acme Store. Our AI receptionist can book you an "
        "appointment, answer questions, or connect you with a staff member on weekends. "
        f"{HOURS_FACT}",
        "Our AI receptionist can book you an appointment, answer questions, or connect you with a "
        f"staff member on weekends. {HOURS_FACT}",
        OLD_T6,
        HOURS_CHUNK,
        "Can I book on a weekend?",
    ),
    (
        "r2f1-ai-receptionist-alone",
        "Our AI receptionist can book you an appointment, answer questions, or connect you with a "
        f"staff member on weekends. {HOURS_FACT}",
        "Our AI receptionist can book you an appointment, answer questions, or connect you with a "
        f"staff member on weekends. {HOURS_FACT}",
        OLD_T6,
        HOURS_CHUNK,
        "Can I book on a weekend?",
    ),
    (
        "r2f1-ai-concierge-bullet",
        "Hi! I'm Luciel, an AI assistant for Acme Store.\n\nWhat we offer:\n- An AI concierge who "
        "can book appointments and connect you with a staff member\n- Open 9am to 5pm Monday to "
        "Friday",
        "What we offer:\n- An AI concierge who can book appointments and connect you with a staff "
        "member\n- Open 9am to 5pm Monday to Friday",
        OLD_T6,
        HOURS_CHUNK,
        "What do you offer?",
    ),
    (
        "r2f1-second-concierge-clause",
        "I'm Luciel, an AI assistant for Acme Store, and our AI concierge can connect you with a "
        f"staff member at home. {HOURS_FACT}",
        f", and our AI concierge can connect you with a staff member at home. {HOURS_FACT}",
        OLD_T6,
        HOURS_CHUNK,
        "Do you do home visits?",
    ),
    (
        "r2f10-chatbot-kiosks",
        "We sell AI chatbot kiosks and laptops.",
        "We sell AI chatbot kiosks and laptops.",
        OLD_T6,
        "Acme Store sells laptops.",
        "What do you sell?",
    ),
    (
        "r2f10-automated-chat-bot",
        "Our automated chat bot integrates with clinics.",
        "Our automated chat bot integrates with clinics.",
        NIMBUS,
        NIMBUS_CHUNK,
        "Integrations?",
    ),
    (
        "r2f10-ai-receptionist-yes",
        "Hi! I'm Luciel, an AI assistant for Nimbus Labs. Yes, we have an AI receptionist.",
        "Yes, we have an AI receptionist.",
        NIMBUS,
        NIMBUS_CHUNK,
        "Do you have an AI receptionist?",
    ),
    (
        "r2f10-ai-receptionist-provider",
        "As an AI receptionist provider, we support clinics and salons.",
        "As an AI receptionist provider, we support clinics and salons.",
        NIMBUS,
        NIMBUS_CHUNK,
        "Who do you support?",
    ),
    (
        "signature-other-business-address",
        f"{T6_FACT}\n\nBest regards,\nLuciel\nsupport@bestdeals.com",
        f"{T6_FACT}\n\nBest regards,\nLuciel\nsupport@bestdeals.com",
        ("Luciel", ("Acme Store",)),
        T6_CHUNK,
        T6_QUESTION,
    ),
    (
        "signature-free-shipping-team",
        f"{T6_FACT}\n\nBest regards,\nLuciel\nFree Shipping Team",
        f"{T6_FACT}\n\nBest regards,\nLuciel\nFree Shipping Team",
        ("Luciel", ("Acme Store",)),
        T6_CHUNK,
        T6_QUESTION,
    ),
    (
        "signature-customer-address",
        f"{T6_FACT}\n\nBest regards,\nLuciel\njordan@acmestore.com",
        f"{T6_FACT}\n\nBest regards,\nLuciel\njordan@acmestore.com",
        ("Luciel", ("Acme Store",)),
        T6_CHUNK,
        T6_QUESTION,
    ),
    (
        "free-ai-assistant-adjective",
        f"I'm the free AI assistant for Free Shipping Co. {HOURS_FACT}",
        f"free {HOURS_FACT}",
        ("Luciel", ("Free Shipping Co",)),
        HOURS_CHUNK,
        "hours?",
    ),
    (
        "r2f2-name-word-before-marker",
        f"Shipping is always free (AI assistant tip: order before noon). {HOURS_FACT}",
        f"Shipping is always free (AI assistant tip: order before noon). {HOURS_FACT}",
        ("Luciel", ("Free Shipping Co",)),
        HOURS_CHUNK,
        "Do you ship?",
    ),
    (
        "r2f2-lifetime-warranty-name",
        f"Every repair comes with a lifetime warranty, your AI assistant can confirm. {HOURS_FACT}",
        f"Every repair comes with a lifetime warranty, your AI assistant can confirm. {HOURS_FACT}",
        ("Luciel", ("Lifetime Warranty Auto",)),
        HOURS_CHUNK,
        "Is there a warranty?",
    ),
    *[
        (
            f"r2f9-bare-signoff-list-item-{item}",
            f"Gift cards are available in these designs:\nBirthday\nWedding\n{item}",
            f"Gift cards are available in these designs:\nBirthday\nWedding\n{item}",
            ("Luciel", ()),
            GIFT_CHUNK,
            None,
        )
        for item in ("Sincerely", "Take Care", "Warm Regards", "All the Best", "Thanks a Bunch")
    ],
    *[
        (
            f"clause-shape-{i}",
            f"{lead}{claim}. {HOURS_FACT}",
            re.sub(r"(?i)AI assistant|Acme", "", f"{lead}{claim}. {HOURS_FACT}"),
            OLD_T6,
            HOURS_CHUNK,
            HOURS_QUESTION,
        )
        for i, (lead, claim) in enumerate(
            (
                ("I'm an AI assistant for Acme: ", "we price match any competitor"),
                ("I'm an AI assistant for Acme, ", "which price matches any competitor"),
                ("I'm an AI assistant for Acme, ", "though I can confirm lifetime warranties"),
                ("I'm an AI assistant for Acme (", "lifetime warranties included)"),
                ("I'm an AI assistant for Acme… ", "lifetime warranties come with every purchase"),
                ("Hi! As Acme's AI assistant, I can confirm ", "every order ships free"),
                ("As an AI assistant, I can tell you that ", "we offer lifetime warranty"),
                ("I'm an AI assistant, ", "our warranty covers accidental damage forever"),
                ("I'm an AI assistant for Acme, ", "ninety minute delivery citywide"),
                ("Ich bin ein AI Assistant für Acme, ", "Garantie lebenslang kostenlos"),
            )
        )
    ],
]


# The platform framing (closed greeting line, closed sign-off, exact disclosure) the new
# rule removes from a corpus case, deleted by hand. A case not listed carries none.
HAND_STRIPPED = {
    "email-framing-fabricated-fact": "I'm Luciel. I'm an AI assistant for Acme Store, and I "
    "can help answer questions, book, or connect you to a person. We offer a lifetime "
    "warranty and free overnight shipping on every order.",
    "as-an-ai-assistant-lead-in": "As an AI assistant for Acme, I can confirm every order "
    f"ships free overnight. {T6_FACT}",
    **{
        case[0]: case[2]  # the exact disclosure was the only framing: parity is the rest
        for case in CLAIM_ESCAPES
        if case[0]
        in {
            "r2f1-ai-receptionist-after-disclosure",
            "r2f1-ai-concierge-bullet",
            "r2f1-second-concierge-clause",
            "r2f10-ai-receptionist-yes",
        }
    },
}


@pytest.mark.parametrize(
    ("case_id", "answer", "parity", "names", "chunk", "question"),
    CLAIM_ESCAPES,
    ids=[case[0] for case in CLAIM_ESCAPES],
)
def test_no_claim_escape_case_scores_above_its_parity(
    case_id: str, answer: str, parity: str, names: Names, chunk: str, question: str | None
) -> None:
    """Each case hands off or scores no higher than its parity value, and scores
    exactly the base score of the answer with only the new rule's framing removed."""
    framed = _framed(answer, names, chunk, question)
    assert not framed.grounded or framed.score <= _base(parity, chunk, question).score, framed
    assert framed == _base(HAND_STRIPPED.get(case_id, answer), chunk, question), framed


def test_claim_escape_cases_the_old_branch_pinned_as_handed_off_still_hand_off() -> None:
    handed_off = {
        "email-framing-fabricated-fact",
        "disclosure-sentence-with-digit",
        "as-an-ai-assistant-lead-in",
        "tagline-under-signoff",
        "title-case-lines-under-thanks",
        "claim-line-under-signature",
        "ps-under-signoff",
        "offer-words-as-a-service",
        "team-member-service",
        "business-name-as-claim-subject",
        "r1f13-personal-trainer",
        "r1f14-dear-evan-hansen",
        "r1f3-r1f12-cards-thank-you-list",
        *(
            f"r2f9-bare-signoff-list-item-{i}"
            for i in ("Sincerely", "Take Care", "Warm Regards", "All the Best", "Thanks a Bunch")
        ),
    }
    by_id = {case[0]: case for case in CLAIM_ESCAPES}
    assert handed_off <= by_id.keys()
    for case_id in sorted(handed_off):
        _, answer, _, names, chunk, question = by_id[case_id]
        result = _framed(answer, names, chunk, question)
        assert result.grounded is False, (case_id, result)


def test_exact_disclosure_variants_of_the_claim_escapes_score_the_hand_stripped_text() -> None:
    """The same claims behind the EXACT disclosure: only the sentence goes, every claim
    word stays, and the score equals the base score with that sentence deleted by hand."""
    for claim, chunk, question in (
        ("We offer a lifetime warranty on every order.", T6_CHUNK, T6_QUESTION),
        ("Our AI receptionist can book you on weekends.", HOURS_CHUNK, HOURS_QUESTION),
        ("Acme Store offers free exchanges.", T6_CHUNK, T6_QUESTION),
        ("I'll book you an appointment for Saturday.", HOURS_CHUNK, HOURS_QUESTION),
    ):
        for framing in (
            DISCLOSURE,
            _compose_greeting(business_name="Acme Store", assistant_name="Luciel"),
        ):
            for answer in (
                f"{framing} {claim}",
                f"Hi Jordan,\n\n{framing}\n{claim}\n\nBest,\nLuciel",
            ):
                assert claim in _strip(answer), answer
                result = _framed(answer, chunk=chunk, question=question)
                assert result == _base(claim, chunk, question), (answer, result)
                assert result.grounded is False


def _framing_free_corpus() -> list[tuple[str, str, str]]:
    """240 answers with no platform framing, many of them near misses: greeting words on
    a content line, sign-off phrases with no configured signature, the configured names
    with no sign-off above them, paraphrased disclosures."""
    bodies = (
        (T6_FACT, T6_CHUNK, T6_QUESTION),
        (HOURS_FACT, HOURS_CHUNK, HOURS_QUESTION),
        ("We offer a lifetime warranty and free overnight shipping.", T6_CHUNK, T6_QUESTION),
        ("Yes, we're open Friday from 9am to 5pm.", DENTAL_CHUNK, DENTAL_QUESTION),
        ("Implant consults are free on Saturdays.", DENTAL_CHUNK, "Do you do implants?"),
    )
    prefixes = (
        "",
        "Sure! ",
        "Hi Jordan, ",
        "Hello! ",
        "As an AI assistant, ",
        "I'm an AI assistant for Acme Store, and ",
        "Hi! I'm Luciel, Acme Store's AI assistant. ",
        "I'm Luciel, an AI assistant for Acme. ",
    )
    suffixes = (
        "",
        " Anything else?",
        "\n\nThanks!",
        "\n\nBest regards,\nJordan",
        "\n\nLuciel\nAcme Store",
        "\nSincerely",
    )
    return [
        (f"{prefix}{body}{suffix}", chunk, question)
        for (body, chunk, question), prefix, suffix in itertools.product(bodies, prefixes, suffixes)
    ]


def test_framing_free_answers_score_exactly_as_the_base_scorer() -> None:
    corpus = _framing_free_corpus()
    assert len(corpus) >= 200
    for answer, chunk, question in corpus:
        expected = _base(answer, chunk, question)
        for names in (ACME, UNNAMED, ("Aurora", ("Glow", None))):
            assert _framed(answer, names, chunk, question) == expected, (answer, names)
        # No names: the base scorer, with and without the question.
        assert score_grounding(answer, [chunk], False, question=question) == expected
        assert score_grounding(answer, [chunk], False) == _base(answer, chunk, None)


def test_framing_strip_is_linear_on_adversarial_50kb_inputs() -> None:
    long_name = ("Luciel", (" ".join(["a"] * 39), "Acme Store"))
    cases: list[tuple[str, Names]] = [
        ("I'm Luciel, an AI assistant for " + "Acme " * 10000, ACME),
        ("I'm Luciel, an AI assistant for " + "a " * 25000, long_name),
        ("I'm Luciel" + ", " * 25000 + "x", ACME),
        ("I" + " " * 50000 + "m", ACME),
        ("I'm " * 12500, ACME),
        ("Hi — I'm Luciel. " * 3000, ACME),
        ("Hi" + " " * 50000 + "x.\nThanks" + " " * 50000 + "y", ACME),
        ("Hi " + "Aa" * 25000, ACME),
        ("Hi " + "a-" * 25000 + "!", ACME),
        ("Good morning" + " A" * 25000, ACME),
        (T6_FACT + "\n\nBest,\n" + "Luciel\n" * 7000, ACME),
        (T6_FACT + "\n" * 50000 + "Best,\nLuciel", ACME),
        ("Best,\n" * 8000 + "Luciel", ACME),
        ("x " + "\n\n Acme Store \n" * 3500, ACME),
        ("—" * 50000, ACME),
        # Almost-matches of every template, back to back.
        ("I'm Luciel, an AI assistant for this " * 1400, UNNAMED),
        ("Hi — I'm Luciel. I'm an AI assistant for Acme Store, and I can help answer " * 700, ACME),
    ]
    for text, names in cases:
        started = time.perf_counter()
        _framed(text, names)
        elapsed = time.perf_counter() - started
        assert elapsed < 0.2, (text[:40], elapsed)


def test_rule_one_quotes_the_composed_disclosure_and_the_scorer_removes_it() -> None:
    """Prompt and scorer share one composer, so the sentence the model is told to write
    is the sentence the scorer removes."""
    assert grounding.platform_disclosure_sentences is input_safety.platform_disclosure_sentences
    for assistant, business in (
        ("Luciel", "Acme Store"),
        ("Aurora", "Joe's Bikes & Boards"),
        ("Zoë", "Café Olé"),
        ("Maya's Front-Desk Assistant", "St. Mary's Dental"),
        ("Luciel", "this business"),
    ):
        sentence = compose_ai_disclosure(assistant_name=assistant, business_name=business)
        assert (
            input_safety.platform_disclosure_sentences(
                assistant_name=assistant, business_name=business
            )[0]
            == sentence
        )
        prompt = build_system_prompt(None, assistant_name=assistant, business_name=business)
        rule_one = next(line for line in prompt.splitlines() if line.startswith("1. AI IDENTITY"))
        assert f'in exactly these words: "{sentence}"' in rule_one
        # Every invariant of the old rule 1 is still stated.
        assert "first message of every conversation" in rule_one
        assert "AI assistant for the business" in rule_one
        assert "never claim to be human" in rule_one and "never deny being an AI" in rule_one
        quoted = re.search(r'in exactly these words: "(.*)"', rule_one)
        assert quoted is not None
        names: Names = (assistant, (business, None))
        assert _framed(f"{quoted.group(1)} {T6_FACT}", names) == _base(T6_FACT), business
    # The orchestrator composes rule 1 from the same names it later scores with.
    import uuid

    from app.runtime.orchestrator import Orchestrator

    orchestrator = Orchestrator(
        session=None,  # type: ignore[arg-type] — construction does no I/O
        router=None,  # type: ignore[arg-type]
        retriever=None,  # type: ignore[arg-type]
        admin_id=uuid.uuid4(),
        instance_id=uuid.uuid4(),
        enabled_channels=set(),
        business_name="Acme Store",
        assistant_name="Luciel",
        has_payment_method=False,
    )
    assert f'"{DISCLOSURE}"' in orchestrator.system_prompt
