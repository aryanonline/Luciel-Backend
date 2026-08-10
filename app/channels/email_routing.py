"""Own-domain email inbound-MX delegation probe (Architecture §3.1.6a / Decision #49).

Mirrors the SMS number-operability probe (``app/channels/number_operability.py``):
answers ONE question against live DNS — *has this domain's MX record actually been
delegated to our inbound relay?* — i.e. does ``domain``'s MX record point at
``inbound.vantagemind.ai`` (``app/connections/service.py::_INBOUND_MX``). This is the
check that makes the own-domain flow honest: choosing "own_domain" and typing an address
does NOT mean mail can reach us — the admin must add the MX record
(``app/connections/service.py::dns_records_for``) at their registrar first, and until
that propagates and is confirmed, the connection stays ``pending_email_routing`` (NOT
"connected") — a non-silent precondition, same posture as SMS carrier registration.

Same Protocol + hermetic-Null + live + factory shape as the SMS probe:
  - a ``MxDelegationProbe`` Protocol,
  - a hermetic ``NullMxProbe`` (mock/CI default — no network; deterministic via a
    caller-seeded ``delegated_domains`` set),
  - a live ``DnsMxProbe`` (lazy ``dns.resolver`` import so mock mode never touches it),
  - a ``build_mx_probe`` factory that returns Null unless ``integration_mode == "live"``.

HONEST BOUNDARY (constant on every call): a DELEGATED result confirms the MX record
resolves to our relay RIGHT NOW. It does NOT confirm the platform has finished wiring
inbound receipt for that specific address on the relay side, and it does NOT survive the
admin later changing the record back — reverify is stateless and repeatable (§3.1.4
posture), never a one-time badge.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol


class MxDelegationResult(str, Enum):
    DELEGATED = "delegated"  # MX resolves to our inbound relay
    NOT_DELEGATED = "not_delegated"  # domain resolves but MX does not point at us
    UNKNOWN = "unknown"  # DNS lookup failed/timed out/domain doesn't resolve at all


class MxDelegationProbe(Protocol):
    async def check(self, domain: str, expected_mx_host: str) -> MxDelegationResult: ...


@dataclass
class NullMxProbe:
    """Mock/CI probe — NO network. Deterministic + inspectable.

    ``delegated_domains`` is the set treated as delegated (empty by default → every
    domain is NOT_DELEGATED, the safe default for hermetic tests). Tests seed this to
    exercise the delegated path without touching real DNS. Default probe whenever the
    live DNS resolver is not configured (mirrors ``NullOperabilityProbe``)."""

    delegated_domains: set[str] = field(default_factory=set)
    calls: list[str] = field(default_factory=list)

    async def check(self, domain: str, expected_mx_host: str) -> MxDelegationResult:
        self.calls.append(domain)
        return (
            MxDelegationResult.DELEGATED
            if domain in self.delegated_domains
            else MxDelegationResult.NOT_DELEGATED
        )


@dataclass
class DnsMxProbe:
    """Live probe: does ``domain``'s MX record resolve to ``expected_mx_host``?

    Lazy-imports ``dns.resolver`` so mock/CI never needs it (mirrors
    ``TwilioOperabilityProbe``). Resolution runs in a worker thread (dnspython's
    resolver is synchronous) so it never blocks the event loop."""

    timeout_seconds: float = 5.0

    async def check(self, domain: str, expected_mx_host: str) -> MxDelegationResult:
        try:
            import dns.resolver
        except ImportError:
            return MxDelegationResult.UNKNOWN
        try:
            answers = await _to_thread(
                lambda: dns.resolver.resolve(domain, "MX", lifetime=self.timeout_seconds)
            )
            expected = expected_mx_host.rstrip(".").lower()
            hosts = {str(r.exchange).rstrip(".").lower() for r in answers}
            return (
                MxDelegationResult.DELEGATED
                if expected in hosts
                else MxDelegationResult.NOT_DELEGATED
            )
        except dns.resolver.NXDOMAIN:
            return MxDelegationResult.NOT_DELEGATED
        except dns.resolver.NoAnswer:
            # Domain resolves but has no MX record at all — not delegated, but not an
            # unknown-probe-failure either; the admin simply hasn't added the record.
            return MxDelegationResult.NOT_DELEGATED
        except Exception:
            # Timeout / SERVFAIL / no nameservers reachable — cannot determine. NEVER
            # default to delegated on error (that would reintroduce the SMS-style
            # silent-failure bug in the email flow).
            return MxDelegationResult.UNKNOWN


async def _to_thread[T](fn: Callable[[], T]) -> T:  # pragma: no cover - asyncio shim
    import asyncio

    return await asyncio.to_thread(fn)


def build_mx_probe() -> MxDelegationProbe:
    """Live DNS probe when ``integration_mode == 'live'``, else the hermetic Null probe.

    Unlike the SMS operability probe, this needs no tenant-owned credentials — MX
    lookups are public DNS, not a BYO-account API call — so the only gate is
    integration mode itself (mock/CI never does real network DNS)."""
    from app.config import get_settings

    if get_settings().integration_mode == "live":
        return DnsMxProbe()
    return NullMxProbe()
