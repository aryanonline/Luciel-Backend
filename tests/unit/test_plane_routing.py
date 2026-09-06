"""Every mounted route belongs to exactly one plane (2026-09-05 audit F067).

The ALB forwards CONTROL_PLANE_PREFIXES to the control-plane service (whose role
can create tenant secrets, write exports and store knowledge originals) and
everything else to the narrower data plane. A router mounted under a prefix on
neither list would silently land on the data plane — and fail the moment it
needed a control-plane grant — so this test refuses it at CI time. The CDK test
(infra/test/plane-routing.test.ts) pins the same list on the listener rules."""

from __future__ import annotations

import re

from app.main import CONTROL_PLANE_PREFIXES, DATA_PLANE_PUBLIC_PREFIXES, create_app

_PLATFORM = ("/docs", "/redoc", "/openapi.json")

# The CDK list, mirrored verbatim so a drift between the two files fails here.
_CDK_PATTERNS = [
    "/api/v1/admin/*",
    "/api/v1/auth/*",
    "/api/v1/dashboard/*",
    "/api/v1/connections/*",
    "/api/v1/exports/*",
    "/api/v1/billing",
    "/api/v1/billing/*",
    "/api/v1/contact",
    "/api/v1/contact/*",
    "/api/v1/escalation-contact/*",
]


def _paths() -> set[str]:
    # The public schema, not app.routes: newer FastAPI wraps included routers in an
    # opaque _IncludedRouter (same approach as tests/unit/test_route_registry.py).
    app = create_app()
    paths = set(app.openapi()["paths"].keys())
    paths.update({"/docs", "/redoc", "/openapi.json", "/healthz"})
    return paths


def _plane_of(path: str) -> str | None:
    for prefix in CONTROL_PLANE_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return "control"
    for prefix in DATA_PLANE_PUBLIC_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return "data"
    if path.startswith(_PLATFORM):
        return "platform"
    return None


def test_every_route_is_on_exactly_one_plane():
    unclassified = sorted(p for p in _paths() if _plane_of(p) is None)
    assert unclassified == [], f"routes on neither plane list: {unclassified}"


def test_the_cdk_path_patterns_match_the_python_prefixes():
    covered = set()
    for pattern in _CDK_PATTERNS:
        covered.add(pattern[:-2] if pattern.endswith("/*") else pattern)
    assert covered == set(CONTROL_PLANE_PREFIXES)
    # Every control-plane prefix has BOTH the bare path and the subtree covered by
    # the CDK rules, either via `/prefix/*` (subtree) plus an exact entry when the
    # bare path is itself a route.
    bare_routes = {p for p in _paths() if p in CONTROL_PLANE_PREFIXES}
    for bare in bare_routes:
        assert bare in _CDK_PATTERNS, f"{bare} is a route but the ALB only matches {bare}/*"


def test_webhooks_and_the_widget_stay_on_the_data_plane():
    data = {p for p in _paths() if _plane_of(p) == "data"}
    assert any(re.match(r"^/api/v1/webhooks/", p) for p in data)
    assert any(p.startswith("/api/v1/chat-widget") for p in data)
    assert "/healthz" in data
    assert not any(p.startswith("/api/v1/admin") for p in data)
