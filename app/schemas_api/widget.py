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

from app.schemas_api.common import WireModel

WidgetRenderStateWire = Literal["active", "paused", "at_cap"]
WidgetRoleWire = Literal["visitor", "assistant"]

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
    text: str


class WidgetBootstrapRequest(WireModel):
    embed_key: str


class WidgetSendResult(WireModel):
    session_id: uuid.UUID
    reply: WidgetMessageOut
    render_state: WidgetRenderStateWire
