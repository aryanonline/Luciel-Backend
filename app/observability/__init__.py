"""Operational truth for a platform nobody watches by hand (2026-09-05 audit WP7).

Three small seams, deliberately dependency-free:

* ``context`` — the request's trace id and the acting tenant, carried in contextvars
  so every log line written while serving a request can name them (F035).
* ``logging`` — a JSON formatter that puts those ids on every line when
  ``LUCIEL_LOG_FORMAT=json`` (the ECS task definitions set it; local runs stay text).
* ``metrics`` — CloudWatch Embedded Metric Format lines (F032): a metric is one JSON
  log line CloudWatch extracts on ingest, so the runtime needs no CloudWatch API
  call, no extra IAM grant and no background flusher. The ObservabilityStack alarms
  on these names.
"""
