"""Widget DATA-PLANE wire shapes — mirror schemas/widget.ts EXACTLY (camelCase via
WireModel). ZERO admin surface (Space Instructions §1, §6.3 / plane separation).

Sources: Architecture §1.2 (endpoint family), §3.6.2 (paused → empty render),
§3.4.1b (at-cap graceful reply), §3.4.16 (AI-identity disclosure), §3.4.17
(Powered by VantageMind chrome).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import Field

from app.schemas_api.common import WireModel

# One visitor turn. Long enough for a pasted paragraph, short enough that a single
# request cannot carry an unbounded prompt into the model (cost + prompt-injection
# surface, §3.1.5 / §3.4.14). The shipped widget input enforces the same limit.
WIDGET_TEXT_MAX_CHARS = 4000

# "channel_off" (round 7 owner decision D9): the owner switched the Website chat
# channel off in the channels pillar. The widget renders NOTHING (exactly as for
# "paused") and re-checks the bootstrap so it can appear once the owner switches the
# channel back on; a conversation already in flight finishes.
WidgetRenderStateWire = Literal["active", "paused", "at_cap", "channel_off"]
# "human": a person's reply from the dashboard (2026-09-06 E2E walk, E2E-11) — the
# widget must never present it under the AI assistant's name.
WidgetRoleWire = Literal["visitor", "assistant", "human"]

# Persistent header label + powered-by chrome are platform literals — admin cannot
# remove the FACT of AI (§3.4.16) nor the platform attribution (§3.4.17).
AI_ASSISTANT_LABEL: Literal["AI assistant"] = "AI assistant"


class WidgetBootstrapOut(WireModel):
    render_state: WidgetRenderStateWire
    business_name: str
    assistant_name: str
    opening_message: str
    # Literals matched to schemas/widget.ts (z.literal). Frozen by contract.
    ai_assistant_label: Literal["AI assistant"] = "AI assistant"
    powered_by_vantage_mind: Literal[True] = True


class WidgetMessageOut(WireModel):
    message_id: uuid.UUID
    role: WidgetRoleWire
    text: str
    at: datetime


class WidgetSendRequest(WireModel):
    # embed_key arrives in the body (widget-adapters.ts: send → { embedKey, ...req }).
    embed_key: str
    session_id: uuid.UUID | None = None  # omitted on first message; server assigns.
    text: str = Field(min_length=1, max_length=WIDGET_TEXT_MAX_CHARS)


class WidgetBootstrapRequest(WireModel):
    embed_key: str
    # The session this tab already holds (sessionStorage), when it has one: a
    # paused Luciel still serves a conversation in progress (2026-09-06 E2E walk).
    session_id: uuid.UUID | None = None


class WidgetSendResult(WireModel):
    session_id: uuid.UUID
    reply: WidgetMessageOut
    render_state: WidgetRenderStateWire
