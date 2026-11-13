"""Own-domain email MX-delegation probe (Architecture §3.1.6a, harmony fix 2026-08-04
item 5). Pure-function/no-DB coverage of the probe contract itself, mirroring
tests/integration/test_number_operability.py's SMS probe coverage.
"""

from __future__ import annotations

from app.channels.email_routing import (
    DnsMxProbe,
    MxDelegationResult,
    NullMxProbe,
    build_mx_probe,
)

_EXPECTED_MX = "inbound.vantagemind.ai"


async def test_null_probe_defaults_to_not_delegated():
    """The safe hermetic default: an unseeded domain is NOT delegated, never a
    fabricated success."""
    probe = NullMxProbe()
    result = await probe.check("acme.example", _EXPECTED_MX)
    assert result is MxDelegationResult.NOT_DELEGATED
    assert probe.calls == ["acme.example"]


async def test_null_probe_reports_seeded_domains_as_delegated():
    probe = NullMxProbe(delegated_domains={"acme.example"})
    assert await probe.check("acme.example", _EXPECTED_MX) is MxDelegationResult.DELEGATED
    assert await probe.check("other.example", _EXPECTED_MX) is MxDelegationResult.NOT_DELEGATED


async def test_build_mx_probe_is_null_outside_live_mode(monkeypatch):
    """Mock/CI must never touch real DNS (mirrors build_operability_probe's honest
    degradation)."""
    from app import config

    config.get_settings.cache_clear()
    monkeypatch.setenv("LUCIEL_INTEGRATION_MODE", "mock")
    config.get_settings.cache_clear()
    probe = build_mx_probe()
    assert isinstance(probe, NullMxProbe)
    config.get_settings.cache_clear()


async def test_dns_probe_missing_dnspython_reports_unknown(monkeypatch):
    """If dnspython somehow isn't importable, the probe must fail closed to UNKNOWN,
    never crash and never claim delegation."""
    import builtins

    real_import = builtins.__import__

    def _blocked(name, *args, **kwargs):
        if name == "dns.resolver" or name.startswith("dns."):
            raise ImportError("blocked for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked)
    probe = DnsMxProbe()
    result = await probe.check("acme.example", _EXPECTED_MX)
    assert result is MxDelegationResult.UNKNOWN


async def test_dns_probe_never_defaults_to_delegated_on_error(monkeypatch):
    """A resolver that raises an unexpected error must stay UNKNOWN, not silently
    report DELEGATED (that would reintroduce the SMS-style silent-failure bug)."""
    import dns.resolver

    def _boom(*args, **kwargs):
        raise TimeoutError("simulated DNS timeout")

    monkeypatch.setattr(dns.resolver, "resolve", _boom)
    probe = DnsMxProbe()
    result = await probe.check("acme.example", _EXPECTED_MX)
    assert result is MxDelegationResult.UNKNOWN


async def test_dns_probe_matches_expected_mx_host_case_and_trailing_dot_insensitively(
    monkeypatch,
):
    """Real MX answers come back as dnspython Name objects with a trailing dot and
    arbitrary case; the comparison must normalize both sides."""
    import dns.resolver

    class _FakeRdata:
        def __init__(self, host: str) -> None:
            self.exchange = host

    def _fake_resolve(domain, rdtype, lifetime=None):
        assert rdtype == "MX"
        return [_FakeRdata("INBOUND.VantageMind.ai.")]

    monkeypatch.setattr(dns.resolver, "resolve", _fake_resolve)
    probe = DnsMxProbe()
    result = await probe.check("acme.example", _EXPECTED_MX)
    assert result is MxDelegationResult.DELEGATED


async def test_dns_probe_not_delegated_when_mx_points_elsewhere(monkeypatch):
    import dns.resolver

    class _FakeRdata:
        def __init__(self, host: str) -> None:
            self.exchange = host

    def _fake_resolve(domain, rdtype, lifetime=None):
        return [_FakeRdata("mail.some-other-provider.example.")]

    monkeypatch.setattr(dns.resolver, "resolve", _fake_resolve)
    probe = DnsMxProbe()
    result = await probe.check("acme.example", _EXPECTED_MX)
    assert result is MxDelegationResult.NOT_DELEGATED


async def test_dns_probe_nxdomain_is_not_delegated_not_unknown(monkeypatch):
    """A domain that plain doesn't exist is a definite 'not set up yet', not an
    ambiguous probe failure."""
    import dns.resolver

    def _fake_resolve(domain, rdtype, lifetime=None):
        raise dns.resolver.NXDOMAIN()

    monkeypatch.setattr(dns.resolver, "resolve", _fake_resolve)
    probe = DnsMxProbe()
    result = await probe.check("nonexistent.example", _EXPECTED_MX)
    assert result is MxDelegationResult.NOT_DELEGATED
