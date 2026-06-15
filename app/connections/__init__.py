"""External Connections Layer (DOCTRINE ANCHOR — Architecture §3.8).

One model for everything external: channels, action tools, live knowledge
sources, escalation delivery — four uses of one connection abstraction. Universal
gate (enabled/active AND connection-healthy), health-check/refresh worker keyed to
auth_class, fail-safe (sync fail-fast-escalate; deferred bounded retry queue with
24h TTL), credential storage in Secrets Manager (pointer in Postgres). Lands in
Arc 17 (with the schema/health-worker scaffolded earlier per arc needs).
"""
