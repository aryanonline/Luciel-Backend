"""CloudWatch Embedded Metric Format emission (F032).

A metric is ONE JSON line on stdout carrying an ``_aws`` envelope; CloudWatch Logs
extracts it into the ``Luciel`` namespace on ingest. No API call, no extra IAM, no
flusher — and locally it is just a log line. The names below are the contract the
ObservabilityStack alarms reference:

    BudgetGateHit        a turn refused at the conversation cap (dim: Channel)
    EscalationFired      an escalation row written (dim: Signal)
    LLMFallbackUsed      the fallback model answered a turn
    LLMUnavailable       NO model served a turn: both providers failed (round 7
                         WP-6 item 4; dim: Channel) — the customer got the outage
                         line and an llm_unavailable row
    LLMCapExceeded       a turn refused by the tenant's per-minute model-call cap
                         (round 7 WP-2 item 4; dim: Channel)
    LLMInputTokens       per model call: uncached prompt tokens (base input rate)
    LLMOutputTokens      per model call: generated tokens
    LLMCacheReadTokens   per model call: prompt-cache hits
    LLMCacheWriteTokens  per model call: prompt-cache creation (Anthropic only)
                         — the four token metrics (round 7 WP-6 item 5; dims:
                         Provider, Model) are the ones that opt into the full set,
                         so per-model cost is queryable (app/runtime/providers.py)
    ConnectionUnhealthy  rows in error/expired after a tenant's health sweep
    ActiveSessions       open sessions after a tenant's finalize sweep
    SweepTenantFailure   a sweep's per-tenant work raised (dim: Sweep)
    KnowledgeRetrievalDegraded
                         a turn ran with NO knowledge because the embedding
                         provider failed, or an ingest could not embed
                         (dim: Reason, prefixed ingest: for the latter)
    EscalationBacklog    escalation pages still queued after the deferred sweep
                         ran (round 7 WP-4b; app/notifications/backlog.py)
    KnowledgeSyncJob     one per knowledge sync job, manual or scheduled (round 7
                         walk run 8; app/knowledge/sync_job.py; dim: Outcome —
                         synced | shrink_pending | unreachable | dead_credential |
                         embedding | error | interrupted | stopped |
                         already_running | halted; opts into the full set)

Dimension sets (round 7 WP-8): by default a datum publishes ONE dimension set,
``[Environment]`` — the set every ObservabilityStack alarm reads
(``dimensionsMap: { Environment }``). The extra values (Plane, Channel, Signal,
Sweep, Reason ...) are still on the document as plain properties, so Logs
Insights can filter and group by them for free; they only become billed custom
metrics when a caller opts in with ``full_dimensions=True``, which publishes the
full combination as a second set. Publishing both sets for every datum was
doubling the custom-metric count (each dimension combination is a separate
billed metric) for drill-downs nothing read.
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
    full_dimensions: bool = False,
) -> dict[str, object]:
    dims: dict[str, str] = {
        "Environment": environment or os.environ.get("LUCIEL_ENVIRONMENT") or "local",
        "Plane": os.environ.get("LUCIEL_PLANE") or "local",
        **(dimensions or {}),
    }
    # [Environment] alone is what the alarms read and the only set published by
    # default; the full combination is an opt-in second set (see module docstring).
    # Every value in ``dims`` stays on the document either way, as a property.
    dimension_sets: list[list[str]] = [["Environment"]]
    if full_dimensions:
        dimension_sets.append(list(dims.keys()))
    return {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": NAMESPACE,
                    "Dimensions": dimension_sets,
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
    full_dimensions: bool = False,
) -> None:
    """Emit one metric datum. Never raises: telemetry must not break a turn.

    ``full_dimensions=True`` additionally publishes the full dimension combination
    as a billed metric; the default publishes ``[Environment]`` only (the set the
    alarms read) and keeps the other values as document properties.
    """
    try:
        from app.config import get_settings  # noqa: PLC0415 — keep this module import-light

        environment = get_settings().environment
    except Exception:  # noqa: BLE001
        environment = None
    try:
        install_metrics_logger()
        _logger.info(
            json.dumps(
                emf_document(
                    name,
                    value,
                    unit=unit,
                    dimensions=dimensions,
                    environment=environment,
                    full_dimensions=full_dimensions,
                )
            )
        )
    except Exception:  # noqa: BLE001 — a metric is never worth an exception
        logging.getLogger(__name__).debug("metric_emit_failed name=%s", name, exc_info=True)
