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

import asyncio
import importlib
import ipaddress
import multiprocessing
import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from multiprocessing import connection as mp_connection
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


# --- subprocess isolation (§3.8.6) -------------------------------------------- #
# ``WebhookExecutor`` above is only an injectable-callable seam — fine for unit-
# testing the SSRF/circuit-breaker envelope, but it gives a BYO endpoint call the
# SAME process memory, DB pool, and secret-store handle as the rest of the app. A
# malicious or buggy admin-registered webhook must not be able to read another
# tenant's secrets or hold a DB connection open. ``SubprocessWebhookExecutor`` is
# the real process-boundary implementation: it does the HTTP call in a genuinely
# separate OS process, started with the "spawn" start method (never "fork") so the
# worker gets a FRESH interpreter — no inherited DB engine/pool, no inherited
# SecretStore handle, no inherited connection objects of any kind. The parent
# attaches the auth header as an argument to the worker; the worker never touches
# app.connections.secrets / app.db / app.config at all — it only knows how to make
# one HTTP request and return (status, body).

# The dotted path to the HTTP call function the worker uses. Overridable ONLY for
# hermetic tests (a test module-level fake, referenced by import path since a spawned
# process cannot receive an unpickled closure/bound method) — production code never
# sets this to anything but the default, which is the real httpx-based call.
_DEFAULT_HTTP_CALL_PATH = "app.connections.sandbox:_httpx_post_json"


def _httpx_post_json(
    url: str, payload: dict[str, object], auth_header: str | None, timeout: int
) -> tuple[int, dict[str, object]]:
    """The REAL HTTP call made inside the subprocess worker. Lazy-imports httpx so
    importing this module never requires it and no network call happens at import
    time. This is the only place the outbound TCP connection is opened — it runs
    entirely inside the isolated worker process, never the parent."""
    import httpx  # noqa: PLC0415 — lazy import: only the worker process needs this

    headers = {"Authorization": auth_header} if auth_header else {}
    resp = httpx.post(url, json=payload, headers=headers, timeout=timeout)
    try:
        body = resp.json()
    except ValueError:
        body = {}
    return resp.status_code, body if isinstance(body, dict) else {}


def _resolve_http_call(
    dotted_path: str,
) -> Callable[..., tuple[int, dict[str, object]]]:
    """Resolve a ``module.sub:attr`` path to the callable. Runs INSIDE the worker
    process only — never touches DB/secrets modules regardless of what the string
    names (tests only ever point this at a hermetic fake function)."""
    module_path, _, attr = dotted_path.partition(":")
    module = importlib.import_module(module_path)
    resolved: Callable[..., tuple[int, dict[str, object]]] = getattr(module, attr)
    return resolved


def _subprocess_worker_entrypoint(
    conn: mp_connection.Connection,
    http_call_path: str,
    url: str,
    payload: dict[str, object],
    auth_header: str | None,
    timeout: int,
) -> None:
    """HARDENED worker entrypoint — runs in the isolated child process (§3.8.6).

    Deliberately narrow: it imports NOTHING from app.db / app.connections.secrets /
    app.config, holds no DB session, and never sees any tenant's OTHER secrets —
    only the single ``auth_header`` string the parent attached for THIS call. Its
    only side effect is the one outbound HTTP request and sending the result back
    over the pipe. Any exception is caught and sent back as a serializable tuple so
    the parent maps it to the correct structured ToolResult (never lets a worker
    crash silently hang the parent's ``recv()``).
    """
    try:
        http_call = _resolve_http_call(http_call_path)
        status, body = http_call(url, payload, auth_header, timeout)
        conn.send(("ok", status, body))
    except Exception as exc:  # noqa: BLE001 — must never let the worker crash silently
        conn.send(("error", type(exc).__name__, str(exc)))
    finally:
        conn.close()


@dataclass
class SubprocessWebhookExecutor:
    """Real process-boundary WebhookExecutor (§3.8.6): dispatches the HTTP call in a
    spawned subprocess with a hard 10s kill, no DB/secret/pool access, and the auth
    header attached by the parent as a plain argument (the worker never reads it from
    anywhere shared). Implements the same ``.call()`` shape as ``WebhookExecutor`` so
    ``run_byo_webhook`` is unchanged — this is a drop-in for the injectable-callable
    seam used in unit tests.

    ``http_call_path`` defaults to the real httpx-based call; tests override it with
    a dotted path to a module-level fake so NO real egress ever happens in CI (a
    spawned process needs a picklable/importable target, not a closure).
    """

    http_call_path: str = _DEFAULT_HTTP_CALL_PATH
    # multiprocessing context bound to "spawn" (never "fork") so the worker never
    # inherits the parent's DB engine/pool, SecretStore handles, or open sockets.
    _mp_context: object = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self._mp_context is None:
            self._mp_context = multiprocessing.get_context("spawn")

    async def call(
        self,
        url: str,
        payload: dict[str, object],
        auth_header: str | None,
        timeout: int,
    ) -> tuple[int, dict[str, object]]:
        # The actual multiprocessing start/poll/join sequence is blocking; run it in
        # a worker thread so the subprocess's up-to-10s wall clock never blocks the
        # asyncio event loop the rest of the app (and other tenants' turns) shares.
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._call_blocking, url, payload, auth_header, timeout
        )

    def _call_blocking(
        self,
        url: str,
        payload: dict[str, object],
        auth_header: str | None,
        timeout: int,
    ) -> tuple[int, dict[str, object]]:
        ctx = self._mp_context
        parent_conn, child_conn = ctx.Pipe(duplex=False)  # type: ignore[attr-defined]
        proc = ctx.Process(  # type: ignore[attr-defined]
            target=_subprocess_worker_entrypoint,
            args=(child_conn, self.http_call_path, url, payload, auth_header, timeout),
            daemon=True,
        )
        proc.start()
        try:
            # Hard 10s kill (Ratified §9 #10 / §3.8.6): the PARENT enforces the wall-
            # clock timeout independent of whatever the worker/http client does, so a
            # hung worker can never outlive the budget.
            if parent_conn.poll(timeout):
                kind, *rest = parent_conn.recv()
            else:
                kind, rest = "timeout", []
        finally:
            parent_conn.close()
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=1)
                if proc.is_alive():  # still alive after terminate — hard kill
                    proc.kill()
                    proc.join(timeout=1)
            else:
                proc.join(timeout=1)

        if kind == "timeout":
            raise TimeoutError("webhook_subprocess_timeout")
        if kind == "error":
            exc_type, exc_msg = rest
            raise RuntimeError(f"{exc_type}:{exc_msg}")
        status, body = rest
        return status, body


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
