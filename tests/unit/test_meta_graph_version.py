"""ONE Meta Graph API version, set in config, behind every Meta URL (round 7 WP-7).

The Graph version was pinned at v19.0 in four places (the WhatsApp/Messenger sender,
the asset lookup, the token refresh, the registry's dialog + exchange endpoints) and in
the identity probe, while every Instagram Login call ran on v23.0. Meta retires a Graph
version about two years after release, so a pin scattered across files is a pin that
gets forgotten until every send starts failing. These tests hold the line:

  * no versioned Meta URL — and no "v19.0"/"v23.0" literal — exists in ``app/`` outside
    ``app/config.py``;
  * every Meta URL the code builds follows ``meta_graph_version``, which the env var or
    the platform secret can move without a code change;
  * a malformed pin refuses to boot rather than 4xx-ing every Graph call.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from app.config import INSTAGRAM_GRAPH_HOST, PLATFORM_SECRET_KEYS, Settings, get_settings

_APP = Path(__file__).resolve().parents[2] / "app"
_LITERALS = ("v19.0", "v23.0")
_VERSIONED_META_URL = re.compile(r"(?:facebook|instagram)\.com/v\d+\.\d+")


def test_no_graph_version_literal_lives_outside_config() -> None:
    offenders: list[str] = []
    for path in _APP.rglob("*.py"):
        if path == _APP / "config.py":
            continue
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            if any(lit in line for lit in _LITERALS) or _VERSIONED_META_URL.search(line):
                offenders.append(f"{path.relative_to(_APP.parent)}:{number}: {line.strip()}")
    assert offenders == []


def test_the_default_is_the_version_instagram_already_ran_on() -> None:
    assert Settings(_env_file=None).meta_graph_version == "v23.0"  # type: ignore[call-arg]
    assert "META_GRAPH_VERSION" in PLATFORM_SECRET_KEYS


def test_the_platform_secret_moves_the_pin_and_a_bare_number_is_normalized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LUCIEL_META_GRAPH_VERSION", raising=False)
    s = Settings(
        _env_file=None,  # type: ignore[call-arg]
        platform_secret=json.dumps({"META_GRAPH_VERSION": "24.0"}),
    )
    assert s.meta_graph_version == "v24.0"


def test_the_env_var_wins_over_the_platform_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LUCIEL_META_GRAPH_VERSION", "v25.0")
    s = Settings(
        _env_file=None,  # type: ignore[call-arg]
        platform_secret=json.dumps({"META_GRAPH_VERSION": "v24.0"}),
    )
    assert s.meta_graph_version == "v25.0"


@pytest.mark.parametrize("bad", ["latest", "v23", "23", "v23.0/me", " "])
def test_a_malformed_pin_refuses_to_boot(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    monkeypatch.setenv("LUCIEL_META_GRAPH_VERSION", bad)
    with pytest.raises(RuntimeError, match="META_GRAPH_VERSION"):
        Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.fixture
def pinned(monkeypatch: pytest.MonkeyPatch):
    """Every Meta URL builder, read under a non-default pin."""
    monkeypatch.setenv("LUCIEL_META_GRAPH_VERSION", "v31.4")
    get_settings.cache_clear()  # type: ignore[attr-defined]
    yield "v31.4"
    get_settings.cache_clear()  # type: ignore[attr-defined]


def test_every_meta_url_follows_the_pin(pinned: str) -> None:
    from app.channels.meta_sender import GraphMetaSender
    from app.connections import meta_assets, oauth_flow, refresh, registry, verify

    meta = registry.get_provider("meta")
    assert meta is not None and meta.authorize is not None
    assert meta.authorize.endpoint == f"https://www.facebook.com/{pinned}/dialog/oauth"
    assert meta.authorize.exchange_endpoint == (
        f"https://graph.facebook.com/{pinned}/oauth/access_token"
    )
    assert meta.authorize.long_lived_endpoint == meta.authorize.exchange_endpoint
    # The connect-start path reads the same resolved spec.
    spec = oauth_flow.authorize_spec("meta")
    assert spec is not None and spec.endpoint == meta.authorize.endpoint
    # The option lists the UI renders resolve too.
    assert registry.providers_for("channel_auth")[0].authorize == meta.authorize

    assert registry.resolve_endpoint(refresh.REFRESH_SPECS["meta"].endpoint) == (
        f"https://graph.facebook.com/{pinned}/oauth/access_token"
    )
    assert meta_assets.graph_base() == f"https://graph.facebook.com/{pinned}"
    assert meta_assets.instagram_graph_base() == f"https://{INSTAGRAM_GRAPH_HOST}/{pinned}"
    assert verify.meta_me_endpoint() == f"https://graph.facebook.com/{pinned}/me"
    assert verify.instagram_me_endpoint() == f"https://graph.instagram.com/{pinned}/me"
    assert GraphMetaSender("tok")._graph_base == f"https://graph.facebook.com/{pinned}"


async def test_the_refresh_call_goes_to_the_pinned_version(
    pinned: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refresher is the path that runs unattended for 60 days at a time."""
    from app.connections import refresh

    monkeypatch.setenv("LUCIEL_META_APP_ID", "meta-cid")
    monkeypatch.setenv("LUCIEL_META_APP_SECRET", "meta-csecret")
    get_settings.cache_clear()  # type: ignore[attr-defined]
    calls: list[str] = []

    class _Transport:
        async def get(self, url: str, *, params: dict[str, str] | None = None) -> object:
            calls.append(url)

            class _R:
                status_code = 200

                @staticmethod
                def json() -> dict[str, object]:
                    return {"access_token": "new", "expires_in": 60}

            return _R()

    result = await refresh._refresh_material(
        "meta",
        {"access_token": "old"},
        spec=refresh.REFRESH_SPECS["meta"],
        transport=_Transport(),  # type: ignore[arg-type]
    )
    assert result is not None
    assert calls == [f"https://graph.facebook.com/{pinned}/oauth/access_token"]
