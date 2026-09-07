"""Round 6 WP-I (F066): website origins are normalised one way everywhere."""

from __future__ import annotations

import pytest
from app.channels.origins import normalize_origin, request_origin


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://acme.example", "https://acme.example"),
        ("https://Acme.Example/", "https://acme.example"),
        ("HTTPS://acme.example:443", "https://acme.example"),
        ("http://acme.example:80", "http://acme.example"),
        ("http://localhost:3000", "http://localhost:3000"),
        ("https://acme.example:8443/", "https://acme.example:8443"),
        ("  https://acme.example  ", "https://acme.example"),
    ],
)
def test_origins_normalise_to_scheme_host_port(raw, expected):
    assert normalize_origin(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "acme.example",
        "ftp://acme.example",
        "https://acme.example/pricing",
        "https://acme.example?x=1",
        "https://acme.example#top",
        "https://user:pw@acme.example",
        "https://acme .example",
        "https://acme.example:notaport",
        "null",
    ],
)
def test_anything_but_an_origin_is_refused(raw):
    assert normalize_origin(raw) is None


def test_the_request_origin_prefers_origin_and_falls_back_to_the_referers_origin():
    assert request_origin({"origin": "https://acme.example"}) == "https://acme.example"
    assert (
        request_origin({"referer": "https://Acme.example/pricing?utm=1"}) == "https://acme.example"
    )
    assert (
        request_origin({"origin": "https://a.example", "referer": "https://b.example/x"})
        == "https://a.example"
    )
    assert request_origin({"origin": "null", "referer": "https://b.example/x"}) is None
    assert request_origin({}) is None
    assert request_origin({"referer": "not a url"}) is None
