"""The page size every owner-facing list endpoint shares (2026-09-05 audit WP7).

The cap stops one tenant's history from being one unbounded query. A list a client
pages through defaults to PAGE_DEFAULT; a list the dashboard reads whole today (it has
no pager for it yet) defaults to PAGE_MAX, so only a list longer than the cap is cut.
tests/unit/test_list_endpoints_bounded.py refuses a new unbounded array route.
"""

from __future__ import annotations

PAGE_DEFAULT = 200
PAGE_MAX = 500
