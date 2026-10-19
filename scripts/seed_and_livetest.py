#!/usr/bin/env python3
"""§10 live-behavior SEED + TEST harness (dev only, GATE 2 LIVE).

Seeds a synthetic admin + Luciel through the REAL API path, then drives live
conversations to convert several §10 rows from CI-proven to LIVE-proven:

  T1  Live LLM happy path        — real grounded answer (not EchoModel).
  T2  Row 9  free-cap = 0 LLM    — at the 50-conversation cap the 51st makes
                                    ZERO LLM calls (money-critical / denial-of-wallet).
  T3  Row 1b 50/min LLM cap      — >50 calls/min for one admin are rejected, and the
                                    edge surfaces a graceful 429 (conversation not dropped).
  T4  Row 5  fail-safe escalate  — a forced tool failure → honest cannot_answer
                                    escalation, never a fabricated success.
  T5  Row 8  primary→fallback    — forced primary failure → live OpenAI fallback answers
                                    end-to-end (max_completion_tokens fix exercised live).
  T6  Row 11 channel parity      — same query across two channels → parity + the
                                    AI-identity disclosure is present.
  T7  CRM custom webhook (opt-in) — a captured lead reaches an owner-provided endpoint.

Each test prints PASS/FAIL with evidence; the process exits non-zero if ANY fails.

STRICT (``--strict``, or LUCIEL_SEEDTEST_STRICT=1 — the SeedTest task definition sets
it, and scripts/run-seedtest-task.sh passes ``--strict`` unless told ``--no-strict``):
a ``[SKIP]`` is otherwise non-fatal and invisible in the exit code, so a case that
silently stopped running (a missing key, a broken precondition) still read "all
PASS". Under strict, every skip whose (case, reason) is not in ``OWNER_GATED_SKIPS``
— the cases the OWNER gates behind LUCIEL_SEEDTEST_LIVE_CAP / LUCIEL_SEEDTEST_WEBHOOK_URL
because they spend money or need an owner asset — fails the run with the list.

Runs as a one-off in-VPC ECS task (SeedTestTask) so it can both reach the private
API/RDS and read the verify JTI from the DB. Synthetic data only; no secrets are
hardcoded (LLM keys come from the platform secret already in the live dev env).
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import sys
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass


# --------------------------------------------------------------------------- #
# Self-contained env bootstrap. Derive LUCIEL_DATABASE_URL(_SYNC) for the
# non-superuser luciel_app role from the injected RDS secret BEFORE importing any
# app module (app.config / app.db read the env at first use). Mirrors
# scripts/app-entrypoint.sh exactly so RLS binds identically to the running app.
# --------------------------------------------------------------------------- #
def _bootstrap_db_env() -> None:
    raw = os.environ.get("LUCIEL_DB_SECRET")
    if not raw or os.environ.get("LUCIEL_DATABASE_URL"):
        return  # locally LUCIEL_DATABASE_URL may already be set; nothing to derive.
    import hashlib

    s = json.loads(raw)
    host = s["host"]
    port = s.get("port", 5432)
    pw = s["password"]
    db = s.get("dbname") or "luciel"
    app_pw = hashlib.sha256(("luciel_app:" + pw).encode()).hexdigest()[:32]
    base = f"luciel_app:{app_pw}@{host}:{port}/{db}"
    os.environ["LUCIEL_DATABASE_URL"] = f"postgresql+asyncpg://{base}"
    os.environ["LUCIEL_DATABASE_URL_SYNC"] = f"postgresql+psycopg://{base}"


_bootstrap_db_env()

import httpx  # noqa: E402

API_BASE = os.environ.get("LUCIEL_API_BASE", "https://api.vantagemind.ai").rstrip("/")
RUN_ID = os.environ.get("LUCIEL_SEEDTEST_RUN_ID", str(int(time.time())))
# Synthetic but syntactically-valid domain (the signup validator rejects reserved
# TLDs like .invalid). seed.vantagemind.ai is ours and routes nowhere real.
EMAIL_DOMAIN = "seed.vantagemind.ai"
PASSWORD = "Seed-" + uuid.uuid4().hex  # synthetic, per-run, never reused.
KNOWN_FACT = "Our return window is 30 days from the date of delivery."
GROUNDED_QUESTION = "How many days do I have to return an item?"
GROUNDED_TOKEN = "30"  # noqa: S105 — not a secret; the fact's load-bearing number.

HTTP_TIMEOUT = 90.0  # live LLM round-trips can be slow.


def _email(role: str) -> str:
    return f"seed+{RUN_ID}-{role}@{EMAIL_DOMAIN}"


# --------------------------------------------------------------------------- #
# Result accounting                                                            #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Row:
    """One recorded check. ``status`` is ``pass`` | ``fail`` | ``skip``; ``kind`` tells a
    T-case from a matrix combination; ``evidence`` is the skip REASON for a skip."""

    name: str
    status: str
    evidence: str
    kind: str = "case"
    seconds: float | None = None


class Results:
    def __init__(self) -> None:
        self.rows: list[Row] = []

    def record(
        self,
        name: str,
        ok: bool,
        evidence: str,
        *,
        kind: str = "case",
        seconds: float | None = None,
    ) -> None:
        self.rows.append(Row(name, "pass" if ok else "fail", evidence, kind, seconds))
        status = "PASS" if ok else "FAIL"
        timing = f" ({seconds:.1f}s)" if seconds is not None else ""
        print(f"[{status}] {name}{timing}: {evidence}", flush=True)

    def skip(self, name: str, reason: str) -> None:
        # A skip is non-fatal but visible. Used only when a precondition (e.g. live
        # keys) is genuinely unavailable; --strict fails the run on any skip that is
        # not owner-gated (OWNER_GATED_SKIPS).
        self.rows.append(Row(name, "skip", reason))
        print(f"[SKIP] {name}: {reason}", flush=True)

    def any_failed(self) -> bool:
        return any(row.status == "fail" for row in self.rows)

    def summary(self) -> str:
        # A skip counts as "passed" here, exactly as before --strict existed; the strict
        # verdict and the one-line SUMMARY say what was skipped.
        passed = sum(1 for row in self.rows if row.status != "fail")
        return f"{passed}/{len(self.rows)} checks passed"


#: The ONLY skips ``--strict`` tolerates, as (case-name prefix, reason prefix): cases the
#: OWNER deliberately gates behind a flag because they spend real money (50 model calls)
#: or need an owner asset (a real CRM endpoint). Any other skip — a missing key, a
#: precondition that quietly stopped holding — is a case that did not run, and strict
#: fails the run on it instead of letting it read as PASS.
OWNER_GATED_SKIPS: tuple[tuple[str, str], ...] = (
    ("T3b ", "set LUCIEL_SEEDTEST_LIVE_CAP=1"),
    ("T7 ", "set LUCIEL_SEEDTEST_WEBHOOK_URL"),
)


def is_owner_gated_skip(name: str, reason: str) -> bool:
    return any(
        name.startswith(case) and reason.startswith(gate) for case, gate in OWNER_GATED_SKIPS
    )


def strict_violations(rows: Sequence[Row]) -> list[Row]:
    """Skipped cases outside the owner-gated allowlist (what ``--strict`` fails on)."""
    return [
        row
        for row in rows
        if row.status == "skip" and not is_owner_gated_skip(row.name, row.evidence)
    ]


def exit_code(rows: Sequence[Row], *, strict: bool) -> int:
    if any(row.status == "fail" for row in rows):
        return 1
    if strict and strict_violations(rows):
        return 1
    return 0


# --------------------------------------------------------------------------- #
# HTTP helpers (real API path)                                                 #
# --------------------------------------------------------------------------- #
def _check(resp: httpx.Response, ctx: str) -> dict:
    if resp.status_code >= 400:
        raise RuntimeError(f"{ctx}: HTTP {resp.status_code} — {resp.text[:400]}")
    return resp.json() if resp.content else {}


def signup(client: httpx.Client, email: str) -> None:
    body = {"email": email, "password": PASSWORD, "captchaToken": "dev-seedtest-token"}
    _check(client.post(f"{API_BASE}/api/v1/auth/signup", json=body), "signup")


async def seed_counted_conversations(email: str, n: int) -> int:
    """Count ``n`` synthetic conversations for the tenant through the REAL fire-once
    counter (``budget.count_session_once``), not by editing the ledger row.

    T2 used to burn the free allowance by firing 50 unthreaded widget messages —
    which is exactly the "new session per message from one address" pattern the
    widget edge now rate-limits (2026-09-05 audit, new-session-per-source cap:
    burst 5, one every 15 s), so the honest harness path is to count the first
    ``n`` conversations here and drive the boundary turns over the wire. Returns
    the counter reading afterwards."""
    from app.auth.lookup import lookup_account_by_email
    from app.db.engine import dispose_engine
    from app.db.session import tenant_session, untenanted_session
    from app.models.instance import Instance
    from app.runtime import budget
    from sqlalchemy import select

    try:
        async with untenanted_session() as s:
            found = await lookup_account_by_email(s, email)
        if found is None:
            raise RuntimeError(f"account not found: {email}")
        async with tenant_session(found.account_id) as s:
            iid = (
                await s.execute(select(Instance.id).where(Instance.admin_id == found.account_id))
            ).scalar_one()
            for _ in range(n):
                await budget.count_session_once(
                    s, admin_id=found.account_id, instance_id=iid, session_id=uuid.uuid4()
                )
            state = await budget.check_budget(s, admin_id=found.account_id, instance_id=iid)
            return state.counted
    finally:
        await dispose_engine()


async def mint_verify_token(email: str) -> str:
    """Read the pending verify JTI from the DB and mint the matching verify token.

    The API discards the verify token on signup (it is emailed in prod). On dev we
    re-mint it in-process from (admin_id, jti) using the SAME signing key the app
    uses, then POST it to the real /verify-email endpoint — the live verification
    code path, not a DB shortcut."""
    from app.auth.lookup import lookup_account_by_email
    from app.auth.tokens import mint_link_token
    from app.config import get_settings
    from app.db.engine import dispose_engine
    from app.db.session import tenant_session, untenanted_session
    from app.models.instance import Account
    from sqlalchemy import select

    # Dispose the engine before returning (see _admin_id_for) so it is not reused
    # across a later asyncio.run() loop.
    try:
        async with untenanted_session() as s:
            found = await lookup_account_by_email(s, email)
        if found is None:
            raise RuntimeError(f"account not found after signup: {email}")
        async with tenant_session(found.account_id) as s:
            acct = (
                await s.execute(select(Account).where(Account.id == found.account_id))
            ).scalar_one()
            jti = acct.pending_verify_jti
        if not jti:
            raise RuntimeError("no pending_verify_jti on freshly signed-up account")
        ttl = get_settings().verification_link_ttl_seconds
        return mint_link_token(found.account_id, "verify", ttl, jti)
    finally:
        await dispose_engine()


def verify_email(client: httpx.Client, token: str) -> None:
    _check(client.post(f"{API_BASE}/api/v1/auth/verify-email", json={"token": token}), "verify")


def login(client: httpx.Client, email: str) -> None:
    _check(
        client.post(f"{API_BASE}/api/v1/auth/login", json={"email": email, "password": PASSWORD}),
        "login",
    )
    if client.cookies.get("luciel_session") is None:
        raise RuntimeError("login did not set the session cookie")


def create_luciel(client: httpx.Client, name: str) -> str:
    body = {
        "name": name,
        "websiteUrl": "https://example.com",
        "businessOneLiner": "We sell refurbished gadgets with a friendly return policy.",
    }
    data = _check(client.post(f"{API_BASE}/api/v1/admin/luciel", json=body), "create_luciel")
    embed_key = data.get("embedKeyPublicId")
    if not embed_key:
        raise RuntimeError(f"create_luciel returned no embedKeyPublicId: {data}")
    return embed_key


def paste_knowledge(client: httpx.Client, text: str) -> None:
    body = {"name": "Return policy", "text": text}
    _check(
        client.post(f"{API_BASE}/api/v1/admin/knowledge/sources/paste", json=body),
        "paste_knowledge",
    )


def widget_bootstrap(client: httpx.Client, embed_key: str) -> dict:
    return _check(
        client.post(f"{API_BASE}/api/v1/chat-widget/bootstrap", json={"embedKey": embed_key}),
        "widget_bootstrap",
    )


def widget_send(client: httpx.Client, embed_key: str, text: str) -> dict:
    """POST one widget message, honoring the §3.1.5 admission contract.

    The widget rate limit answers a GRACEFUL 429 with ``retryAfterSeconds`` —
    the shipped widget waits and retries, and so does this harness. T2's
    cap-burning loop fires messages far faster than the per-source refill
    (burst 5, ~3s), so without this the harness read the product's own
    admission control as a failure (live-caught on the 2026-08-23 run)."""
    return widget_turn(client, embed_key, text)


def widget_turn(
    client: httpx.Client, embed_key: str, text: str, *, session_id: str | None = None
) -> dict:
    """One widget message; ``session_id`` continues THAT conversation (the shipped widget
    sends the id it holds), omitted opens a new one. Paced on 429 like ``widget_send``."""
    body: dict[str, str] = {"embedKey": embed_key, "text": text}
    if session_id:
        body["sessionId"] = str(session_id)
    resp = _paced(
        lambda: client.post(f"{API_BASE}/api/v1/chat-widget/messages", json=body), "widget_send"
    )
    return _check(resp, "widget_send")


def _retry_after(resp: httpx.Response) -> float:
    try:
        return float(resp.json().get("retryAfterSeconds") or 3.0)
    except Exception:  # noqa: BLE001 — a malformed body still means "wait"
        return 3.0


def _paced(send: Callable[[], httpx.Response], ctx: str) -> httpx.Response:
    """Send, honoring the edge's graceful 429 (``retryAfterSeconds``) a bounded number of
    times — the product's own admission control, never a harness failure."""
    for _ in range(40):
        resp = send()
        if resp.status_code != 429:
            return resp
        time.sleep(min(_retry_after(resp), 10.0) + 0.2)
    raise RuntimeError(f"{ctx}: still rate-limited after bounded retries")


# --------------------------------------------------------------------------- #
# Event-loop hygiene                                                           #
# --------------------------------------------------------------------------- #
# Every in-process block runs under its own asyncio.run(), which CLOSES its loop on the
# way out. An async client opened inside one — the Anthropic/OpenAI SDK clients a live
# router builds lazily, the app's DB engine — and left open stays bound to that dead
# loop. The SDK's finalizer runs whenever the garbage collector reaches the client
# (typically during the NEXT asyncio.run()) and schedules ``aclose()`` on whatever loop
# is running then; closing the dead loop's transports raises "RuntimeError: Event loop is
# closed" ("Task exception was never retrieved" — three of them in the 2026-10-19 dev run,
# after T5 and T6). So every client is closed inside the loop that opened it.
class LoopClients:
    """The async clients one event loop opened, closed in THAT loop, last tracked first."""

    def __init__(self) -> None:
        self._held: list[object] = []

    def track[H](self, holder: H) -> H:
        """Remember a client — or a provider/router holding one — opened in this loop."""
        self._held.append(holder)
        return holder

    async def aclose(self) -> list[str]:
        """Close every tracked client once. Returns what could not be closed: one failing
        close never leaves the others open."""
        errors: list[str] = []
        seen: set[int] = set()
        while self._held:
            for client in _async_clients_of(self._held.pop()):
                if id(client) in seen:
                    continue
                seen.add(id(client))
                try:
                    await _close_client(client)
                except Exception as exc:  # noqa: BLE001 — reported, never masks the rest
                    errors.append(f"{type(client).__name__}: {type(exc).__name__}: {exc}")
        return errors


def _async_clients_of(holder: object) -> list[object]:
    """The closeable clients behind ``holder``: a client itself (it has close/aclose); a
    provider's lazily-built SDK client (``_client``, None until first used); or every leg
    of an LLMRouter (``_primary`` / ``_fallback`` / ``_fast``)."""
    if holder is None:
        return []
    if callable(getattr(holder, "aclose", None)) or callable(getattr(holder, "close", None)):
        return [holder]
    legs = [getattr(holder, leg, None) for leg in ("_primary", "_fallback", "_fast")]
    if any(leg is not None for leg in legs):
        return [client for leg in legs for client in _async_clients_of(leg)]
    return _async_clients_of(getattr(holder, "_client", None))


async def _close_client(client: object) -> None:
    is_closed = getattr(client, "is_closed", None)
    if (is_closed() if callable(is_closed) else is_closed) is True:
        return
    closer = getattr(client, "aclose", None) or client.close  # type: ignore[attr-defined]
    result = closer()
    if inspect.isawaitable(result):
        await result


async def close_llm_clients(*holders: object) -> None:
    """Close the SDK clients the given routers/providers opened — call it inside the loop
    that used them, before that loop ends."""
    scope = LoopClients()
    for holder in holders:
        scope.track(holder)
    for error in await scope.aclose():
        print(f"::warning::seedtest could not close {error}", flush=True)


async def _settle_background_work(timeout: float = 60.0) -> None:
    """Let the off-turn work an in-process turn started — the owner notification its
    escalation row schedules after the commit — finish INSIDE this loop, bounded.
    asyncio.run() would otherwise cancel it mid-flight when the loop closes."""
    from app.notifications import escalation_dispatch

    try:
        await asyncio.wait_for(escalation_dispatch.drain_background_work(), timeout)
    except TimeoutError:
        print(
            f"::warning::seedtest: off-turn escalation work still running after {timeout:.0f}s "
            "- cancelled inside its loop (the deferred sweep owns the retry)",
            flush=True,
        )


def run_async[T](fn: Callable[[LoopClients], Awaitable[T]], *, app_cleanup: bool = True) -> T:
    """asyncio.run() with the hygiene built in: ``fn`` receives the loop's LoopClients to
    track what it opens; on the way out — still INSIDE the loop — the tracked clients are
    closed and (``app_cleanup``) the off-turn work is settled and the DB engine disposed."""

    async def _scoped() -> T:
        clients = LoopClients()
        try:
            return await fn(clients)
        finally:
            for error in await clients.aclose():
                print(f"::warning::seedtest could not close {error}", flush=True)
            if app_cleanup:
                from app.db.engine import dispose_engine

                await _settle_background_work()
                await dispose_engine()

    return asyncio.run(_scoped())


def _harness_process_posture() -> None:
    """This process exits minutes after its in-process turns: the dispatcher's in-process
    retry ladder (30 s, 120 s) would either hold a loop open or be cancelled at its close.
    The durable deferred queue the dispatcher also writes owns the retry — the same
    posture the test suite runs in."""
    from app.notifications import escalation_dispatch

    escalation_dispatch.ESCALATION_RETRY_DELAYS = ()


def usage_conversations(client: httpx.Client) -> int:
    data = _check(client.get(f"{API_BASE}/api/v1/admin/usage/overview"), "usage_overview")
    return int(data.get("conversationsThisPeriod", -1))


# --------------------------------------------------------------------------- #
# Seed one fully-provisioned synthetic tenant                                  #
# --------------------------------------------------------------------------- #
class Tenant:
    def __init__(self, role: str) -> None:
        self.role = role
        self.email = _email(role)
        self.client = httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True)
        self.embed_key: str = ""

    def seed(self) -> None:
        signup(self.client, self.email)
        token = asyncio.run(mint_verify_token(self.email))
        verify_email(self.client, token)
        login(self.client, self.email)
        self.embed_key = create_luciel(self.client, f"SeedTest {self.role}")
        paste_knowledge(self.client, KNOWN_FACT)
        print(f"[seed] tenant '{self.role}' ready (embed_key={self.embed_key})", flush=True)


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #
def test_happy_path(r: Results, t: Tenant) -> None:
    """T1 — live LLM produces a real grounded answer (not the EchoModel)."""
    out = widget_send(t.client, t.embed_key, GROUNDED_QUESTION)
    reply = out["reply"]["text"]
    is_echo = reply.startswith("Echo:")
    grounded = GROUNDED_TOKEN in reply
    active = out.get("renderState") == "active"
    ok = grounded and not is_echo and active
    r.record(
        "T1 live-LLM-happy-path",
        ok,
        f"renderState={out.get('renderState')} grounded({GROUNDED_TOKEN!r})={grounded} "
        f"echo={is_echo} reply={reply[:120]!r}",
    )


def test_free_cap_zero_llm(r: Results, t: Tenant) -> None:
    """T2 / Row 9 — at the free conversation cap the next request makes ZERO LLM calls.

    The widget opens a NEW session per unthreaded message, so each served message
    counts one conversation. The first ``free_allowance - 1`` conversations are counted
    through the real fire-once counter in-process (the widget edge deliberately caps
    new sessions per source, so 50 unthreaded messages from one address is the abuse
    pattern, not a customer); the 50th and 51st go over the wire. The over-cap request
    must (a) return the canned at-cap reply, NOT a generated answer, and (b) NOT
    advance the conversation counter — proving no LLM call was made (a served LLM
    turn would fire the fire-once counter)."""
    from app.config import get_settings

    cap = get_settings().free_allowance
    seeded = asyncio.run(seed_counted_conversations(t.email, cap - 1))
    # The 50th conversation is a real served turn: still inside the allowance.
    last_free = widget_send(t.client, t.embed_key, "Question number fifty please")
    counted_at_cap = usage_conversations(t.client)

    over = widget_send(t.client, t.embed_key, "One more question over the cap?")
    counted_after = usage_conversations(t.client)
    reply = over["reply"]["text"]

    served_last_free = last_free.get("renderState") == "active"
    at_cap_render = over.get("renderState") == "at_cap"
    no_increment = counted_after == counted_at_cap == cap
    not_generated = GROUNDED_TOKEN not in reply and not reply.startswith("Echo:")
    ok = served_last_free and at_cap_render and no_increment and not_generated
    r.record(
        "T2 row9-free-cap-zero-LLM",
        ok,
        f"cap={cap} seeded={seeded} counted@cap={counted_at_cap} "
        f"counted_after_51st={counted_after} last_free={last_free.get('renderState')} "
        f"renderState={over.get('renderState')} reply={reply[:90]!r}",
    )


def test_llm_cap_per_minute(r: Results) -> None:
    """T3 / Row 1b — >50 LLM calls/min for one admin are rejected, gracefully (429).

    IN-PROCESS (2026-09-05 audit F173): this drives the cap store and the error
    handler directly, with no model call and nothing over the wire. It proves the
    arithmetic and the edge mapping; the live router's own gate is proven by T3b,
    which is opt-in because it spends real model calls."""
    # (a) The cap admits exactly `cap` calls in the window, then rejects — for ONE admin.
    import app.runtime.llm_cap as cap_mod
    from app.runtime.llm_cap import (
        LLM_CAP_PER_MINUTE,
        InMemoryLLMCapStore,
        LLMCapExceeded,
        admit_llm_call,
    )

    fresh = InMemoryLLMCapStore()
    original = cap_mod._store
    cap_mod._store = fresh
    admin = uuid.uuid4()
    admitted = 0
    rejected = False
    try:
        for _ in range(LLM_CAP_PER_MINUTE):
            admit_llm_call(admin)  # must not raise
            admitted += 1
        try:
            admit_llm_call(admin)  # the (cap+1)-th must raise
        except LLMCapExceeded:
            rejected = True
    finally:
        cap_mod._store = original
    cap_ok = admitted == LLM_CAP_PER_MINUTE and rejected

    # (b) The edge maps LLMCapExceeded → 429 (conversation rate-limited, not dropped/500).
    from app.api.errors import register_error_handlers
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    probe = FastAPI()
    register_error_handlers(probe)

    @probe.get("/boom")
    async def _boom() -> dict:
        raise LLMCapExceeded(uuid.uuid4())

    resp = TestClient(probe, raise_server_exceptions=False).get("/boom")
    edge_ok = resp.status_code == 429 and "message" in resp.json()

    ok = cap_ok and edge_ok
    r.record(
        "T3 row1b-llm-cap-50-per-min (in-process)",
        ok,
        f"admitted={admitted}/{LLM_CAP_PER_MINUTE} rejected_51st={rejected} "
        f"edge_status={resp.status_code} (graceful 429, not dropped; cap store + edge "
        "mapping only — no model call)",
    )


def test_llm_cap_live(r: Results, admin_id: uuid.UUID) -> None:
    """T3b / Row 1b, LIVE — the real router refuses the 51st model call in a minute.

    Opt-in (LUCIEL_SEEDTEST_LIVE_CAP=1) because it spends LLM_CAP_PER_MINUTE real,
    tiny completions against the platform's primary provider. Nothing rides the
    HTTP edge: the widget's own per-source buckets would refuse a 51-message burst
    long before the LLM cap does, so the router — the layer that actually holds the
    cap (llm_router.complete → admit_llm_call) — is driven directly."""
    if os.environ.get("LUCIEL_SEEDTEST_LIVE_CAP") != "1":
        r.skip(
            "T3b row1b-llm-cap-live",
            "set LUCIEL_SEEDTEST_LIVE_CAP=1 to spend 50 real model calls proving the "
            "router's per-minute cap",
        )
        return
    import app.runtime.llm_cap as cap_mod
    from app.runtime.llm_cap import LLM_CAP_PER_MINUTE, InMemoryLLMCapStore, LLMCapExceeded
    from app.runtime.provider_factory import build_llm_router

    fresh = InMemoryLLMCapStore()
    original = cap_mod._store
    cap_mod._store = fresh
    admitted = 0
    rejected = False
    failure: str | None = None

    async def _go() -> None:
        nonlocal admitted, rejected, failure
        router = build_llm_router(admin_id=admin_id)
        try:
            for _ in range(LLM_CAP_PER_MINUTE):
                resp = await router.complete(
                    "Reply with the single word OK.",
                    [{"role": "user", "content": "OK?"}],
                )
                if not resp.text.strip():
                    failure = "empty completion"
                    return
                admitted += 1
            try:
                await router.complete("Reply with OK.", [{"role": "user", "content": "OK?"}])
            except LLMCapExceeded:
                rejected = True
        finally:
            await close_llm_clients(router)  # inside the loop that opened them

    try:
        asyncio.run(_go())
    except Exception as exc:  # noqa: BLE001 — a live failure is a FAIL with its reason
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        cap_mod._store = original
    ok = failure is None and admitted == LLM_CAP_PER_MINUTE and rejected
    r.record(
        "T3b row1b-llm-cap-live",
        ok,
        f"admitted={admitted}/{LLM_CAP_PER_MINUTE} rejected_51st={rejected} "
        f"failure={failure!r} (real router, real model calls)",
    )


async def _run_inprocess_turn(
    admin_id: uuid.UUID,
    *,
    router,
    channel: str,
    enabled_channels: set[str],
    inbound: str,
    retriever,
    tool_resolver=None,
):
    """Open a real session for the seeded tenant and run one orchestrator turn."""
    from app.db.engine import dispose_engine
    from app.db.session import tenant_session
    from app.models.instance import Instance
    from app.runtime.orchestrator import Orchestrator
    from app.runtime.sessions import participant_key_for, resolve_or_open_session
    from sqlalchemy import select

    # Every transport channel keys its session on the customer's transport identifier
    # (§3.4.8); a session with no key is refused since round 7 (SessionUnkeyable), so
    # the harness supplies the synthetic identifier a real inbound would carry — the
    # widget alone is server-keyed and opens without one.
    synthetic_identifier = {
        "email": f"parity-{RUN_ID}@seedtest.invalid",
        "sms": "+15005550006",
        "whatsapp": "+15005550006",
        "messenger": f"psid-{RUN_ID}",
        "instagram": f"igsid-{RUN_ID}",
        "voice": f"CA{RUN_ID:0>32}",
    }.get(channel)
    participant_key = participant_key_for(admin_id, channel, synthetic_identifier)

    # This coroutine is invoked from several separate asyncio.run() blocks; dispose
    # the global engine on exit so it is never reused across a dead event loop — and
    # settle the off-turn work and close the router's SDK clients first, inside THIS
    # loop (see "Event-loop hygiene").
    try:
        async with tenant_session(admin_id) as s:
            instance_id = (
                await s.execute(select(Instance.id).where(Instance.admin_id == admin_id))
            ).scalar_one()
            sess, _ = await resolve_or_open_session(
                s,
                admin_id=admin_id,
                instance_id=instance_id,
                channel=channel,
                resolved_lead_id=None,
                participant_key=participant_key,
            )
            orch = Orchestrator(
                session=s,
                router=router,
                retriever=retriever,
                admin_id=admin_id,
                instance_id=instance_id,
                enabled_channels=enabled_channels,
                business_context="We sell refurbished gadgets.",
                has_payment_method=False,
                tool_resolver=tool_resolver,
            )
            return await orch.run_turn(
                session_id=sess.id,
                resolved_lead_id=None,
                channel=channel,
                mode=sess.mode,
                inbound_text=inbound,
            )
    finally:
        await _settle_background_work()
        await close_llm_clients(router)
        await dispose_engine()


def test_failsafe_escalation(r: Results, admin_id: uuid.UUID) -> None:
    """T4 / Row 5 — a forced tool failure escalates honestly (cannot_answer), no fabrication.

    A low-confidence model proposes a tool call; with no tool backend wired the broker
    returns a `failed` outcome, retrieval is empty, so grounding is below the 0.50 floor.
    The outcome gate must fire cannot_answer — Luciel hands off honestly rather than
    inventing a confident answer."""
    from app.runtime.escalation import Signal
    from app.runtime.llm_router import LLMRouter
    from app.runtime.testing import EchoModel

    async def _no_knowledge(_q: str) -> list[str]:
        return []

    # confidence < 0.6 + a proposed tool the loop cannot resolve (→ failed outcome).
    echo = EchoModel(
        confidence=0.3,
        tool_calls=[{"tool_id": "book_appointment", "inputs": {}}],
        fixed_reply="I'm not certain about that.",
    )
    router = LLMRouter(primary=echo, fallback=echo, admin_id=admin_id)
    result = asyncio.run(
        _run_inprocess_turn(
            admin_id,
            router=router,
            channel="widget",
            enabled_channels={"widget"},
            inbound="Can you confirm the exact serial number on my specific order?",
            retriever=_no_knowledge,
            tool_resolver=None,  # no backend → broker returns `failed`
        )
    )
    esc = result.escalation
    fired_cannot_answer = esc is not None and esc.signal == Signal.cannot_answer
    tool_failed = "failed" in result.tool_outcomes
    # BUG FIX: the old `(result.grounding_score or 1.0) < 0.50` treated a *real*
    # grounding of 0.0 as falsy and defaulted it to 1.0 (ABOVE the floor), so a
    # correctly-ungrounded turn scored as "not below floor" → a false FAIL. Treat a
    # missing score as 0.0 (ungrounded) and compare the ACTUAL value, so a genuine
    # sub-floor 0.0 is recognized as below the 0.50 floor.
    grounding = result.grounding_score if result.grounding_score is not None else 0.0
    below_floor = grounding < 0.50
    # No fabricated answer surfaced: with every tool failed and grounding below the
    # floor, Luciel must NOT have returned the grounded fact it never retrieved — it
    # hands off honestly (cannot_answer) rather than inventing a confident answer.
    no_fabricated_answer = GROUNDED_TOKEN not in (result.reply_text or "")
    ok = fired_cannot_answer and tool_failed and below_floor and no_fabricated_answer
    r.record(
        "T4 row5-failsafe-escalation",
        ok,
        f"signal={esc.signal.value if esc and esc.signal else None} "
        f"tool_outcomes={result.tool_outcomes} grounding={result.grounding_score} "
        f"below_floor={below_floor} no_fabricated_answer={no_fabricated_answer} "
        f"reply={(result.reply_text or '')[:80]!r}",
    )


def test_fallback_failover(r: Results) -> None:
    """T5 / Row 8 — forced primary failure → live OpenAI fallback answers end-to-end."""
    from app.config import get_settings, parse_platform_secret

    secret = parse_platform_secret(get_settings().platform_secret)
    openai_key = secret.get("OPENAI_API_KEY", "")
    if not openai_key:
        r.skip(
            "T5 row8-primary-to-fallback",
            "no OPENAI_API_KEY in platform secret (mock mode); router unit failover is "
            "CI-proven in tests/unit",
        )
        return

    from app.runtime.llm_router import LLMRouter
    from app.runtime.providers import OpenAIProvider
    from app.runtime.testing import EchoModel

    primary = EchoModel(raise_provider_error=True)  # force immediate failover
    fallback = OpenAIProvider(
        api_key=openai_key,
        model=get_settings().llm_openai_fallback_model,
        max_output_tokens=64,
    )
    router = LLMRouter(primary=primary, fallback=fallback, admin_id=uuid.uuid4())

    async def _go():
        try:
            return await router.complete(
                "You are a helpful assistant. Answer in one short sentence.",
                [{"role": "user", "content": "Say the word 'pong' and nothing else."}],
            )
        finally:
            await close_llm_clients(router)  # inside the loop that opened them

    try:
        resp = asyncio.run(_go())
    except Exception as exc:  # noqa: BLE001 — surface the live failure as a FAIL
        r.record("T5 row8-primary-to-fallback", False, f"live fallback raised: {exc!r}")
        return
    ok = resp.used_fallback and bool(resp.text.strip())
    r.record(
        "T5 row8-primary-to-fallback",
        ok,
        f"used_fallback={resp.used_fallback} provider={resp.provider} "
        f"reply={resp.text.strip()[:80]!r} (max_completion_tokens path exercised live)",
    )


def test_crm_custom_webhook_live(r: Results, t: Tenant) -> None:
    """T7 — a captured lead reaches a REAL custom-webhook CRM endpoint (owner-provided).

    Opt-in: LUCIEL_SEEDTEST_WEBHOOK_URL names the endpoint (LUCIEL_SEEDTEST_WEBHOOK_AUTH
    an optional Authorization header). Drives the product path end to end over the
    wire: connect the CRM through the admin API, enable the Push-to-CRM tool, let the
    widget capture a lead with an email, and read the lead's honest CRM state back
    from the Leads API (F134) — never a claim the push happened."""
    url = os.environ.get("LUCIEL_SEEDTEST_WEBHOOK_URL", "").strip()
    if not url:
        r.skip(
            "T7 crm-custom-webhook-live",
            "set LUCIEL_SEEDTEST_WEBHOOK_URL (and optionally LUCIEL_SEEDTEST_WEBHOOK_AUTH) "
            "to push a captured lead to a real endpoint",
        )
        return
    from urllib.parse import urlparse

    started = _check(
        t.client.post(
            f"{API_BASE}/api/v1/admin/connections",
            json={"connectionType": "crm", "provider": "custom_webhook"},
        ),
        "crm connect",
    )
    # The start result carries the flow shape (requiresClientForm), not the row id: the
    # row is read back from the connections list exactly as the dashboard does.
    rows = _check(t.client.get(f"{API_BASE}/api/v1/admin/connections"), "connections")
    cid = next(
        (
            row.get("connectionId")
            for row in rows
            if row.get("connectionType") == "crm" and row.get("provider") == "custom_webhook"
        ),
        None,
    )
    if not started.get("requiresClientForm") or not cid:
        r.record(
            "T7 crm-custom-webhook-live",
            False,
            f"connect did not open a credential form / no crm row: {started} rows={len(rows)}",
        )
        return
    fields: dict[str, str] = {"url": url}
    auth = os.environ.get("LUCIEL_SEEDTEST_WEBHOOK_AUTH", "").strip()
    if auth:
        fields["auth_header"] = auth
    conn = _check(
        t.client.post(
            f"{API_BASE}/api/v1/admin/connections/{cid}/credentials", json={"fields": fields}
        ),
        "crm credentials",
    )
    luciel = _check(t.client.get(f"{API_BASE}/api/v1/admin/luciel"), "luciel")
    tools = [
        {"id": tool["id"], "enabled": tool["id"] == "push_to_crm" or bool(tool.get("enabled"))}
        for tool in luciel.get("tools", [])
    ]
    if not any(tool["id"] == "push_to_crm" for tool in tools):
        tools.append({"id": "push_to_crm", "enabled": True})
    _check(t.client.put(f"{API_BASE}/api/v1/admin/luciel/tools", json={"tools": tools}), "tools")

    lead_email = f"seed-lead-{RUN_ID}@example.com"
    widget_send(
        t.client,
        t.embed_key,
        f"Hi, I'm Jordan. My email is {lead_email} — please send me a quote for a "
        "refurbished laptop.",
    )
    status = detail = None
    for _ in range(10):
        leads = _check(t.client.get(f"{API_BASE}/api/v1/dashboard/leads"), "leads")
        for lead in leads:
            keys = {
                str(lead.get(k) or "").lower()
                for k in ("contactIdentifier", "email", "contact_identifier")
            }
            if lead_email in keys:
                status, detail = lead.get("crmStatus"), lead.get("crmDetail")
        if status:
            break
        time.sleep(1)
    ok = status == "synced"
    r.record(
        "T7 crm-custom-webhook-live",
        ok,
        f"connection={conn.get('status')} crmStatus={status} crmDetail={detail} "
        f"endpoint={urlparse(url).hostname}",
    )


def test_channel_parity_and_disclosure(r: Results, t: Tenant, admin_id: uuid.UUID) -> None:
    """T6 / Row 11 — same query across two channels → parity + AI-identity disclosure."""
    # Disclosure (always present on bootstrap, §3.4.16/§3.4.17).
    boot = widget_bootstrap(t.client, t.embed_key)
    disclosure_ok = (
        boot.get("aiAssistantLabel") == "AI assistant"
        and boot.get("poweredByVantageMind") is True
        and "AI assistant" in (boot.get("openingMessage") or "")
    )

    # Parity: identical inputs through the SAME loop on two different channels. A live,
    # grounded reply must appear on both and reference the seeded fact.
    from app.runtime.provider_factory import build_llm_router

    async def _knowledge(_q: str) -> list[str]:
        return [KNOWN_FACT]

    replies: dict[str, str] = {}
    why: dict[str, object] = {}
    for channel in ("widget", "email"):
        res = asyncio.run(
            _run_inprocess_turn(
                admin_id,
                router=build_llm_router(admin_id=admin_id),
                channel=channel,
                enabled_channels={"widget", "email"},
                inbound=GROUNDED_QUESTION,
                retriever=_knowledge,
            )
        )
        replies[channel] = res.reply_text
        # Why a turn handed off instead of answering (round 6 WP-G reasons): a 60-char
        # reply prefix is the disclosure line on every first turn and explains nothing.
        esc = res.escalation
        why[channel] = (
            {
                "signal": getattr(esc.signal, "value", esc.signal),
                "score": esc.score_or_confidence,
                "reasons": list(esc.reasons),
                "grounding": res.grounding_score,
            }
            if esc is not None and esc.fired
            else {"grounding": res.grounding_score}
        )

    parity_ok = all(GROUNDED_TOKEN in v and not v.startswith("Echo:") for v in replies.values())
    ok = disclosure_ok and parity_ok
    r.record(
        "T6 row11-channel-parity-and-disclosure",
        ok,
        f"disclosure={disclosure_ok} parity={parity_ok} "
        f"widget={replies.get('widget', '')[-220:]!r} email={replies.get('email', '')[-220:]!r} "
        f"why={why}",
    )


# --------------------------------------------------------------------------- #
# Entry point                                                                  #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Options:
    strict: bool


def parse_args(argv: Sequence[str], env: dict[str, str] | None = None) -> Options:
    """Command line → Options. ``--strict`` / ``--no-strict`` beat LUCIEL_SEEDTEST_STRICT
    (the task definition's default); with neither, strict is off."""
    env = dict(os.environ) if env is None else env
    p = argparse.ArgumentParser(
        prog="seed_and_livetest.py", description="§10 live-behavior SEED + TEST harness"
    )
    p.add_argument(
        "--strict",
        dest="strict",
        action="store_true",
        default=None,
        help="fail the run on any skip outside the owner-gated allowlist",
    )
    p.add_argument("--no-strict", dest="strict", action="store_false", help="opt out of strict")
    ns = p.parse_args(list(argv))
    strict = ns.strict if ns.strict is not None else env.get("LUCIEL_SEEDTEST_STRICT") == "1"
    return Options(strict=bool(strict))


def main(argv: Sequence[str] | None = None) -> int:
    opts = parse_args(sys.argv[1:] if argv is None else argv)
    print(
        f"[seedtest] API_BASE={API_BASE} RUN_ID={RUN_ID} strict={'on' if opts.strict else 'off'}",
        flush=True,
    )
    _harness_process_posture()
    r = Results()
    try:
        run_cases(r)
    except Exception as exc:  # noqa: BLE001 — an aborted run still reports its verdict
        import traceback

        traceback.print_exc()
        r.record("T-cases aborted", False, f"{type(exc).__name__}: {str(exc)[:300]}")
    return finish(r, strict=opts.strict)


def finish(r: Results, *, strict: bool) -> int:
    """Print the verdict and return the process exit code."""
    print(f"[seedtest] {r.summary()}", flush=True)
    unexpected = strict_violations(r.rows)
    if strict and unexpected:
        listed = "; ".join(f"{row.name} ({row.evidence[:120]})" for row in unexpected)
        print(
            f"::error::--strict: {len(unexpected)} case(s) skipped outside the owner-gated "
            f"allowlist: {listed}",
            flush=True,
        )
    code = exit_code(r.rows, strict=strict)
    if r.any_failed():
        print("::error::§10 live-behavior harness found FAIL(s)", flush=True)
    elif code == 0:
        print("[seedtest] §10 live-behavior harness: all checks PASS.", flush=True)
    return code


def run_cases(r: Results) -> None:
    """T1–T7 on two synthetic tenants, torn down afterwards."""
    # Seed two synthetic tenants: a primary (happy-path / parity / fail-safe) and a
    # dedicated free-cap tenant (driven to its conversation cap in T2).
    main_tenant = Tenant("main")
    cap_tenant = Tenant("cap")
    try:
        main_tenant.seed()
        main_admin_id = asyncio.run(_admin_id_for(main_tenant.email))
        cap_tenant.seed()

        # T1, T3, T4, T5, T6 do not consume the free cap; run them on the main tenant.
        test_happy_path(r, main_tenant)
        test_llm_cap_per_minute(r)
        test_llm_cap_live(r, main_admin_id)  # T3b: opt-in, spends real calls
        test_failsafe_escalation(r, main_admin_id)
        test_fallback_failover(r)
        test_channel_parity_and_disclosure(r, main_tenant, main_admin_id)
        test_crm_custom_webhook_live(r, main_tenant)  # T7: opt-in, real endpoint
        # T2 last: it exhausts the cap tenant's free allowance.
        test_free_cap_zero_llm(r, cap_tenant)
    finally:
        # 2026-09-05 audit F179: the synthetic tenants never outlive the run. Every
        # earlier run left two accounts (and their Luciels, sessions and secrets)
        # behind in the environment. LUCIEL_SEEDTEST_KEEP=1 keeps them for debugging.
        if os.environ.get("LUCIEL_SEEDTEST_KEEP") != "1":
            asyncio.run(_teardown([main_tenant, cap_tenant]))
        for tenant in (main_tenant, cap_tenant):
            tenant.client.close()


async def _teardown(tenants: list[Tenant]) -> None:
    """Hard-purge the synthetic tenants this run created (F179): the Luciel-scope
    cascade plus the billing identity and the account shell — the same destruction the
    account purge paths run, on rows only this run ever knew about."""
    from app.db.engine import dispose_engine
    from app.db.session import tenant_session
    from app.lifecycle.cascade import hard_delete_cascade
    from app.models.instance import Account
    from sqlalchemy import delete as sa_delete

    try:
        for tenant in tenants:
            if not tenant.embed_key and not tenant.email:
                continue
            try:
                admin_id = await _admin_id_for(tenant.email)
            except Exception as exc:  # noqa: BLE001 — never seeded; nothing to purge
                print(
                    f"[seedtest] teardown: {tenant.role} was never seeded ({type(exc).__name__})",
                    flush=True,
                )
                continue
            try:
                async with tenant_session(admin_id) as s:
                    counts = await hard_delete_cascade(s, admin_id, include_account_scope=True)
                    await s.execute(sa_delete(Account).where(Account.id == admin_id))
                rows = sum(counts.values())
                print(
                    f"[seedtest] teardown: purged synthetic tenant '{tenant.role}' ({rows} rows)",
                    flush=True,
                )
            except Exception as exc:  # noqa: BLE001 — report, never mask the test result
                print(
                    f"::warning::seedtest teardown could not purge '{tenant.role}': "
                    f"{type(exc).__name__}: {exc}",
                    flush=True,
                )
    finally:
        # The purge's engine was built on THIS loop; dispose it here, not at exit.
        await dispose_engine()


async def _admin_id_for(email: str) -> uuid.UUID:
    from app.auth.lookup import lookup_account_by_email
    from app.db.engine import dispose_engine
    from app.db.session import untenanted_session

    # Dispose the async engine before returning so it is never reused across a later
    # asyncio.run() loop (otherwise SQLAlchemy raises 'attached to a different loop').
    try:
        async with untenanted_session() as s:
            found = await lookup_account_by_email(s, email)
        if found is None:
            raise RuntimeError(f"could not resolve admin_id for {email}")
        return found.account_id
    finally:
        await dispose_engine()


if __name__ == "__main__":
    sys.exit(main())
