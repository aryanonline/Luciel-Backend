"""Tool contract + the five-outcome result contract (Architecture §3.3.1, §3.3.4).

Every tool (built-in and BYO) resolves to exactly one of five normalized outcome
classes, which is what makes accuracy a structural property of the tool layer:

  ok          — fully succeeded; payload carries the confirmed result.
  empty       — ran correctly, no matching result.
  ambiguous   — multiple candidates where one was expected.
  failed      — could not complete (connection/provider error, timeout, write conflict).
  missing_input — broker rejected pre-execution because a required input was absent
                  or schema-invalid; the action never ran.

Platform-wide accuracy rules (enforced by the broker / loop):
  1. No success-claim without an `ok` payload (§3.3.4 rule 1).
  2. Read-before-write for externally observable writes (§3.3.4 rule 2).
  3. Every outcome is traced (§3.3.4 rule 3).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Protocol


class Outcome(str, enum.Enum):
    ok = "ok"
    empty = "empty"
    ambiguous = "ambiguous"
    failed = "failed"
    missing_input = "missing_input"


@dataclass
class ToolResult:
    outcome: Outcome
    # payload carries confirming fields on ok; candidates on ambiguous; reason on failed.
    payload: dict[str, Any] = field(default_factory=dict)
    # For missing_input: the field names the loop must obtain (§3.3.7).
    missing_fields: list[str] = field(default_factory=list)
    # For failed: a non-secret reason string.
    reason: str | None = None

    @property
    def is_success(self) -> bool:
        return self.outcome is Outcome.ok


def ok(payload: dict[str, Any]) -> ToolResult:
    return ToolResult(Outcome.ok, payload=payload)


def empty(payload: dict[str, Any] | None = None) -> ToolResult:
    return ToolResult(Outcome.empty, payload=payload or {})


def ambiguous(candidates: list[dict[str, Any]]) -> ToolResult:
    return ToolResult(Outcome.ambiguous, payload={"candidates": candidates})


def failed(reason: str) -> ToolResult:
    return ToolResult(Outcome.failed, reason=reason)


def missing_input(fields: list[str]) -> ToolResult:
    return ToolResult(Outcome.missing_input, missing_fields=fields)


@dataclass
class ToolContext:
    admin_id: Any
    instance_id: Any
    # Connection status for the tool's required connection (resolved by the broker).
    connection_status: str | None = None
    # Live values gathered in the conversation (for required-input resolution).
    gathered: dict[str, Any] = field(default_factory=dict)


class Tool(Protocol):
    tool_id: str
    display_name: str
    description: str
    requires_connection: str | None  # connection_type or None (§3.8)
    requires_channels: frozenset[str]
    execution_mode: str  # "in_process" | "subprocess"
    # Required input field names validated before execution.
    required_inputs: frozenset[str]

    async def execute(self, inputs: dict[str, Any], context: ToolContext) -> ToolResult: ...
