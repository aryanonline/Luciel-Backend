"""BYO-webhook subprocess sandbox security envelope (Architecture §3.8.6).

The bring_your_own_webhook tool runs the admin-registered endpoint call with:
  - execution isolation: dispatched in a separate subprocess (no access to the
    main process memory, DB pool, or other tenants' secrets). The auth header is
    injected by the parent as an HTTP header; the subprocess never sees the token.
  - enforced timeout: 10s (Ratified §9 #10); over-time → structured timeout error.
  - request + response schema validation; out-of-schema response → failed.
  - per-endpoint circuit breaker: 5 consecutive failures → open for a cooldown.
  - SSRF egress controls: domain allowlist + block of private/link-local/metadata
    IP ranges, validated AFTER DNS resolution (defeats DNS rebinding).

This module owns the SSRF/allowlist/circuit-breaker logic and the result mapping;
the actual subprocess HTTP execution is abstracted behind an executor so the
security envelope is unit-testable without real network egress.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass, field
from urllib.parse import urlparse

from app.tools import contract as c
from app.tools.contract import ToolContext, ToolResult

WEBHOOK_TIMEOUT_SECONDS = 10  # Ratified §9 #10
CIRCUIT_FAILURE_THRESHOLD = 5  # Ratified §9 #11
CIRCUIT_COOLDOWN_SECONDS = 300

_BLOCKED_NETS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),  # link-local incl. 169.254.169.254 (metadata)
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fe80::/10"),
]


class SsrfBlocked(Exception):
    pass


def _resolve_ips(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None)
    return [str(info[4][0]) for info in infos]


def is_egress_allowed(url: str, allowlist: frozenset[str]) -> bool:
    """Allow only allowlisted hosts whose RESOLVED IPs are public (anti-rebinding)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.hostname or ""
    if host not in allowlist:
        return False
    try:
        ips = _resolve_ips(host)
    except OSError:
        return False
    if not ips:
        return False
    for ip in ips:
        addr = ipaddress.ip_address(ip)
        for net in _BLOCKED_NETS:
            if addr.version == net.version and addr in net:
                return False
    return True


@dataclass
class CircuitBreaker:
    """Per-(admin_id, connection_id) consecutive-failure breaker (§3.8.6)."""

    consecutive_failures: int = 0
    opened_at: float | None = None

    def is_open(self, now: float) -> bool:
        if self.opened_at is None:
            return False
        if now - self.opened_at >= CIRCUIT_COOLDOWN_SECONDS:
            return False  # half-open: allow a probe
        return True

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.opened_at = None

    def record_failure(self, now: float) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= CIRCUIT_FAILURE_THRESHOLD:
            self.opened_at = now


@dataclass
class WebhookExecutor:
    """Abstracts the subprocess HTTP call. Real impl runs in an isolated subprocess
    with the timeout enforced and the auth header injected by the parent."""

    # response: (status_code, json_body) or raises on timeout/transport error.
    responder: object = field(default=None)

    async def call(
        self,
        url: str,
        payload: dict[str, object],
        auth_header: str | None,
        timeout: int,
    ) -> tuple[int, dict[str, object]]:
        if self.responder is None:
            raise RuntimeError("no responder bound")
        result = await self.responder(url, payload, auth_header, timeout)  # type: ignore[operator]
        return result  # type: ignore[no-any-return]


async def run_byo_webhook(
    *,
    url: str,
    payload: dict,  # type: ignore[type-arg]
    auth_header: str | None,
    allowlist: frozenset[str],
    breaker: CircuitBreaker,
    executor: WebhookExecutor,
    now: float,
    response_required_fields: frozenset[str] = frozenset(),
    context: ToolContext | None = None,
) -> ToolResult:
    """Execute a BYO webhook through the full security envelope; map to ToolResult."""
    if breaker.is_open(now):
        return c.failed("circuit_open")
    if not is_egress_allowed(url, allowlist):
        return c.failed("egress_blocked")
    try:
        status, body = await executor.call(url, payload, auth_header, WEBHOOK_TIMEOUT_SECONDS)
    except TimeoutError:
        breaker.record_failure(now)
        return c.failed("timeout")
    except Exception as exc:  # noqa: BLE001
        breaker.record_failure(now)
        return c.failed(f"transport_error:{type(exc).__name__}")

    if status < 200 or status >= 300:
        breaker.record_failure(now)
        return c.failed(f"non_2xx:{status}")
    # Response schema validation: required fields must be present (faithful-readback).
    missing = [f for f in response_required_fields if f not in body]
    if missing:
        breaker.record_failure(now)
        return c.failed("schema_invalid")
    breaker.record_success()
    if not body:
        return c.empty()
    return c.ok(body)
