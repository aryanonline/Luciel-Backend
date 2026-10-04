"""Conversation memory + summarization cognition (Architecture §3.4.6 / §3.4.7).

Two cognition functions the loop wires in:

  Within-session memory (§3.4.6): full conversation context for the live session
    (the transcript rows for this session).

  Cross-session memory (§3.4.6): prior-session SUMMARIES for the same customer
    identifier — never the full prior transcripts (§3.4.8 reopened threads). This
    is gated by the HARD RULE in §3.4.9: cross-session memory is retrievable only
    for strong / platform-scoped-strong identifiers; a weak widget token never
    inherits history. The gate is sessions.can_carry_cross_session_memory.

  Summarization (§3.4.7): at session close (end signal or inactivity timeout) the
    runtime produces a structured summary (intent, key facts, actions, next step),
    persisted to the conversation record and available as prior-session context.
    Summarization runs on every conversation on every tier — not admin-configurable.

Improvement is through CONTEXT, never model modification (§3.4.6): no tenant
conversation is used to train/fine-tune weights. These functions only read/write
the per-tenant context store; they never touch a model artifact.

The summarizer here is a deterministic, dependency-free structurer so the loop +
tests run without an LLM. Round 7 WP-3c item 4 bound the production summarizer in
front of it: ``summarize_session`` asks the fast model (live posture, the router's
fast tier) for the §3.4.7 shape — intent / actions taken / open items / next step —
grounded ONLY in the transcript, and falls back to the deterministic structurer
whenever the model is unavailable, over cap, off-shape or ungrounded. Both paths
run the §3.4.11 minimization step (``minimize``): a summary outlives the transcript
(one year, cross-session injection), so contact facts become presence flags
("email on file"), card-number-like and long digit strings are redacted, and the
intent line is a word-boundary truncation with identifiers removed. A summary is
constrained to facts present in the transcript (§3.4.13 / §3.4.7).
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.conversation import SessionSummary, Transcript
from app.runtime import provider_factory
from app.runtime.sessions import can_carry_cross_session_memory

if TYPE_CHECKING:
    from app.runtime.llm_router import LLMRouter

logger = logging.getLogger(__name__)

# Cross-session memory INJECTION bound (§3.4.10 / Decision #38): at most N most-recent
# summaries within a rolling 12-month window, so the injected context — and therefore
# the footprint — PLATEAUS. Platform constant, not admin-configurable.
CROSS_SESSION_SUMMARY_LIMIT = 10
CROSS_SESSION_SUMMARY_WINDOW = timedelta(days=365)


async def load_session_history(
    session: AsyncSession, *, admin_id: uuid.UUID, session_id: uuid.UUID
) -> list[Transcript]:
    """Within-session memory (§3.4.6): the ordered transcript for the live session."""
    rows = (
        (
            await session.execute(
                select(Transcript)
                .where(
                    Transcript.admin_id == admin_id,
                    Transcript.session_id == session_id,
                )
                .order_by(Transcript.at.asc())
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def load_cross_session_summaries(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    channel: str,
    resolved_lead_id: uuid.UUID | None,
    exclude_session_id: uuid.UUID | None = None,
    limit: int = CROSS_SESSION_SUMMARY_LIMIT,
    now: datetime | None = None,
) -> list[str]:
    """Cross-session memory (§3.4.6): prior-session summaries for this customer.

    Returns [] when the channel identifier is weak (HARD RULE §3.4.9) or no lead is
    resolved — a weak widget token never inherits another lead's history. Summaries
    are scoped by (admin_id, instance_id, resolved_lead_id); RLS additionally scopes
    by admin_id so cross-tenant bleed is impossible.

    Injection is bounded (§3.4.10 / Decision #38): at most ``limit`` (default N=10)
    most-recent summaries WITHIN a rolling 12-month window — summaries older than the
    window are excluded so injected context, and thus footprint, plateaus.
    """
    if resolved_lead_id is None:
        return []
    # HARD RULE §3.4.9: memory attaches only to a proven identity. On strong /
    # platform-scoped-strong channels the transport identifier IS the proof. On
    # the weak widget (round 5C item 19) a session carries resolved_lead_id ONLY
    # when the visitor volunteered a strong identifier IN THIS SESSION (weak
    # tokens never inherit a binding across sessions — sessions resume by id
    # alone, and capture binds only on a volunteered email/phone), so a resolved
    # lead here is the same in-conversation proof, and the returning visitor who
    # re-states their email gets their continuity instead of a blank slate. A
    # matching identifier still never authorizes PII READBACK — that rule lives
    # in the prompt/readback layer, not here.
    if not can_carry_cross_session_memory(channel) and channel != "widget":
        return []
    now = now or datetime.now(UTC)
    window_start = now - CROSS_SESSION_SUMMARY_WINDOW
    q = (
        select(SessionSummary)
        .where(
            SessionSummary.admin_id == admin_id,
            SessionSummary.instance_id == instance_id,
            SessionSummary.resolved_lead_id == resolved_lead_id,
            SessionSummary.created_at >= window_start,
        )
        .order_by(SessionSummary.created_at.desc())
        .limit(limit)
    )
    if exclude_session_id is not None:
        q = q.where(SessionSummary.session_id != exclude_session_id)
    rows = (await session.execute(q)).scalars().all()
    return [r.summary for r in rows]


def summarize(history: list[Transcript]) -> str:
    """Structured conversation summary (§3.4.7): intent, key facts, actions, next step.

    Deterministic, transcript-grounded structurer (§3.4.13: no facts the transcript
    does not contain) — the mock posture's summarizer and the live posture's
    FALLBACK (``summarize_session`` binds the fast model in front of it). Every line
    it echoes passes the §3.4.11 minimization step first (round 7 WP-3c item 4).
    """
    if not history:
        return "No conversation content."
    lead_msgs = [t.text for t in history if t.role == "lead"]
    luciel_msgs = [t.text for t in history if t.role in ("luciel", "human_agent")]
    intent = minimize(lead_msgs[0]) if lead_msgs else "(no lead message)"
    last_action = minimize(luciel_msgs[-1]) if luciel_msgs else "(no response)"
    # Truncate to keep the persisted summary bounded (one-line cognition summary) —
    # at a word boundary, so the cut never leaves half an identifier behind.
    intent = _truncate(intent, SUMMARY_LINE_LIMIT)
    last_action = _truncate(last_action, SUMMARY_LINE_LIMIT)
    summary = f"Intent: {intent} | Last response: {last_action} | Turns: {len(history)}"
    # Key facts the lead volunteered (§3.4.7 "key facts"), lifted from THEIR turns only
    # — a number Luciel quoted is not a fact about the lead. Deterministic and
    # transcript-bound, so the cross-session memory can say "you mentioned a $5k
    # budget last time" without inventing it (2026-09-05 audit). Contact facts are
    # PRESENCE flags (§3.4.11): the value lives on the lead row, never in a summary.
    facts = _key_facts(" ".join(lead_msgs))
    if facts:
        summary += " | Facts: " + "; ".join(facts)
    handled_by_person = any(t.role == "human_agent" for t in history)
    if handled_by_person:
        summary += " | A person joined this conversation"
    return summary[:SUMMARY_LIMIT]


# --- §3.4.11 minimization (round 7 WP-3c item 4) ---------------------------------- #
#: A persisted summary line / the whole summary are bounded.
SUMMARY_LINE_LIMIT = 160
SUMMARY_LIMIT = 600
EMAIL_ON_FILE = "email on file"
PHONE_ON_FILE = "phone on file"
NUMBER_REDACTED = "[number redacted]"

# One run of digits with the separators a human types inside a number. A card
# number (13-19 digits) and any other long digit string are redacted outright; a
# phone-shaped run becomes the presence flag; short numbers (a $5,000 budget, "2
# pm", "3 bedrooms") are facts the summary may keep. An ISO date is kept whole.
_DIGIT_RUN_RE = re.compile(r"\+?\d(?:[\d\s().-]*\d)?")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _replace_digit_run(m: re.Match[str]) -> str:
    raw = m.group(0)
    if _ISO_DATE_RE.match(raw):
        return raw
    digits = sum(ch.isdigit() for ch in raw)
    if digits >= 13:
        return NUMBER_REDACTED
    if digits >= 8:
        separated = raw.startswith("+") or any(ch in " ().-" for ch in raw)
        if separated or digits in (10, 11):
            return PHONE_ON_FILE
        return NUMBER_REDACTED
    return raw


def minimize(text: str) -> str:
    """The §3.4.11 minimization step: contact facts → presence flags, card-number-
    like and long digit strings → redacted, whitespace collapsed. Idempotent."""
    out = _EMAIL_RE.sub(EMAIL_ON_FILE, text or "")
    out = _DIGIT_RUN_RE.sub(_replace_digit_run, out)
    return " ".join(out.split())


def _truncate(text: str, limit: int) -> str:
    """Bound a line at a WORD boundary (a paraphrase-safe cut: never half a token)."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:-—") + "…"


# --- the bound summarizer (§3.4.7 shape, live posture) ------------------------------ #
SUMMARY_LABELS: tuple[str, ...] = ("Intent", "Actions taken", "Open items", "Next step")
SUMMARY_SYSTEM_PROMPT = (
    "You write the closing summary of one customer conversation for the business's own "
    "records. Use ONLY what the transcript says: never add, infer or guess a fact, a "
    "name, a date, a place or a figure that is not in it; when something is unknown, "
    'write "not stated". Never copy an email address, a phone number, a card number '
    'or any long number into the summary — write "email on file" or "phone on file" '
    "instead. Answer in exactly four lines, each at most 160 characters, in this shape "
    "and nothing else:\n"
    "Intent: <what the customer wanted>\n"
    'Actions taken: <what was done in the conversation, or "none">\n'
    'Open items: <what is still unresolved, or "none">\n'
    'Next step: <the agreed or obvious next step, or "not stated">'
)
#: The transcript handed to the model is bounded; the opening (the intent) and the
#: tail (the latest state) survive a cut in the middle.
_TRANSCRIPT_HEAD = 3000
_TRANSCRIPT_TAIL = 9000
_LABEL_RE = re.compile(
    r"^\s*[-*\d.\s]*\**\s*(intent|actions? taken|open items?|next steps?)\s*\**\s*:\s*(.*)$",
    re.I,
)
_WORD_RE = re.compile(r"[a-z]{4,}")
# For the grounding check only: a number with its typed separators INCLUDING commas
# ("$5,000" and "$5000" are the same figure), reduced to its digits.
_NUMBER_TOKEN_RE = re.compile(r"\d(?:[\d\s().,-]*\d)?")
# Words a summary uses to TALK ABOUT a conversation; they carry no transcript fact
# and are ignored by the grounding floor.
_SUMMARY_VOCAB: frozenset[str] = frozenset(
    {
        "intent",
        "actions",
        "taken",
        "open",
        "items",
        "next",
        "step",
        "none",
        "stated",
        "customer",
        "asked",
        "asks",
        "asking",
        "about",
        "wants",
        "wanted",
        "said",
        "says",
        "gave",
        "given",
        "provided",
        "file",
        "email",
        "phone",
        "luciel",
        "team",
        "member",
        "person",
        "sent",
        "request",
        "requested",
        "back",
        "follow",
        "with",
        "from",
        "their",
        "them",
        "they",
        "this",
        "that",
        "there",
        "have",
        "been",
        "were",
        "also",
        "into",
        "more",
        "than",
        "then",
        "when",
        "what",
        "which",
        "while",
        "would",
        "could",
        "should",
        "does",
        "will",
        "shall",
        "only",
        "some",
        "such",
        "wait",
        "waiting",
        "reply",
        "answer",
        "answered",
        "question",
        "questions",
        "details",
        "information",
        "shared",
        "confirm",
        "confirmed",
        "confirmation",
        "conversation",
        "message",
        "messages",
        "contact",
        "reach",
        "reached",
        "call",
        "callback",
        "text",
        "later",
        "soon",
        "business",
        "offered",
        "offer",
        "explained",
        "told",
        "know",
        "needs",
        "need",
        "unresolved",
        "pending",
        "agreed",
        "obvious",
        "still",
        "left",
        "made",
        "make",
        "come",
        "came",
        "help",
        "helped",
        "looking",
        "interested",
        "interest",
        "like",
        "want",
        "note",
        "noted",
        "mentioned",
        "mention",
        "unknown",
        "nothing",
        "further",
        "handed",
        "escalated",
        "passed",
        "human",
        "agent",
    }
)
_GROUNDING_FLOOR = 0.2


def transcript_for_summary(history: list[Transcript]) -> str:
    """The transcript as the model sees it: who said what, in order, bounded."""
    speaker = {"lead": "Customer", "luciel": "Luciel", "human_agent": "Team member"}
    lines = [f"{speaker.get(t.role, t.role)}: {' '.join((t.text or '').split())}" for t in history]
    text = "\n".join(lines)
    if len(text) <= _TRANSCRIPT_HEAD + _TRANSCRIPT_TAIL:
        return text
    return (
        text[:_TRANSCRIPT_HEAD]
        + "\n[… middle of the conversation omitted …]\n"
        + text[-_TRANSCRIPT_TAIL:]
    )


def _canonical_label(raw: str) -> str:
    key = raw.strip().lower()
    if key.startswith("intent"):
        return "Intent"
    if key.startswith("action"):
        return "Actions taken"
    if key.startswith("open"):
        return "Open items"
    return "Next step"


def _digit_tokens(text: str) -> set[str]:
    return {re.sub(r"\D", "", m) for m in _NUMBER_TOKEN_RE.findall(text)} - {""}


def shape_llm_summary(text: str, history: list[Transcript]) -> str | None:
    """The model's answer, verified and minimized — or None when it must not be used.

    Refuses (→ the deterministic fallback) when the four §3.4.7 lines are not all
    there, when a number in the summary does not occur in the transcript (a
    fabricated figure is the one hallucination a business record cannot carry), or
    when the summary's content words barely overlap the transcript at all. Every
    line is minimized (§3.4.11) whatever the model did with the instruction."""
    values: dict[str, str] = {}
    for line in (text or "").splitlines():
        m = _LABEL_RE.match(line)
        if not m:
            continue
        label = _canonical_label(m.group(1))
        values.setdefault(label, m.group(2).strip())
    if any(label not in values for label in SUMMARY_LABELS):
        return None
    transcript = transcript_for_summary(history)
    transcript_digits = _digit_tokens(transcript)
    transcript_words = set(_WORD_RE.findall(transcript.lower()))
    parts: list[str] = []
    candidate_words: set[str] = set()
    for label in SUMMARY_LABELS:
        value = _truncate(minimize(values[label]), SUMMARY_LINE_LIMIT) or "not stated"
        for token in _digit_tokens(value):
            if not any(token in known for known in transcript_digits):
                return None
        candidate_words |= set(_WORD_RE.findall(value.lower())) - _SUMMARY_VOCAB
        parts.append(f"{label}: {value}")
    if len(candidate_words) >= 4:
        overlap = len(candidate_words & transcript_words) / len(candidate_words)
        if overlap < _GROUNDING_FLOOR:
            return None
    summary = " | ".join(parts)
    if any(t.role == "human_agent" for t in history):
        summary += " | A person joined this conversation"
    return summary[:SUMMARY_LIMIT]


def _live_summary_router(admin_id: uuid.UUID) -> LLMRouter | None:
    """The tenant-capped router in live posture; None (→ deterministic) otherwise."""
    if get_settings().integration_mode != "live":
        return None
    try:
        return provider_factory.build_llm_router(admin_id=admin_id)
    except Exception:  # noqa: BLE001 — a misconfigured router never breaks a close
        logger.warning("session_summary_router_unavailable", exc_info=True)
        return None


async def summarize_session(
    history: list[Transcript],
    *,
    admin_id: uuid.UUID,
    router: LLMRouter | None = None,
) -> str:
    """The session's closing summary (§3.4.7), model-written when a fast LLM is available.

    Live posture (or an explicit ``router``): the fast tier is asked — the routing
    hints ``requires_tools=False`` / ``complexity=0`` are exactly what selects it —
    for the four-line shape, grounded only in the transcript, under the tenant's LLM
    cap. Any failure (provider down, cap exceeded, off-shape, ungrounded) lands on
    the deterministic structurer, which is also the whole story in mock posture:
    a session close never fails and never waits on a model that is not there."""
    if not history:
        return "No conversation content."
    fallback = summarize(history)
    if router is None:
        router = _live_summary_router(admin_id)
        if router is None:
            return fallback
    transcript = transcript_for_summary(history)
    try:
        resp = await router.complete(
            SUMMARY_SYSTEM_PROMPT,
            [{"role": "user", "content": transcript}],
            admin_id=admin_id,
            requires_tools=False,
            context_tokens=max(1, len(transcript) // 4),
            complexity=0.0,
        )
    except Exception:  # noqa: BLE001 — ProviderError, LLMCapExceeded, anything
        logger.warning("session_summary_llm_unavailable", exc_info=True)
        return fallback
    shaped = shape_llm_summary(resp.text, history)
    if shaped is None:
        logger.info("session_summary_llm_rejected")
        return fallback
    return shaped


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?<![\w.])(\+?\d[\d\s().-]{7,}\d)(?![\w.])")
_MONEY_RE = re.compile(
    r"(?:\$|£|€)\s?\d[\d,]*(?:\.\d+)?(?:\s?[kK])?"
    r"|\b\d[\d,]*\s?(?:k|K)\b(?=\s*(?:budget|dollars|usd|cad))"
)
_WHEN_RE = re.compile(
    r"\b(today|tomorrow|tonight|this (?:week|weekend|month)"
    r"|next (?:week|month|monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
    r"|(?:mon|tues|wednes|thurs|fri|satur|sun)day|\d{1,2}(?::\d{2})?\s?(?:am|pm))\b",
    re.IGNORECASE,
)


def _key_facts(text: str) -> list[str]:
    facts: list[str] = []
    seen: set[str] = set()

    def add(label: str, values: list[str], cap: int = 2) -> None:
        for raw in values:
            v = raw.strip().rstrip(",.;:")  # "$5,000," mid-sentence is the figure $5,000
            key = f"{label}:{v.lower()}"
            if key in seen:
                continue
            seen.add(key)
            facts.append(f"{label} {v}")
            if sum(1 for f in facts if f.startswith(label)) >= cap:
                break

    # §3.4.11 (round 7 WP-3c item 4): contact facts are PRESENCE flags. The values
    # live on the lead row (lead_capture) and are never repeated into a record that
    # outlives the transcript and is injected into later conversations.
    if _EMAIL_RE.search(text):
        facts.append(EMAIL_ON_FILE)
    if any(sum(ch.isdigit() for ch in m) >= 8 for m in _PHONE_RE.findall(text)):
        facts.append(PHONE_ON_FILE)
    # Budget / timing are read off the MINIMIZED text, so a card number can never be
    # mistaken for a figure and no digit string leaks through a fact.
    safe = minimize(text)
    add("budget", _MONEY_RE.findall(safe))
    add("timing", _WHEN_RE.findall(safe))
    return facts[:5]


async def persist_summary(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    session_id: uuid.UUID,
    resolved_lead_id: uuid.UUID | None,
    summary: str,
) -> SessionSummary:
    """Persist the session summary (§3.4.7) to the conversation record + summary store.

    Writes a session_summaries row (available as prior-session context, §3.4.6) and
    stamps the session.summary one-liner shown in the dashboard conversation list.
    """
    from app.models.conversation import Session as SessionModel

    row = SessionSummary(
        id=uuid.uuid4(),
        admin_id=admin_id,
        instance_id=instance_id,
        resolved_lead_id=resolved_lead_id,
        session_id=session_id,
        summary=summary,
    )
    session.add(row)
    sess = (
        await session.execute(
            select(SessionModel).where(
                SessionModel.admin_id == admin_id, SessionModel.id == session_id
            )
        )
    ).scalar_one_or_none()
    if sess is not None:
        sess.summary = summary
    await session.flush()
    return row
