"""The closed-account reduced surface, pinned structurally (round 7 WP-5 / owner
decision D5, 2026-09-14).

After closure the owner can sign in for 30 days to a surface that offers exactly two
actions — reopen the account and re-request the export — plus the one read that
renders it; NOTHING else is reachable. "Nothing else" cannot be proven by sampling a
few routes, so this walks every route the app registers and classifies it by the auth
dependency in its dependency tree:

* ``current_account_owner`` (open, or closed inside the reopen window) — exactly the
  routes listed in ``REDUCED_SURFACE``;
* ``current_session_subject`` alone (any valid session, a closed account included) —
  exactly the three auth routes in ``SESSION_ONLY``;
* everything else under the control-plane prefixes depends on ``current_admin``, which
  refuses a closed account with the typed 403.

A route added on the wrong dependency fails here, by name. No Postgres needed.
"""

from __future__ import annotations

from collections.abc import Iterator

from app.api import deps
from app.api.errors import ACCOUNT_CLOSED, ApiError, account_closed, register_error_handlers
from app.main import create_app
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

#: The whole D5 surface. Adding to this set widens what a CLOSED account can do.
REDUCED_SURFACE = frozenset(
    {
        ("GET", "/api/v1/admin/account"),
        ("POST", "/api/v1/admin/account/reopen"),
        ("POST", "/api/v1/admin/account/export"),
    }
)

#: Reachable with any valid session — a closed account's included. Read-only identity,
#: sign-out, and a resend that only ever acts on an UNVERIFIED account.
SESSION_ONLY = frozenset(
    {
        ("GET", "/api/v1/auth/me"),
        ("POST", "/api/v1/auth/logout"),
        ("POST", "/api/v1/auth/resend-verification"),
    }
)

#: Control-plane prefixes: every route under these acts on behalf of a signed-in owner.
OWNER_PREFIXES = ("/api/v1/admin/", "/api/v1/dashboard/", "/api/v1/billing/")

#: Routes under those prefixes that carry NO session by design, each with its own
#: authorization — and each with its own closed-account story.
PUBLIC_BY_DESIGN: frozenset[tuple[str, str]] = frozenset()


def _calls(dependant) -> set[object]:
    found: set[object] = set()
    for dep in dependant.dependencies:
        found.add(dep.call)
        found |= _calls(dep)
    return found


def _api_routes(routes, prefix: str = "") -> Iterator[tuple[str, APIRoute]]:
    """Every APIRoute with its full path. FastAPI >= 0.137 keeps an included router as
    one wrapper object (``original_router`` + ``include_context``) instead of copying
    its routes onto the app, so a flat ``app.routes`` scan sees almost nothing."""
    for route in routes:
        if isinstance(route, APIRoute):
            yield prefix + route.path, route
            continue
        inner = getattr(route, "original_router", None)
        if inner is not None:
            included_at = getattr(getattr(route, "include_context", None), "prefix", "") or ""
            yield from _api_routes(inner.routes, prefix + included_at)


def _owner_routes() -> list[tuple[str, str, set[object]]]:
    return [
        (method, path, _calls(route.dependant))
        for path, route in _api_routes(create_app().routes)
        for method in route.methods - {"HEAD", "OPTIONS"}
    ]


def _by_gate() -> dict[str, set[tuple[str, str]]]:
    gates: dict[str, set[tuple[str, str]]] = {"owner": set(), "admin": set(), "session": set()}
    for method, path, calls in _owner_routes():
        pair = (method, path)
        if deps.current_account_owner in calls:
            gates["owner"].add(pair)
        elif deps.current_admin in calls:
            gates["admin"].add(pair)
        elif deps.current_session_subject in calls:
            gates["session"].add(pair)
    return gates


def test_the_walk_sees_every_route_the_framework_serves() -> None:
    """The classification below is only as good as the walk: it must cover exactly the
    surface FastAPI itself reports (the same view the route-registry test freezes)."""
    served = {
        (method.upper(), path)
        for path, item in create_app().openapi()["paths"].items()
        for method in item
    }
    assert {(method, path) for method, path, _ in _owner_routes()} == served


def test_the_reduced_surface_is_exactly_the_d5_routes() -> None:
    assert _by_gate()["owner"] == REDUCED_SURFACE


def test_a_bare_session_reaches_only_identity_logout_and_resend() -> None:
    assert _by_gate()["session"] == SESSION_ONLY


def test_every_other_owner_route_is_behind_the_gate_that_refuses_a_closed_account() -> None:
    gates = _by_gate()
    checked = 0
    for method, path, _ in _owner_routes():
        if not path.startswith(OWNER_PREFIXES) or (method, path) in REDUCED_SURFACE:
            continue
        if (method, path) in PUBLIC_BY_DESIGN:
            continue
        assert (method, path) in gates["admin"], f"{method} {path} is not behind current_admin"
        checked += 1
    assert checked > 50, "the walk covered almost nothing — it is broken, not the surface"


def test_the_typed_refusal_carries_code_and_reopen_until_and_stays_a_403() -> None:
    """The wire shape the frontend keys on. ``message`` is unchanged, so a client that
    reads only ``message`` + status behaves exactly as it did before D5."""
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/closed")
    async def _closed() -> None:
        raise account_closed(
            closed_at="2026-09-01T12:00:00+00:00", reopen_until="2026-10-01T12:00:00+00:00"
        )

    @app.get("/plain")
    async def _plain() -> None:
        raise ApiError(409, "Conflict.")

    client = TestClient(app)
    closed = client.get("/closed")
    assert closed.status_code == 403
    assert closed.json() == {
        "message": "Account closed.",
        "code": ACCOUNT_CLOSED,
        "detail": {
            "closedAt": "2026-09-01T12:00:00+00:00",
            "reopenUntil": "2026-10-01T12:00:00+00:00",
        },
    }
    # An untyped error's body is byte-for-byte what it was: no null ``code``/``detail``.
    assert client.get("/plain").json() == {"message": "Conflict."}
