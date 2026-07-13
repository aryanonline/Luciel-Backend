"""Analytics & Reporting (DOCTRINE ANCHOR — Architecture §3.9).

Read-only aggregates over existing tenant-scoped stores (sessions, leads,
escalation_events, budget counters, admin_audit_log). No new write path; no new
PII; all aggregations within the WHERE admin_id boundary (RLS).

``service.py`` holds the real aggregate queries (conversations, leads,
escalations-by-signal, channel mix, busiest-times heatmap, conversion-by-channel)
plus the CSV rendering for Vision §7's "exportable reports" and the honest-gap
markers for the two metrics that need a trace store this codebase does not
persist yet (appointments-booked, top-knowledge-sources) and the one that needs
an ack-timestamp store that does not exist yet (response-time p50/p95) — see
service.py's module docstring for the full accounting of what is real vs. an
honest gap and why.
"""
