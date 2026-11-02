"""The API schema and its viewers are served on a local checkout only.

/docs, /redoc and /openapi.json were public on both deployed API hosts: 111 paths, 64
of them admin and 9 webhook, fetched by an external scanner on 2026-10-29. The deploy
smoke probe proves the 404s against the live host; these pin the decision itself.
ComputeStack gives both planes LUCIEL_ENVIRONMENT=<env> (infra/test/hostnames.test.ts).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from app.config import LOCAL_ENVIRONMENT, get_settings
from app.main import create_app
from fastapi import FastAPI
from starlette.testclient import TestClient

_DOCS_PATHS = ("/docs", "/redoc", "/openapi.json")


@pytest.fixture(autouse=True)
def _fresh_settings() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _app(monkeypatch: pytest.MonkeyPatch, environment: str) -> FastAPI:
    monkeypatch.setenv("LUCIEL_ENVIRONMENT", environment)
    get_settings.cache_clear()
    return create_app()


def test_a_local_checkout_serves_them(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TestClient(_app(monkeypatch, LOCAL_ENVIRONMENT))
    for path in _DOCS_PATHS:
        assert client.get(path).status_code == 200, path


@pytest.mark.parametrize("environment", ["dev", "preprod", "prod", "a-mistyped-value"])
def test_anything_else_serves_none_of_them(
    monkeypatch: pytest.MonkeyPatch, environment: str
) -> None:
    client = TestClient(_app(monkeypatch, environment))
    for path in (*_DOCS_PATHS, "/docs/oauth2-redirect"):
        assert client.get(path).status_code == 404, path


@pytest.mark.parametrize("environment", [LOCAL_ENVIRONMENT, "prod"])
def test_the_schema_is_still_built_for_the_route_registry(
    monkeypatch: pytest.MonkeyPatch, environment: str
) -> None:
    """tests/unit/test_route_registry.py and test_plane_routing.py read app.openapi()."""
    assert "/api/v1/auth/me" in _app(monkeypatch, environment).openapi()["paths"]
