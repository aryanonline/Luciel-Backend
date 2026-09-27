"""The graph extractor posture follows integration mode (owner decision D10, round 7
WP-4c #10).

Round 6 built the LLM extractor behind ``LUCIEL_GRAPH_EXTRACTOR=llm`` and nothing set
the variable anywhere, so it was inert in every deployment. The setting now resolves
by posture when unset — ``llm`` wherever real providers are bound, ``deterministic`` in
mock/CI — and an explicit value still wins (the operator's off switch in live).
"""

from __future__ import annotations

import pytest
from app.config import Settings, get_settings
from app.knowledge import graph_extract as ge

from .live_settings import live_settings


def test_live_posture_defaults_to_the_llm_extractor():
    assert live_settings().graph_extractor == "llm"


def test_mock_posture_stays_deterministic():
    assert Settings(platform_secret="{}").graph_extractor == "deterministic"


def test_an_explicit_value_wins_in_either_posture():
    assert live_settings(**{}).graph_extractor == "llm"
    assert (
        Settings(
            integration_mode="live",
            platform_secret=live_settings().platform_secret,
            graph_extractor="deterministic",
        ).graph_extractor
        == "deterministic"
    )
    assert Settings(platform_secret="{}", graph_extractor="llm").graph_extractor == "llm"


def test_the_seam_binds_the_llm_extractor_in_live_with_nothing_set(
    monkeypatch: pytest.MonkeyPatch,
):
    """The live deployment shape: no LUCIEL_GRAPH_EXTRACTOR in the environment."""
    monkeypatch.delenv("LUCIEL_GRAPH_EXTRACTOR", raising=False)
    ge.reset_extractor()
    try:
        assert isinstance(ge.get_extractor(), ge.DeterministicGraphExtractor)  # mock/CI
        ge.reset_extractor()
        monkeypatch.setattr("app.config.live_mode", lambda: True)
        monkeypatch.setattr("app.config.get_settings", lambda: live_settings())
        assert isinstance(ge.get_extractor(), ge.LlmGraphExtractor)
    finally:
        ge.reset_extractor()
        get_settings.cache_clear()  # type: ignore[attr-defined]
