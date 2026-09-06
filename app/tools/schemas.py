"""Provider-neutral JSON Schemas for the tool catalog (§3.3.1 / audit round 3).

THE single source of truth for what the model is told about each tool. Both LLM
providers derive their native tool declarations from these defs (Anthropic
``tools=[{name, description, input_schema}]``; OpenAI function-calling), and the
system prompt's tool-catalog stanza lists the same names and descriptions — so
the model can never be told about a tool the broker doesn't know, and vice versa.

Two invariants, pinned by tests/unit/test_tool_schemas.py:
  * every ``catalog.ALL_TOOL_IDS`` entry has exactly one schema here;
  * each schema's ``required`` list is a superset of the broker-enforced
    ``catalog.TOOL_REQUIRED_INPUTS`` for that tool (the schema may describe more,
    never less, than the broker will reject on).

Formats mirror the effect layer exactly (calendar_backend.parse_interval et al) —
a schema that described a friendlier format than the backend parses would make the
model produce inputs the tool maps to ``failed``, which reads to the lead as a
capability outage.
"""

from __future__ import annotations

from typing import Any

# Write tools: externally observable writes that require infer-but-confirm before
# dispatch (§3.3.7 rule 2). Moved here from the (previously dead) broker constant so
# the prompt stanza and any enforcement read one list.
ACTION_TOOLS = frozenset(
    {
        "book_appointment",
        "reschedule_appointment",
        "cancel_appointment",
        "send_email",
        "send_sms",
        "schedule_callback",
        "push_to_crm",
    }
)

_WINDOW_DESC = (
    "UTC ISO-8601 window to check: a bare date YYYY-MM-DD for that whole day, a "
    "single instant, or 'start/end' (e.g. 2026-08-25T14:00:00/2026-08-25T17:00:00)."
)
_SLOT_DESC = (
    "The exact slot to book, as one of the ISO start times check_availability "
    "returned. Never a time you have not seen confirmed open."
)
_EMAIL_DESC = (
    "The customer's email address. If the conversation has not produced one, ask "
    "for it; if you are reusing one they gave earlier, confirm it first."
)

TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "check_availability": {
        "description": "Read the connected calendar's free/busy to find open slots.",
        "input_schema": {
            "type": "object",
            "properties": {"window": {"type": "string", "description": _WINDOW_DESC}},
            "required": ["window"],
        },
    },
    "book_appointment": {
        "description": (
            "Write a booking into the connected calendar. Call check_availability "
            "first and book only a confirmed-open slot."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "slot": {"type": "string", "description": _SLOT_DESC},
                "email": {"type": "string", "description": _EMAIL_DESC},
            },
            "required": ["slot", "email"],
        },
    },
    "reschedule_appointment": {
        "description": (
            "Move the customer's existing booking to a new slot. The booking is "
            "located by their email first; pass booking_ref only if you have one."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "email": {"type": "string", "description": _EMAIL_DESC},
                "slot": {"type": "string", "description": _SLOT_DESC},
                "booking_ref": {
                    "type": "string",
                    "description": "The booking reference, when the customer supplied one.",
                },
            },
            "required": ["email", "slot"],
        },
    },
    "cancel_appointment": {
        "description": (
            "Cancel the customer's existing booking, located by their email; pass "
            "booking_ref only if you have one."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "email": {"type": "string", "description": _EMAIL_DESC},
                "booking_ref": {
                    "type": "string",
                    "description": "The booking reference, when the customer supplied one.",
                },
            },
            "required": ["email"],
        },
    },
    "lookup_record": {
        "description": (
            "Query the business's connected record source for an exact, current "
            "record (e.g. a product, service, or price). Use it instead of guessing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to look up, in the customer's own terms.",
                }
            },
            "required": ["query"],
        },
    },
    "send_email": {
        "description": (
            "Send an outbound email to the customer on a channel they are NOT "
            "currently talking on (a same-channel reply is never this tool)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": _EMAIL_DESC},
                "body": {"type": "string", "description": "The message body, plain text."},
            },
            "required": ["to", "body"],
        },
    },
    "send_sms": {
        "description": (
            "Send an outbound SMS to the customer on a channel they are NOT "
            "currently talking on (a same-channel reply is never this tool)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": "The customer's phone number in +E.164 form; confirm it first.",
                },
                "body": {"type": "string", "description": "The message body, plain text."},
            },
            "required": ["to", "body"],
        },
    },
    "schedule_callback": {
        "description": (
            "Queue a future follow-up message from you to this customer, delivered "
            "later over an already-connected channel."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "fire_at": {
                    "type": "string",
                    "description": "When to send it — a UTC ISO-8601 instant in the future.",
                },
                "channel": {
                    "type": "string",
                    "enum": ["email", "sms", "whatsapp"],
                    "description": (
                        "The delivery channel; it must already be connected and the "
                        "customer must have given an address on it. Text channels only "
                        "— a callback is a message, never an outbound call, and a web "
                        "chat visitor cannot be reached once they leave."
                    ),
                },
                "message": {"type": "string", "description": "The follow-up message text."},
            },
            "required": ["fire_at", "channel", "message"],
        },
    },
    "push_to_crm": {
        "description": (
            "Write the captured lead's facts to the business's connected CRM, "
            "updating the same record on repeat pushes (never a duplicate)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "lead_key": {
                    "type": "string",
                    "description": "The lead's stable contact key — their email address.",
                },
                "fields": {
                    "type": "object",
                    "description": (
                        "Captured facts to write (name, phone, intent, budget, next step) "
                        "— only facts the customer actually stated."
                    ),
                },
            },
            "required": ["lead_key", "fields"],
        },
    },
    "bring_your_own_webhook": {
        "description": "Post structured data to the endpoint the business registered.",
        "input_schema": {
            "type": "object",
            "properties": {
                "payload": {
                    "type": "object",
                    "description": "The JSON payload to deliver to the business's endpoint.",
                }
            },
            "required": ["payload"],
        },
    },
}


def build_tool_defs(tool_ids: set[str] | frozenset[str]) -> list[dict[str, Any]]:
    """Provider-neutral tool defs for the given (already-gated) tool ids.

    Sorted for deterministic prompts/caching. Unknown ids are skipped — the broker
    is the enforcement layer; this function only describes what it will accept.
    """
    defs: list[dict[str, Any]] = []
    for tool_id in sorted(tool_ids):
        spec = TOOL_SCHEMAS.get(tool_id)
        if spec is None:
            continue
        defs.append(
            {
                "tool_id": tool_id,
                "description": spec["description"],
                "input_schema": spec["input_schema"],
            }
        )
    return defs
