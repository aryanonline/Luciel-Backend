"""The cutover defaults to mock — unset env leaves the pre-cutover behavior intact.

This is the RUNTIME-UNCHANGED proof at the seam level: with no integration-mode env
set, the secret store is Local, the cap store is in-memory, and the platform-secret
parser yields nothing — exactly the state the system shipped in before the cutover.
"""

from __future__ import annotations

import app.connections.secrets as secrets_mod
import app.runtime.llm_cap as cap_mod
from app.config import Settings, parse_platform_secret
from app.connections.secrets import LocalSecretStore, get_secret_store
from app.runtime.llm_cap import InMemoryLLMCapStore, get_llm_cap_store


def test_settings_default_is_mock() -> None:
    s = Settings()
    assert s.integration_mode == "mock"
    assert s.platform_secret == "{}"


def test_parse_platform_secret_empty_cases() -> None:
    assert parse_platform_secret("{}") == {}
    assert parse_platform_secret("") == {}
    assert parse_platform_secret("not-json") == {}
    assert parse_platform_secret('{"ANTHROPIC_API_KEY": "fake"}') == {"ANTHROPIC_API_KEY": "fake"}


def test_secret_store_default_is_local(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(secrets_mod, "_default", None)
    store = get_secret_store()
    assert isinstance(store, LocalSecretStore)


def test_cap_store_default_is_in_memory(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(cap_mod, "_store", None)
    store = get_llm_cap_store()
    assert isinstance(store, InMemoryLLMCapStore)
