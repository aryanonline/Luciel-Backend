"""Structured JSON logging with the request's trace id and tenant (F035)."""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime

from app.observability.context import admin_id_var, trace_id_var


class StructuredFormatter(logging.Formatter):
    """One JSON object per line. ``record.getMessage()`` is untouched, so tests that
    read messages through caplog see exactly what they always did."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        trace_id = trace_id_var.get()
        if trace_id:
            payload["trace_id"] = trace_id
        admin_id = admin_id_var.get()
        if admin_id:
            payload["admin_id"] = admin_id
        plane = os.environ.get("LUCIEL_PLANE")
        if plane:
            payload["plane"] = plane
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


_TARGET_LOGGERS = ("", "uvicorn", "uvicorn.access", "uvicorn.error", "app")


def install_structured_logging(log_format: str) -> bool:
    """Format every handler on the root + uvicorn + app loggers as JSON.

    Returns True when JSON was installed. ``text`` (the local default) leaves the
    handlers alone so a developer terminal stays readable."""
    if log_format != "json":
        return False
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=logging.INFO)
    formatter = StructuredFormatter()
    for name in _TARGET_LOGGERS:
        for handler in logging.getLogger(name).handlers:
            handler.setFormatter(formatter)
    return True
