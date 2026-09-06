"""CloudWatch Embedded Metric Format emission (F032).

A metric is ONE JSON line on stdout carrying an ``_aws`` envelope; CloudWatch Logs
extracts it into the ``Luciel`` namespace on ingest. No API call, no extra IAM, no
flusher — and locally it is just a log line. The names below are the contract the
ObservabilityStack alarms reference:

    BudgetGateHit        a turn refused at the conversation cap (dim: Channel)
    EscalationFired      an escalation row written (dim: Signal)
    LLMFallbackUsed      the fallback model answered a turn
    ConnectionUnhealthy  rows in error/expired after a tenant's health sweep
    ActiveSessions       open sessions after a tenant's finalize sweep
    SweepTenantFailure   a sweep's per-tenant work raised (dim: Sweep)
    KnowledgeRetrievalDegraded
                         a turn ran with NO knowledge because the embedding
                         provider failed, or an ingest could not embed
                         (dim: Reason, prefixed ingest: for the latter)
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Final, TextIO

NAMESPACE: Final = "Luciel"
METRICS_LOGGER_NAME: Final = "luciel.metrics"

_logger = logging.getLogger(METRICS_LOGGER_NAME)


class _StdoutHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """Writes to whatever ``sys.stdout`` is at emit time (a captured stream in tests,
    the container's stdout in ECS) instead of the object bound at install time."""

    _luciel_metrics = True

    def __init__(self) -> None:
        super().__init__(sys.stdout)

    @property
    def stream(self) -> TextIO:
        return sys.stdout

    @stream.setter
    def stream(self, value: object) -> None:  # StreamHandler assigns in __init__
        return None


def install_metrics_logger() -> None:
    """A dedicated stdout handler with a bare ``%(message)s`` format: the EMF envelope
    must reach CloudWatch verbatim, never wrapped by the structured formatter."""
    if any(getattr(h, "_luciel_metrics", False) for h in _logger.handlers):
        return
    handler = _StdoutHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(handler)
    _logger.propagate = False
    _logger.setLevel(logging.INFO)


def emf_document(
    name: str,
    value: float,
    *,
    unit: str = "Count",
    dimensions: dict[str, str] | None = None,
    environment: str | None = None,
) -> dict[str, object]:
    dims: dict[str, str] = {
        "Environment": environment or os.environ.get("LUCIEL_ENVIRONMENT") or "local",
        "Plane": os.environ.get("LUCIEL_PLANE") or "local",
        **(dimensions or {}),
    }
    return {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": NAMESPACE,
                    # Two dimension sets: the full one for drill-down and
                    # [Environment] alone so an alarm can sum the environment's
                    # total without enumerating channels, planes or signals.
                    "Dimensions": [["Environment"], list(dims.keys())],
                    "Metrics": [{"Name": name, "Unit": unit}],
                }
            ],
        },
        **dims,
        name: value,
    }


def emit_metric(
    name: str,
    value: float = 1.0,
    *,
    unit: str = "Count",
    dimensions: dict[str, str] | None = None,
) -> None:
    """Emit one metric datum. Never raises: telemetry must not break a turn."""
    try:
        from app.config import get_settings  # noqa: PLC0415 — keep this module import-light

        environment = get_settings().environment
    except Exception:  # noqa: BLE001
        environment = None
    try:
        install_metrics_logger()
        _logger.info(
            json.dumps(
                emf_document(name, value, unit=unit, dimensions=dimensions, environment=environment)
            )
        )
    except Exception:  # noqa: BLE001 — a metric is never worth an exception
        logging.getLogger(__name__).debug("metric_emit_failed name=%s", name, exc_info=True)
