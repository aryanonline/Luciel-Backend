"""Conversation counter + Stripe overage reporting (DOCTRINE ANCHOR — §3.4.1b).

Counter: once per session (first model call OR human-controlled-before-model),
idempotent on session id, synchronous write-through to Postgres (source of truth)
+ Redis (hot cache); reset on Stripe invoice.paid; no rollover. PAYG $39/100 above
free 50, rounded up per 100-block, monthly arrears via Stripe metered usage.
Lands in Arc 18. Build-status honesty: until write-through ships, the §4.5
Redis-down fallback is NOT claimed operative.
"""

from __future__ import annotations
