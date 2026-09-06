"""Request-scoped identifiers every log line can carry (F035)."""

from __future__ import annotations

import uuid
from contextvars import ContextVar, Token

trace_id_var: ContextVar[str] = ContextVar("luciel_trace_id", default="")
admin_id_var: ContextVar[str] = ContextVar("luciel_admin_id", default="")


def new_trace_id() -> str:
    return uuid.uuid4().hex


def bind_trace(trace_id: str) -> Token[str]:
    return trace_id_var.set(trace_id)


def bind_admin(admin_id: object) -> Token[str]:
    """Name the acting tenant for the rest of this request (set once auth resolved)."""
    return admin_id_var.set(str(admin_id) if admin_id else "")


def current_trace_id() -> str:
    return trace_id_var.get()


def current_admin_id() -> str:
    return admin_id_var.get()
