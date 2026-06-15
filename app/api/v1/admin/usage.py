"""Budget monitoring API (DOCTRINE ANCHOR — Architecture §8: app/api/v1/admin/usage.py).

Serves GET /api/v1/admin/usage/overview and /audit-log. Reads budget counters and
analytics aggregates, tenant-scoped. Lands in Arc 18 (with auth/usage surfaced
incrementally). Placeholder preserves the doctrine-anchored path.
"""

from __future__ import annotations
