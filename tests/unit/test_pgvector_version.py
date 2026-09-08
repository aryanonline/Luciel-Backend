"""pgvector version truth and the per-transaction HNSW search settings (round 6 WP-G, F044)."""

from __future__ import annotations

from app.knowledge.pgvector import (
    MIN_PGVECTOR,
    parse_version,
    search_settings_sql,
    supports_iterative_scan,
    too_old_reason,
)


def test_versions_parse_and_compare_against_the_pin():
    assert parse_version("0.8.6") == (0, 8, 6)
    assert parse_version("0.7") == (0, 7, 0)
    assert parse_version(" 1.0.0 ") == (1, 0, 0)
    assert parse_version("") is None
    assert parse_version("vector") is None
    assert MIN_PGVECTOR == (0, 8, 0)
    assert supports_iterative_scan((0, 8, 0)) and supports_iterative_scan((1, 2, 0))
    assert not supports_iterative_scan((0, 7, 4)) and not supports_iterative_scan(None)


def test_search_settings_widen_with_k_and_use_iterative_scans_only_when_supported():
    assert search_settings_sql(5, (0, 8, 0)) == [
        "SET LOCAL hnsw.ef_search = 40",
        "SET LOCAL hnsw.iterative_scan = 'relaxed_order'",
    ]
    assert search_settings_sql(25, (0, 7, 4)) == ["SET LOCAL hnsw.ef_search = 100"]
    assert search_settings_sql(1, None) == ["SET LOCAL hnsw.ef_search = 40"]


def test_healthz_reason_names_the_installed_version_below_the_pin():
    assert too_old_reason((0, 7, 4)) == "pgvector_too_old:0.7.4"
    assert too_old_reason((0, 8, 6)) is None
    assert too_old_reason(None) == "pgvector_too_old:unknown"
