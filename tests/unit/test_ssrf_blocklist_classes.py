"""The SSRF blocklist covers every non-public address class (round 7 WP-4c, item 4).

The hand-written network list behind ``pin_public_target`` / ``resolves_to_public_host``
omitted 0.0.0.0/8, fc00::/7 (IPv6 ULA), 100.64.0.0/10 (carrier-grade NAT) and
198.18.0.0/15 (benchmarking): a crawl or webhook target whose DNS answer landed in one
of those reached the socket with a clean bill of health. The check now composes the
``ipaddress`` predicates (private / reserved / loopback / link-local / unspecified /
multicast / not-global) with the explicit CGNAT, benchmarking and NAT64 ranges, and
unwraps an IPv4 address embedded in an IPv6 answer so the v4 classes apply to what a
connect() would actually reach.
"""

from __future__ import annotations

import pytest
from app.connections import sandbox
from app.connections.sandbox import SsrfBlocked, pin_public_target, resolves_to_public_host


def _resolver(*ips: str):
    def resolve(host: str) -> list[str]:
        return list(ips)

    return resolve


@pytest.mark.parametrize(
    ("answer", "address_class"),
    [
        ("0.0.0.0", "this-network / unspecified (0.0.0.0/8)"),
        ("0.1.2.3", "this-network (0.0.0.0/8)"),
        ("100.64.1.1", "carrier-grade NAT (100.64.0.0/10)"),
        ("100.127.255.254", "carrier-grade NAT upper edge"),
        ("198.18.0.1", "benchmarking (198.18.0.0/15)"),
        ("198.19.255.1", "benchmarking upper half"),
        ("10.1.2.3", "RFC 1918 10/8"),
        ("172.16.0.9", "RFC 1918 172.16/12"),
        ("192.168.1.1", "RFC 1918 192.168/16"),
        ("127.0.0.1", "loopback"),
        ("127.255.255.254", "loopback upper edge"),
        ("169.254.169.254", "link-local (task metadata endpoint)"),
        ("169.254.170.2", "link-local (ECS credentials endpoint)"),
        ("224.0.0.1", "IPv4 multicast"),
        ("240.0.0.1", "reserved (240/4)"),
        ("255.255.255.255", "broadcast"),
        ("192.0.0.170", "IETF protocol assignments"),
        ("192.0.2.1", "documentation TEST-NET-1"),
        ("198.51.100.1", "documentation TEST-NET-2"),
        ("203.0.113.9", "documentation TEST-NET-3"),
        ("::", "IPv6 unspecified"),
        ("::1", "IPv6 loopback"),
        ("fc00::1", "IPv6 ULA (fc00::/7, low half)"),
        ("fd12:3456::1", "IPv6 ULA (fc00::/7, high half)"),
        ("fe80::1", "IPv6 link-local"),
        ("ff02::1", "IPv6 multicast"),
        ("2001:db8::1", "IPv6 documentation"),
        ("::ffff:127.0.0.1", "IPv4-mapped loopback"),
        ("::ffff:169.254.169.254", "IPv4-mapped link-local (metadata)"),
        ("::ffff:10.0.0.5", "IPv4-mapped RFC 1918"),
        ("::ffff:100.64.0.7", "IPv4-mapped CGNAT"),
        ("64:ff9b::a9fe:a9fe", "NAT64 prefix embedding 169.254.169.254"),
        ("64:ff9b::7f00:1", "NAT64 prefix embedding 127.0.0.1"),
        ("2002:a9fe:a9fe::1", "6to4 embedding 169.254.169.254"),
    ],
)
def test_every_non_public_class_is_refused(monkeypatch, answer: str, address_class: str):
    monkeypatch.setattr(sandbox, "_resolve_ips", _resolver(answer))
    assert sandbox._is_blocked_address(answer) is True, address_class
    with pytest.raises(SsrfBlocked):
        pin_public_target("https://site.example/page")
    assert resolves_to_public_host("https://site.example/page") is False


@pytest.mark.parametrize(
    "answer",
    ["93.184.216.34", "8.8.8.8", "1.1.1.1", "2606:4700:4700::1111", "2600:1f18::1"],
)
def test_a_genuinely_global_answer_still_passes(monkeypatch, answer: str):
    """The tightened classes must not turn into a blanket refusal: routable public
    addresses on both IP versions still pin."""
    monkeypatch.setattr(sandbox, "_resolve_ips", _resolver(answer))
    assert sandbox._is_blocked_address(answer) is False
    pinned, host = pin_public_target("https://site.example/page")
    assert host == "site.example"
    assert pinned.startswith("https://")
    assert resolves_to_public_host("https://site.example/page") is True


def test_a_public_answer_beside_a_cgnat_answer_is_refused(monkeypatch):
    """Mixed answers are refused as before — now also when the second address is in a
    range the old list did not know about."""
    monkeypatch.setattr(sandbox, "_resolve_ips", _resolver("93.184.216.34", "100.64.9.9"))
    with pytest.raises(SsrfBlocked):
        pin_public_target("https://site.example/page")
