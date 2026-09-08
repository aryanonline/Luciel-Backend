"""Personality preset/axes → system-prompt stanza (Vision §3.5, Arch §3.4.14).

Vision §3.5: each named preset "maps to a specific underlying configuration of four
axes — tone, verbosity, formality, and pace — that the platform composes into the
system prompt at runtime. The customer never sees the prompt; they see the named
preset." The ``custom`` preset exposes the four axes directly.

This module is the mapping + the composition. It is a PLATFORM-CONTROLLED stanza:
the axes come from admin config, but the rendered text is platform-authored, so an
admin can shape *how* Luciel talks without gaining a raw prompt-authoring surface.
The stanza is composed UNDER the invariant stanzas (Defense 6, §3.4.14) so it can
never suppress AI-identity disclosure, grounding, escalation, or moderation.
"""

from __future__ import annotations

AXIS_NAMES = ("tone", "verbosity", "formality", "pace")

# Preset → the four axes, each in [0, 1]. These are the "specific underlying
# configuration" Vision §3.5 refers to; the admin sees only the preset name.
#   tone:      0 = reserved/neutral      → 1 = warm/personable
#   verbosity: 0 = terse                 → 1 = expansive
#   formality: 0 = casual                → 1 = formal
#   pace:      0 = unhurried/thorough    → 1 = brisk/efficient
PRESET_AXES: dict[str, dict[str, float]] = {
    "warm_concierge": {"tone": 0.9, "verbosity": 0.6, "formality": 0.4, "pace": 0.4},
    "professional_advisor": {"tone": 0.5, "verbosity": 0.6, "formality": 0.85, "pace": 0.5},
    "friendly_expert": {"tone": 0.8, "verbosity": 0.5, "formality": 0.3, "pace": 0.7},
    "trusted_authority": {"tone": 0.4, "verbosity": 0.4, "formality": 0.9, "pace": 0.6},
}
DEFAULT_PRESET = "professional_advisor"

# Axis → (low-band phrasing, mid-band phrasing, high-band phrasing).
_AXIS_BANDS: dict[str, tuple[str, str, str]] = {
    "tone": (
        "reserved and matter-of-fact",
        "approachable without being effusive",
        "warm and personable",
    ),
    "verbosity": (
        "brief — one or two sentences unless more is genuinely needed",
        "moderately detailed",
        "thorough, giving the fuller explanation",
    ),
    "formality": (
        "casual and conversational; contractions are fine",
        "plainly professional",
        "formal and precise; avoid slang and contractions",
    ),
    "pace": (
        "unhurried — let the person set the rhythm",
        "steady",
        "brisk and efficient; get to the answer quickly",
    ),
}


def _band(value: float) -> int:
    if value < 0.34:
        return 0
    if value < 0.67:
        return 1
    return 2


def resolve_axes(personality: dict[str, object] | None) -> dict[str, float]:
    """Resolve the effective four axes for a personality config.

    ``custom`` reads the admin's explicit axes; a named preset reads its mapping. An
    unknown/absent preset falls back to :data:`DEFAULT_PRESET` so the stanza is always
    composable — a missing personality config must never mean "no personality".
    """
    cfg = personality or {}
    preset = str(cfg.get("preset") or DEFAULT_PRESET)
    if preset == "custom":
        raw = cfg.get("axes")
        axes = raw if isinstance(raw, dict) else {}
        resolved: dict[str, float] = {}
        for name in AXIS_NAMES:
            try:
                resolved[name] = min(1.0, max(0.0, float(axes[name])))
            except (KeyError, TypeError, ValueError):
                # An incomplete custom config (the API rejects it, but a legacy row
                # may exist) falls back per-axis rather than dropping the stanza.
                resolved[name] = PRESET_AXES[DEFAULT_PRESET][name]
        return resolved
    return dict(PRESET_AXES.get(preset, PRESET_AXES[DEFAULT_PRESET]))


def preset_label(personality: dict[str, object] | None) -> str:
    """The preset name as it should read in the prompt (``warm_concierge`` → Warm
    Concierge). ``custom`` renders as "Custom" since there is no named shape."""
    preset = str((personality or {}).get("preset") or DEFAULT_PRESET)
    if preset != "custom" and preset not in PRESET_AXES:
        preset = DEFAULT_PRESET
    return preset.replace("_", " ").title()


def compose_personality_stanza(personality: dict[str, object] | None) -> str:
    """Render the personality stanza appended under the platform invariants.

    Always returns a non-empty stanza: every account has a personality (the default
    preset when unset), and Vision §3.5 makes composing it a platform guarantee, not
    an optional extra.
    """
    axes = resolve_axes(personality)
    lines = [
        f"\n--- Personality (platform-composed from the '{preset_label(personality)}' "
        "preset; it shapes HOW you speak, never WHAT the rules above require) ---\n"
    ]
    for name in AXIS_NAMES:
        lines.append(f"- {name.capitalize()}: {_AXIS_BANDS[name][_band(axes[name])]}.\n")
    return "".join(lines)


def high_value_signals_of(personality: dict[str, object] | None) -> list[str]:
    """The owner's hot-lead phrases (round 6 WP-G, F102) — ``highValueSignals`` on the
    personality pillar; [] when unset. Cleaned the way the wire validator cleans them."""
    raw = (personality or {}).get("highValueSignals")
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        phrase = " ".join(str(item).split())
        if phrase:
            out.append(phrase)
    return out


def custom_rules_hit(personality: dict[str, object] | None, text: str) -> str | None:
    """The first owner phrase that appears in ``text`` (case- and whitespace-insensitive),
    or None. Feeds ``high_value_lead_score(custom_rules_hit=...)`` — the ONLY admin input to
    the fixed escalation signals (Locked Decision §7 #16)."""
    haystack = " ".join((text or "").lower().split())
    if not haystack:
        return None
    for phrase in high_value_signals_of(personality):
        if phrase.lower() in haystack:
            return phrase
    return None


def business_context_of(personality: dict[str, object] | None) -> str | None:
    """The admin-editable 280-char business-context field (Vision §3.5).

    Stored on the personality pillar as ``businessContext`` (the wire alias). Returns
    None when unset so the caller can fall back to the signup one-liner.
    """
    raw = (personality or {}).get("businessContext")
    ctx = str(raw).strip() if raw is not None else ""
    return ctx or None
