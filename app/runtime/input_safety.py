"""Prompt-injection defense + content moderation (DOCTRINE ANCHOR — §3.4.14/§3.4.15).

The LLM's instructions are NOT a security boundary (Locked Decision §7 #36).
Tenant isolation, tool authorization, and PII-disclosure rules hold in code even
against a fully convinced model. This module provides the two cognition-time
defenses that sit around the model:

  Defense (§3.4.14): retrieved/ingested/inbound content is wrapped as clearly
    delimited UNTRUSTED data, never as instructions. A successful injection's blast
    radius is bounded to "the model said something it shouldn't within this one
    tenant's own conversation" — never cross-tenant or an unauthorized action
    (those are enforced by RLS + the tool broker, which this module does not weaken).

  Content moderation (§3.4.15, Locked Decision §7 #37): always-on cognition that
    classifies abuse/threats/self-harm/illegal-solicitation at intake and escalates
    to a human; never an admin toggle.

Classifiers are deterministic heuristics here; the production classifier plugs in
behind the same interface. The structural guarantees (delimiting, the in-code
invariants) are what matter and are not classifier-dependent.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass

UNTRUSTED_OPEN = "<<<UNTRUSTED_DATA_BEGIN>>>"
UNTRUSTED_CLOSE = "<<<UNTRUSTED_DATA_END>>>"


def wrap_untrusted(content: str) -> str:
    """Wrap attacker-influenceable content (lead messages, crawled/ingested text)
    as clearly delimited untrusted data so it is never interpreted as instructions
    (§3.4.14). Any pre-existing delimiter tokens in the content are neutralized."""
    safe = content.replace(UNTRUSTED_OPEN, "").replace(UNTRUSTED_CLOSE, "")
    return f"{UNTRUSTED_OPEN}\n{safe}\n{UNTRUSTED_CLOSE}"


_INJECTION_PATTERNS = [
    re.compile(r"ignore (all|previous|the above|prior) (instructions|prompts|rules)", re.I),
    re.compile(r"disregard (your|all|the) (instructions|system prompt|rules)", re.I),
    re.compile(r"you are now\b", re.I),
    re.compile(r"\bsystem prompt\b", re.I),
    re.compile(r"reveal (your|the) (instructions|system prompt|prompt)", re.I),
    re.compile(r"pretend (you are|to be)\b", re.I),
    re.compile(r"\bdeveloper mode\b", re.I),
    re.compile(r"act as if you (have no|are not) (restrictions|an ai)", re.I),
]


def detect_injection(content: str) -> bool:
    """Heuristic detection of an injection attempt (for monitoring/audit only).

    Detection is NOT the security boundary — the boundary is the in-code invariants
    + the untrusted-data delimiting. A True here triggers a prompt_injection_detected
    audit event; it does not change isolation/authorization (those always hold).
    """
    text = content or ""
    return any(p.search(text) for p in _INJECTION_PATTERNS)


class ModerationCategory(str, enum.Enum):
    none = "none"
    threats_violence = "threats_violence"
    self_harm = "self_harm"
    illegal_solicitation = "illegal_solicitation"
    abuse_harassment = "abuse_harassment"


@dataclass
class ModerationResult:
    category: ModerationCategory
    should_escalate: bool


_MOD_PATTERNS: dict[ModerationCategory, re.Pattern[str]] = {
    ModerationCategory.threats_violence: re.compile(
        r"\b(kill|hurt|attack|bomb|shoot)\b.{0,20}\b(you|your|them|him|her|people)\b", re.I
    ),
    ModerationCategory.self_harm: re.compile(
        r"\b(kill myself|end my life|suicide|self[- ]harm)\b", re.I
    ),
    ModerationCategory.illegal_solicitation: re.compile(
        r"\b(buy|sell|launder|how to make)\b.{0,20}\b(drugs|weapon|counterfeit|explosive)\b", re.I
    ),
}


def moderate(content: str) -> ModerationResult:
    """Classify inbound content; harmful categories escalate to a human (§3.4.15).

    Luciel stays professional, does not retaliate, and escalates — it never
    produces harmful content. Self-harm and threats always escalate.
    """
    text = content or ""
    for category, pattern in _MOD_PATTERNS.items():
        if pattern.search(text):
            return ModerationResult(category, should_escalate=True)
    return ModerationResult(ModerationCategory.none, should_escalate=False)
