"""Outbound webhook calls connect to the address that was validated (2026-09-05 audit
F090).

``resolves_to_public_host`` validated a NAME; the connect() that followed resolved it
again. A DNS answer that flips between the two — rebinding — reached the metadata
endpoint with a clean bill of health. ``pin_public_target`` resolves once, refuses
anything non-public, and hands back a URL that connects to that exact address while
the original hostname travels as Host + SNI.
"""

from __future__ import annotations

import pytest
from app.connections import sandbox
from app.connections.sandbox import SsrfBlocked, pin_public_target


def _resolver(*ips: str):
    def resolve(host: str) -> list[str]:
        return list(ips)

    return resolve


def test_a_public_host_is_pinned_to_its_resolved_address(monkeypatch):
    monkeypatch.setattr(sandbox, "_resolve_ips", _resolver("93.184.216.34"))
    pinned, host = pin_public_target("https://hooks.example.com/crm?x=1")
    assert pinned == "https://93.184.216.34/crm?x=1"
    assert host == "hooks.example.com"


def test_an_explicit_port_and_an_ipv6_answer_survive_the_rewrite(monkeypatch):
    monkeypatch.setattr(sandbox, "_resolve_ips", _resolver("2606:4700:4700::1111"))
    pinned, host = pin_public_target("https://hooks.example.com:8443/in")
    assert pinned == "https://[2606:4700:4700::1111]:8443/in"
    assert host == "hooks.example.com"


@pytest.mark.parametrize(
    "answer",
    ["169.254.169.254", "10.1.2.3", "127.0.0.1", "192.168.1.1", "172.16.0.9", "::1"],
)
def test_a_private_or_link_local_answer_is_refused(monkeypatch, answer):
    monkeypatch.setattr(sandbox, "_resolve_ips", _resolver(answer))
    with pytest.raises(SsrfBlocked):
        pin_public_target("https://hooks.example.com/in")


def test_a_mixed_answer_is_refused_even_when_the_first_address_is_public(monkeypatch):
    """Which address a later connect() would pick is not ours to know — and the
    rebinding trick is exactly one public answer followed by one private one."""
    monkeypatch.setattr(sandbox, "_resolve_ips", _resolver("93.184.216.34", "169.254.169.254"))
    with pytest.raises(SsrfBlocked):
        pin_public_target("https://hooks.example.com/in")


def test_unresolvable_and_non_http_targets_are_refused(monkeypatch):
    def boom(host: str) -> list[str]:
        raise OSError("nxdomain")

    monkeypatch.setattr(sandbox, "_resolve_ips", boom)
    with pytest.raises(SsrfBlocked):
        pin_public_target("https://nowhere.example/in")
    with pytest.raises(SsrfBlocked):
        pin_public_target("ftp://hooks.example.com/in")
