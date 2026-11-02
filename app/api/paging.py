"""The page sizes every owner-facing list endpoint shares (2026-09-05 audit WP7).

The cap stops one tenant's history from being one unbounded query. A list a client
pages through defaults to PAGE_DEFAULT. A list the dashboard reads whole today (it has
no pager for it yet) defaults to its ceiling, so only a list longer than the ceiling is
cut. tests/unit/test_list_endpoints_bounded.py refuses a new unbounded route.

Two of those whole-read lists answer TOTAL_COUNT_HEADER, so a client can tell it was
handed less than everything: the knowledge sources and the transcript. The body stays a
bare array. The dashboard does not read the header yet, so today nothing on screen says
a list was cut; the CORS expose in app/main.py is what lets it.
"""

from __future__ import annotations

PAGE_DEFAULT = 200
PAGE_MAX = 500

# The knowledge list's ceiling. A row count, not a byte count: the storage quota is bytes
# only, and one Drive sync alone can add 5,000 files (50 pages of 100), so PAGE_MAX would
# hide real sources from the only page that can view, rename or delete them.
SOURCES_MAX = 10_000

TOTAL_COUNT_HEADER = "X-Total-Count"
