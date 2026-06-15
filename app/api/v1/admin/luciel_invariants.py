"""Server-side enforcement of the LOCKED config invariants for the five pillars.

The Pydantic request models (StrictWireModel + Literal enums) already reject unknown
fields and out-of-enum values structurally. This module holds the invariants that
CANNOT be expressed structurally because they are cross-field or depend on the
instance's persisted state:

  * voice channel may be enabled ONLY if consent was previously acknowledged
    (needs the stored voiceConsentAcknowledgedAt) — Vision §3.1, Arch §3.1.2.
  * custom personality preset REQUIRES the four axes — Vision §3.5, Arch §3.4.3.
  * builtin cognition (capture_leads/escalate/handoff/summarize) is NEVER a togglable
    addon tool — Vision §3.2, Arch §3.8.7.
  * config mutation is gated by lifecycle state — Arch §3.6.

Each raises an ApiError with the status transport.ts expects:
  * 422 validation_error  — the body is internally malformed (missing axes, etc.).
  * 409 conflict          — the body is well-formed but conflicts with current state
                            (enabling voice before consent; editing a dead instance).
"""

from __future__ import annotations

from app.api.errors import conflict, validation_error
from app.models.instance import Instance, LucielState
from app.schemas_api.luciel import (
    UpdateChannelsRequest,
    UpdatePersonalityRequest,
)

# The 4 builtin cognition items are non-interactive and live OUTSIDE the addon tool
# registry (Vision §3.2, Arch §3.8.7). They must NEVER appear as togglable addon tools.
# AddonToolIdWire already excludes them, so a strict body can't name one — this constant
# backs an explicit, readable rejection and documents the locked list.
BUILTIN_COGNITION_IDS = frozenset({"capture_leads", "escalate", "handoff", "summarize"})

# Config writes are permitted only while the instance is live-and-editable. Per the
# Arch §3.6 lifecycle table: `active` and `paused` both RETAIN all data and are
# instantly reversible, so config edits are allowed. `luciel_grace_window` is the inert
# soft-deleted state (embed key revoked, not queryable) and `luciel_hard_deleted` is
# gone — editing config in either is meaningless, so we block it (409 conflict, matching
# the LifecycleError → conflict convention used by the lifecycle endpoints).
_CONFIG_EDITABLE_STATES = frozenset({LucielState.active, LucielState.paused})


def ensure_config_editable(inst: Instance) -> None:
    """Block config mutation unless the instance is active/paused (Arch §3.6)."""
    if inst.state not in _CONFIG_EDITABLE_STATES:
        # 409: well-formed request, but the instance's lifecycle state forbids edits.
        raise conflict(f"Configuration cannot be changed while the Luciel is {inst.state.value}.")


def _voice_consent_already_acknowledged(inst: Instance) -> bool:
    """True iff the persisted voice channel already carries a consent-ack timestamp."""
    for ch in inst.channels or []:
        if ch.get("id") == "voice" and ch.get("voiceConsentAcknowledgedAt"):
            return True
    return False


def validate_channels(req: UpdateChannelsRequest, inst: Instance) -> None:
    """Voice may be enabled only after consent-ack (Vision §3.1, Arch §3.1.2).

    The one-time consent gate: a voice channel can flip to enabled=true ONLY if the
    instance already has a voiceConsentAcknowledgedAt (set by POST /voice-consent). A
    body that both enables voice AND carries the ack inline is also allowed (the admin
    is acknowledging in the same write). 409 conflict because the body is well-formed
    but conflicts with the current (no-consent) state.
    """
    already_acked = _voice_consent_already_acknowledged(inst)
    for ch in req.channels:
        if ch.id == "voice" and ch.enabled:
            inline_ack = ch.voice_consent_acknowledged_at is not None
            if not (already_acked or inline_ack):
                raise conflict(
                    "Voice cannot be enabled until the one-time recording/AI "
                    "disclosure consent has been acknowledged."
                )


def validate_personality(req: UpdatePersonalityRequest) -> None:
    """Custom preset requires the 4 axes; non-custom ignores axes (Vision §3.5, §3.4.3).

    422 validation_error: a custom preset with no axes is an internally incomplete body.
    For a non-custom preset the doc says axes are absent/ignored — we do not error on
    stray axes, we simply do not persist them (handled by the endpoint), keeping the
    preset the single source of the personality shape.
    """
    if req.preset == "custom" and req.axes is None:
        raise validation_error(
            "A custom personality preset requires all four axes (tone, verbosity, formality, pace)."
        )
