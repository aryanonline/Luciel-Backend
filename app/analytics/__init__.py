"""Analytics & Reporting (DOCTRINE ANCHOR — Architecture §3.9).

Read-only aggregates over existing tenant-scoped stores (sessions, leads,
escalation_events, trace store, budget counters). No new write path; no new PII;
all aggregations within the WHERE admin_id boundary (RLS). Lands in Arc 18.
"""
