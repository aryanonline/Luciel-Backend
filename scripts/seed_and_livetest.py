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

Each test prints PASS/FAIL with evidence; the process exits non-zero if ANY fails. The
LAST line is always one summary (``[seedtest] SUMMARY result=... cases=... passed=...
failed=... skipped=... matrix=... seconds=...``), which run-seedtest-task.sh repeats after
the task and writes into the GitHub job summary.

MATRIX (``--matrix``, round 7 WP-12): the configuration COMBINATIONS the owner asked
for, M1-M8, each on its own throwaway tenant with its own assertions and wall-clock —
widget + pasted knowledge, widget channel off (D9), escalation (hot lead + ungrounded),
takeover, leads (own vs third-party address), lifecycle (pause/resume/delete/restore),
free cap (51st conversation + a counted one never cut), closure (reduced surface +
reopen). ``--only M3,M8`` picks some; ``--cases`` adds T1-T7 (the default without
``--matrix``); ``--list-matrix`` prints the catalogue and the manual live walk's items
(configurations that need an OWNER asset: a Twilio number, a mailbox, Meta, a CRM, a
calendar — never fabricated here).

PERSIST (``--persist NAME``): keep ONE named tenant for the orchestrator's customer-style
walk. NAME is a seed-domain mailbox (``e2e-walk`` -> e2e-walk@seed.vantagemind.ai, the dev
walk account). The harness cannot know that account's password, so it ADOPTS an existing
account: a session minted in-process with the app's signing key at the account's current
epoch (exactly as it already mints the verify link) — nothing about the account changes,
the owner's own sign-in keeps working. It creates a Luciel + pasted knowledge only when
the account has none, leaves an existing Luciel as it is, and records "P persist"
readiness (verified, Luciel active, knowledge present, widget serving). A MISSING account
is created through the real signup path with this run's random password (never printed;
the next --persist run adopts it). The teardown purges only accounts THIS run minted, so
the persisted tenant is never torn down. Refused outside dev (LUCIEL_ENV prod/preprod)
and for any non-seed domain. ``--persist`` alone runs no T-cases; add ``--cases``.

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
import re
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


def _derive_api_base(environ: dict[str, str] | os._Environ[str] = os.environ) -> str:
    """The API this run proves (round 7 WP-8b, owner decision D6).

    ``LUCIEL_API_BASE`` wins — the SeedTest task definition injects the environment's
    own API origin (infra/lib/env-config.ts ``hostnames``). Without it, the
    environment's name derives the same D6 host (dev → api-dev.vantagemind.ai, preprod
    → api-preprod, prod → the public api). There is deliberately NO public-host
    default any more: that default is how a harness reading one environment's
    database drives ANOTHER environment's API, and every check it then passes proves
    the wrong system. Neither set → empty, and ``main`` refuses to run."""
    explicit = (environ.get("LUCIEL_API_BASE") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    env_name = (environ.get("LUCIEL_ENV") or "").strip()
    if env_name == "prod":
        return "https://api.vantagemind.ai"
    if env_name in ("dev", "preprod"):
        return f"https://api-{env_name}.vantagemind.ai"
    return ""


API_BASE = _derive_api_base()
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
        # A skip is NOT a pass: this line used to count skips as passed ("6/8 checks
        # passed" over a run with 3 skips), contradicting the SUMMARY line under it.
        counts = {s: sum(1 for row in self.rows if row.status == s) for s in STATUSES}
        return (
            f"{counts['pass']}/{len(self.rows)} checks passed, {counts['skip']} skipped, "
            f"{counts['fail']} failed"
        )


STATUSES = ("pass", "fail", "skip")


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
    def __init__(self, role: str, *, email: str | None = None) -> None:
        self.role = role
        self.email = email or _email(role)
        self.client = httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True)
        self.embed_key: str = ""
        # Resolved once seeded (the matrix reads rows in-process by tenant).
        self.admin_id: uuid.UUID | None = None
        # A persisted tenant (--persist) is never torn down.
        self.persist = False

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
    from app.channels.runtime_entry import disclosure_business_name, names
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
            inst = (
                await s.execute(select(Instance).where(Instance.admin_id == admin_id))
            ).scalar_one()
            instance_id = inst.id
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
                # The names every production entry point passes (runtime_entry), so rule
                # 1's exact disclosure and the grounding scorer's names are the ones a
                # real customer turn uses: T6 is the live check of prompt compliance.
                business_name=disclosure_business_name(inst),
                assistant_name=names(inst)[1],
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
# Matrix (--matrix): the owner's configuration COMBINATIONS                    #
# --------------------------------------------------------------------------- #
# Each combination runs on its OWN throwaway tenant (seeded through the real API path
# exactly like the T-cases), drives one configuration over the wire, reads back what the
# owner would see (the API) and what was persisted (in-process reads as luciel_app under
# the tenant's RLS), and records ONE [PASS]/[FAIL] with its wall-clock and the evidence
# of every check in it. ``drive`` does the I/O and returns plain observations; ``judge``
# is pure (unit-tested table by table). A combination that needs an OWNER asset is not
# in the matrix — MANUAL_WALK_ITEMS lists those for the manual live walk.

HOT_UNGROUNDED_QUESTION = (
    "We have a $5,000 budget and want to buy this week - what is the exact battery "
    "capacity, in mAh, of the refurbished DJI Mavic 3 Pro drones you sell?"
)
#: The full hand-off ack in every variant. Mirrors ``orchestrator._HANDOFF_ACK_MARK``
#: (a unit test pins the two together).
HANDOFF_ACK_RE = re.compile(r"\b(?:flagged this for the team|passed this along to the team)\b")
#: A real-time connection claim the doctrine forbids: an owner notification is never a
#: person on the line (root CLAUDE.md, "Trust, grounding, and delivery").
REALTIME_CLAIM_RE = re.compile(
    r"\b(?:connecting you|connect you (?:with|to)|transferring you|transfer you"
    r"|(?:is|are) joining (?:the|this) chat|live agent|on the line)\b",
    re.IGNORECASE,
)
#: escalation.high_value_lead_reasons' phrases (the opportunity half of the row).
LEAD_REASONS = ("named a budget", "wants it soon", "ready to buy or book")
NOTIFICATION_EVENTS = (
    "escalation_notification_sent",
    "escalation_delivery_failed",
    "escalation_owner_fallback",
)
#: Who the dispatcher pages (escalation_dispatch ``recipient_role``).
CONTACT_ROLES = ("primary", "secondary")
OWNER_ROLES = ("owner_fallback", "cc_owner_email", "owner_unverified_copy")
#: Escalation rows still waiting on their off-turn notification.
UNSETTLED_DELIVERY = ("pending",)

#: The reduced surface a closed account keeps for 30 days (owner decision D5).
CLOSED_REDUCED_ROUTES: tuple[tuple[str, str], ...] = (
    ("GET", "/api/v1/admin/account"),
    ("POST", "/api/v1/admin/account/export"),
    ("POST", "/api/v1/admin/account/reopen"),
)
#: Everything else refuses a closed account with the typed 403 ``account_closed`` — the
#: writes among them refused before they change anything.
CLOSED_REFUSED_ROUTES: tuple[tuple[str, str, dict[str, object] | None], ...] = (
    ("GET", "/api/v1/admin/luciel", None),
    ("GET", "/api/v1/admin/luciel/status", None),
    ("GET", "/api/v1/admin/knowledge/sources", None),
    ("GET", "/api/v1/admin/connections", None),
    ("GET", "/api/v1/admin/usage/overview", None),
    ("GET", "/api/v1/dashboard/conversations", None),
    ("GET", "/api/v1/dashboard/leads", None),
    ("GET", "/api/v1/dashboard/escalations", None),
    ("GET", "/api/v1/billing", None),
    ("POST", "/api/v1/admin/luciel/pause", None),
    ("POST", "/api/v1/admin/account/close", {"confirmDeleteLuciel": True}),
)

#: Configurations that need an OWNER asset the harness must never fabricate — they are
#: the manual live walk's items, not matrix combinations.
MANUAL_WALK_ITEMS: tuple[tuple[str, str], ...] = (
    (
        "SMS + voice (BYO Twilio number)",
        "inbound text and a phone call answered on the owner's number; a takeover reply on "
        "voice bridged to SMS; an SMS escalation contact confirmed by its 6-digit code and "
        "its page reading sent -> delivered from the carrier receipt",
    ),
    (
        "Email channel (BYO work mailbox)",
        "an inbound email answered from the owner's own mailbox, a takeover reply in the "
        "customer's thread, the first poll's history drained unprocessed",
    ),
    (
        "Meta (WhatsApp + Messenger) and Instagram",
        "each destination bound before it reads Connected; inbound message answered; a "
        "takeover reply on the surface the customer wrote to",
    ),
    (
        "CRM",
        "a captured lead pushed and read back synced (T7 with LUCIEL_SEEDTEST_WEBHOOK_URL "
        "covers the custom-webhook flavour); crm-retry and the one-time backfill",
    ),
    (
        "Calendar",
        "check_availability + book_appointment end to end on the owner's calendar, the "
        "booking on the Today card",
    ),
)

Check = tuple[str, bool, str]


def verdict(checks: Sequence[Check]) -> tuple[bool, str]:
    """All checks must hold; the evidence names every check and what it saw."""
    ok = all(passed for _, passed, _ in checks)
    evidence = " ".join(
        f"{label}={'ok' if passed else 'FAIL'}[{detail}]" for label, passed, detail in checks
    )
    return ok, evidence


def _api(
    t: Tenant,
    method: str,
    path: str,
    *,
    json_body: dict[str, object] | None = None,
    params: dict[str, str] | None = None,
) -> httpx.Response:
    return t.client.request(method, f"{API_BASE}{path}", json=json_body, params=params)


def _body(resp: httpx.Response) -> dict | list:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict | list) else {}


def _obj(resp: httpx.Response) -> dict:
    body = _body(resp)
    return body if isinstance(body, dict) else {}


def _state_of(resp: httpx.Response) -> dict:
    """A lifecycle call's JSON object, or ``{"state": "HTTP <code>: <message>"}``."""
    body = _obj(resp)
    if resp.status_code < 400:
        return body
    return {"state": f"HTTP {resp.status_code}: {str(body.get('message') or '')[:80]}"}


def _reply(out: dict) -> str:
    return str((out.get("reply") or {}).get("text") or "")


def _require_admin_id(t: Tenant) -> uuid.UUID:
    if t.admin_id is None:
        raise RuntimeError(f"tenant '{t.role}' has no resolved admin_id")
    return t.admin_id


def widget_boot(client: httpx.Client, embed_key: str, *, session_id: str | None = None) -> dict:
    """The widget bootstrap a visitor's page makes (``session_id``: the tab already holds a
    conversation). Paced on 429 like the message send."""
    body: dict[str, str] = {"embedKey": embed_key}
    if session_id:
        body["sessionId"] = str(session_id)
    resp = _paced(
        lambda: client.post(f"{API_BASE}/api/v1/chat-widget/bootstrap", json=body),
        "widget_bootstrap",
    )
    return _check(resp, "widget_bootstrap")


def widget_history(client: httpx.Client, embed_key: str, session_id: str) -> list[dict]:
    """The widget's history poll — how a takeover reply reaches the visitor (§3.4.12)."""
    resp = _paced(
        lambda: client.get(
            f"{API_BASE}/api/v1/chat-widget/sessions/{session_id}", params={"embedKey": embed_key}
        ),
        "widget_history",
    )
    data = _check(resp, "widget_history")
    return [m for m in data if isinstance(m, dict)] if isinstance(data, list) else []


def channel_off_served() -> bool:
    """Whether THIS build's widget contract carries the D9 ``channel_off`` render state.
    The harness runs the image the API runs, so its own wire vocabulary is the API's."""
    from typing import get_args

    from app.schemas_api.widget import WidgetRenderStateWire

    return "channel_off" in get_args(WidgetRenderStateWire)


def llm_calls_in_window(admin_id: uuid.UUID) -> int | None:
    """The live router's per-minute model-call counter for this tenant (``llmcap:<admin>``,
    shared Redis — the API process increments it on every turn that calls a model).
    None when this process has no Redis-backed store (mock / in-memory: the API's calls
    are invisible from here) or the read fails — never a fabricated 0."""
    from app.runtime import llm_cap

    store = llm_cap.get_llm_cap_store()
    if not isinstance(store, llm_cap.RedisLLMCapStore):
        return None
    try:
        raw = store._client.get(llm_cap.RedisLLMCapStore._key(str(admin_id)))  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — unreadable is "no proof", never "zero calls"
        return None
    return int(raw) if raw is not None else 0


def await_quiet_llm_window(
    admin_id: uuid.UUID,
    *,
    timeout: float = 75.0,
    poll: float = 2.0,
    read: Callable[[uuid.UUID], int | None] = llm_calls_in_window,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int | None:
    """Wait (bounded) until the tenant's 60-second model-call window is empty, so a reading
    after the next turn is that turn's own count. Returns the last reading (None: no
    probe available)."""
    deadline = clock() + timeout
    while True:
        calls = read(admin_id)
        if calls is None or calls == 0 or clock() >= deadline:
            return calls
        sleep(poll)


async def _escalation_delivery(
    admin_id: uuid.UUID, session_id: str, *, timeout: float = 60.0
) -> list[dict]:
    """The session's escalation rows once their off-turn notification has settled (the
    API process pages after the turn's commit), bounded."""
    from app.db.session import tenant_session
    from app.models.conversation import EscalationEvent
    from sqlalchemy import select

    deadline = time.monotonic() + timeout
    while True:
        async with tenant_session(admin_id) as s:
            rows = (
                (
                    await s.execute(
                        select(EscalationEvent).where(
                            EscalationEvent.admin_id == admin_id,
                            EscalationEvent.session_id == uuid.UUID(session_id),
                        )
                    )
                )
                .scalars()
                .all()
            )
            out = [
                {"signal": r.signal, "status": r.delivery_status, "detail": r.delivery_detail}
                for r in rows
            ]
        settled = bool(out) and all(o["status"] not in UNSETTLED_DELIVERY for o in out)
        if settled or time.monotonic() >= deadline:
            return out
        await asyncio.sleep(2.0)


async def _notification_audit(admin_id: uuid.UUID) -> list[dict]:
    """What the dispatcher recorded doing (§3.5.5): who was paged, in which role, and
    why a send was refused — the trail a delivery status must agree with."""
    from app.db.session import tenant_session
    from app.models.audit import AdminAuditLog
    from sqlalchemy import select

    async with tenant_session(admin_id) as s:
        rows = (
            await s.execute(
                select(AdminAuditLog.event_type, AdminAuditLog.event_payload)
                .where(
                    AdminAuditLog.admin_id == admin_id,
                    AdminAuditLog.event_type.in_(NOTIFICATION_EVENTS),
                )
                .order_by(AdminAuditLog.created_at.asc())
            )
        ).all()
    return [
        {
            "event": event,
            "recipient": str((payload or {}).get("recipient") or "").lower(),
            "role": (payload or {}).get("recipient_role"),
            "failure": (payload or {}).get("failure_reason"),
        }
        for event, payload in rows
    ]


def judge_notification(
    delivery: dict, audit: Sequence[dict], *, contact: str, owner: str
) -> tuple[bool, str]:
    """Does the escalation row's delivery status say what the dispatcher actually did?

    ``delivered``/``sent`` needs a recorded send to the configured contact; ``owner_fallback``
    a send to the owner and none to the contact; ``queued``/``failed`` a recorded refusal
    and no send to the contact. Anything else (``pending`` never settled, ``unknown``, an
    SMS-only ``unconfirmed`` on an email page, no row) is not an honest settled answer."""
    status = str(delivery.get("status") or "missing")
    detail = str(delivery.get("detail") or "")
    sent = [a for a in audit if a.get("event") == "escalation_notification_sent"]
    refused = [a for a in audit if a.get("event") == "escalation_delivery_failed"]
    to_contact = [
        a for a in sent if a.get("recipient") == contact and a.get("role") in CONTACT_ROLES
    ]
    to_owner = [a for a in sent if a.get("recipient") == owner and a.get("role") in OWNER_ROLES]
    if status in ("delivered", "sent"):
        ok = bool(to_contact) and detail.startswith("email:")
    elif status == "owner_fallback":
        ok = bool(to_owner) and not to_contact
    elif status in ("queued", "failed"):
        ok = bool(refused) and not to_contact
    else:
        ok = False
    trail = ", ".join(
        f"{str(a.get('event')).removeprefix('escalation_')}:{a.get('role')}"
        + (f"({a.get('failure')})" if a.get("failure") else "")
        for a in audit
    )
    return ok, f"row={status}/{detail or '-'} audit=[{trail}]"


# --- M1 widget-only + pasted knowledge ---------------------------------------- #
def m1_drive(t: Tenant) -> dict:
    from app.config import get_settings
    from app.runtime.orchestrator import AT_CAP_REPLY

    first = widget_turn(t.client, t.embed_key, GROUNDED_QUESTION)
    status = _check(_api(t, "GET", "/api/v1/admin/luciel/status"), "employee_status") or {}
    cap = get_settings().free_allowance
    counted = usage_conversations(t.client)
    seeded = asyncio.run(seed_counted_conversations(t.email, max(cap - counted, 0)))
    boot = widget_boot(t.client, t.embed_key)
    counted_before = usage_conversations(t.client)
    over = widget_turn(t.client, t.embed_key, "Do you have refurbished tablets in stock?")
    return {
        "render": first.get("renderState"),
        "reply": _reply(first),
        "today": status.get("today") or {},
        "cap": cap,
        "seeded": seeded,
        "boot_render": boot.get("renderState"),
        "counted_before": counted_before,
        "over_render": over.get("renderState"),
        "over_reply": _reply(over),
        "counted_after": usage_conversations(t.client),
        "at_cap_reply": AT_CAP_REPLY,
    }


def judge_m1(o: dict) -> tuple[bool, str]:
    reply, over_reply = str(o["reply"]), str(o["over_reply"])
    today = o.get("today") or {}
    by_channel = today.get("byChannel") or {}
    cap = int(o["cap"])
    return verdict(
        [
            (
                "grounded_answer",
                o["render"] == "active"
                and GROUNDED_TOKEN in reply
                and not reply.startswith("Echo:"),
                f"render={o['render']} reply={reply[:100]!r}",
            ),
            (
                "today_card_counts",
                int(today.get("conversations") or 0) >= 1
                and int(by_channel.get("widget") or 0) >= 1,
                f"today.conversations={today.get('conversations')} byChannel={by_channel}",
            ),
            (
                "at_cap",
                o["boot_render"] == "at_cap"
                and o["over_render"] == "at_cap"
                and over_reply.startswith(str(o["at_cap_reply"]))
                and o["counted_before"] == o["counted_after"] == cap,
                f"cap={cap} bootstrap={o['boot_render']} next={o['over_render']} "
                f"counted {o['counted_before']}->{o['counted_after']} reply={over_reply[:60]!r}",
            ),
        ]
    )


# --- M2 widget + channel off (D9) ---------------------------------------------- #
def m2_drive(t: Tenant) -> dict:
    first = widget_turn(t.client, t.embed_key, GROUNDED_QUESTION)
    sid = str(first.get("sessionId") or "")
    luciel = _check(_api(t, "GET", "/api/v1/admin/luciel"), "luciel") or {}
    channels = [
        {"id": c["id"], "enabled": False if c["id"] == "widget" else bool(c.get("enabled"))}
        for c in luciel.get("channels") or []
        if isinstance(c, dict) and c.get("id")
    ]
    put = _api(t, "PUT", "/api/v1/admin/luciel/channels", json_body={"channels": channels})
    saved = next(
        (
            c.get("enabled")
            for c in _obj(put).get("channels") or []
            if isinstance(c, dict) and c.get("id") == "widget"
        ),
        None,
    )
    boot = widget_boot(t.client, t.embed_key)  # a NEW visitor: no session in the tab
    inflight = widget_turn(
        t.client, t.embed_key, "Does the same return window apply to gifts?", session_id=sid
    )
    return {
        "d9_served": channel_off_served(),
        "put_status": put.status_code,
        "widget_enabled_after": saved,
        "boot_new": boot.get("renderState"),
        "inflight_render": inflight.get("renderState"),
        "inflight_reply": _reply(inflight),
        "inflight_same_session": bool(sid) and str(inflight.get("sessionId")) == sid,
    }


def judge_m2(o: dict) -> tuple[bool, str]:
    inflight = (
        "in_flight_keeps_answering",
        o["inflight_render"] == "active"
        and bool(str(o["inflight_reply"]).strip())
        and bool(o["inflight_same_session"]),
        f"render={o['inflight_render']} same_session={o['inflight_same_session']} "
        f"reply={str(o['inflight_reply'])[:60]!r}",
    )
    put_ok = int(o["put_status"]) < 400
    if o["d9_served"]:
        checks: list[Check] = [
            (
                "toggle_saved",
                put_ok and o["widget_enabled_after"] is False,
                f"status={o['put_status']} widget.enabled={o['widget_enabled_after']}",
            ),
            (
                "new_visitor_channel_off",
                o["boot_new"] == "channel_off",
                f"bootstrap={o['boot_new']}",
            ),
            inflight,
        ]
        contract = "D9: this build serves renderState channel_off"
    else:
        checks = [
            ("toggle_accepted", put_ok, f"status={o['put_status']}"),
            (
                "new_visitor_current_contract",
                o["boot_new"] == "active",
                f"bootstrap={o['boot_new']}",
            ),
            inflight,
        ]
        contract = (
            "pre-D9: this build does not serve renderState channel_off (the widget is "
            "always on), so the CURRENT contract was asserted - re-run after the D9 gate ships"
        )
    ok, evidence = verdict(checks)
    return ok, f"{evidence} contract=[{contract}]"


# --- M3 escalation: hot lead + ungrounded turn ---------------------------------- #
def m3_drive(t: Tenant) -> dict:
    # The escalation contact is the seed tenant's OWN synthetic address (our domain,
    # routes nowhere real) — whatever the live tenant can deliver to, never a third
    # party's mailbox.
    contact = _email(f"{t.role}-contact").lower()
    _check(
        _api(
            t,
            "PUT",
            "/api/v1/admin/luciel/escalation",
            json_body={"primaryEmail": contact, "preferredChannel": "email"},
        ),
        "escalation contact",
    )
    out = widget_turn(t.client, t.embed_key, HOT_UNGROUNDED_QUESTION)
    sid = str(out.get("sessionId") or "")
    escalations = _check(_api(t, "GET", "/api/v1/dashboard/escalations"), "escalations") or []
    rows = [e for e in escalations if isinstance(e, dict) and str(e.get("sessionId")) == sid]
    admin_id = _require_admin_id(t)
    delivery = run_async(lambda _clients: _escalation_delivery(admin_id, sid))
    audit = run_async(lambda _clients: _notification_audit(admin_id))
    return {
        "render": out.get("renderState"),
        "reply": _reply(out),
        "rows": rows,
        "delivery": delivery,
        "audit": audit,
        "contact": contact,
        "owner": t.email.lower(),
    }


def judge_m3(o: dict) -> tuple[bool, str]:
    reply = str(o["reply"])
    rows = list(o["rows"])
    row = rows[0] if len(rows) == 1 else {}
    reasons = [str(x) for x in (row.get("reasons") or [])]
    lead = [x for x in reasons if x in LEAD_REASONS]
    answer = [x for x in reasons if "backed by your knowledge" in x or x.endswith("% sure")]
    deliveries = list(o["delivery"])
    notified, notified_detail = judge_notification(
        deliveries[0] if len(deliveries) == 1 else {},
        o["audit"],
        contact=str(o["contact"]),
        owner=str(o["owner"]),
    )
    return verdict(
        [
            (
                "honest_handoff_ack",
                o["render"] == "active"
                and bool(HANDOFF_ACK_RE.search(reply))
                and not REALTIME_CLAIM_RE.search(reply),
                f"reply={reply[:140]!r}",
            ),
            (
                "one_escalation_row",
                len(rows) == 1 and len(deliveries) == 1,
                f"rows={len(rows)} signals={[r.get('signal') for r in rows]}",
            ),
            (
                "both_reasons",
                row.get("signal") == "high_value_lead" and bool(lead) and bool(answer),
                f"signal={row.get('signal')} reasons={reasons}",
            ),
            ("notification_honest", notified, notified_detail),
        ]
    )


# --- M4 takeover ------------------------------------------------------------------ #
def m4_drive(t: Tenant) -> dict:
    first = widget_turn(t.client, t.embed_key, "Hi, do you sell refurbished laptops?")
    sid = str(first.get("sessionId") or "")
    take = _api(t, "POST", f"/api/v1/dashboard/conversations/{sid}/take-over")
    human_text = f"Hi, this is the team picking this up (seedtest {RUN_ID})."
    reply = _api(
        t,
        "POST",
        f"/api/v1/dashboard/conversations/{sid}/messages",
        json_body={"text": human_text},
    )
    reply_body = _obj(reply)
    message_id = str((reply_body.get("message") or {}).get("messageId") or "")
    stored = _body(_api(t, "GET", f"/api/v1/dashboard/conversations/{sid}/messages"))
    poll = widget_history(t.client, t.embed_key, sid)
    # A second conversation, ended by the widget's close beacon, then taken over.
    second = widget_turn(t.client, t.embed_key, "Thanks, that is all for now.")
    sid2 = str(second.get("sessionId") or "")
    end = _api(
        t, "POST", f"/api/v1/chat-widget/sessions/{sid2}/end", params={"embedKey": t.embed_key}
    )
    refused = _api(t, "POST", f"/api/v1/dashboard/conversations/{sid2}/take-over")
    return {
        "take_status": take.status_code,
        "take_mode": _obj(take).get("mode"),
        "reply_status": reply.status_code,
        "delivered": reply_body.get("delivered"),
        "delivery_detail": reply_body.get("deliveryDetail"),
        "message_id": message_id,
        "human_text": human_text,
        "stored": [
            {"messageId": str(m.get("messageId")), "role": m.get("role"), "text": m.get("text")}
            for m in (stored if isinstance(stored, list) else [])
            if isinstance(m, dict)
        ],
        "poll": [
            {"messageId": str(m.get("messageId")), "role": m.get("role"), "text": m.get("text")}
            for m in poll
        ],
        "end_status": end.status_code,
        "refused_status": refused.status_code,
        "refused_message": str(_obj(refused).get("message") or ""),
    }


def judge_m4(o: dict) -> tuple[bool, str]:
    mid = str(o["message_id"])
    stored = [m for m in o["stored"] if mid and m.get("messageId") == mid]
    served = [m for m in o["poll"] if mid and m.get("messageId") == mid]
    return verdict(
        [
            (
                "take_over",
                o["take_status"] == 200 and o["take_mode"] == "human_controlled",
                f"status={o['take_status']} mode={o['take_mode']}",
            ),
            (
                "human_reply_persisted_honestly",
                o["reply_status"] == 200
                and o["delivered"] is False
                and o["delivery_detail"] == "widget_poll_only"
                and len(stored) == 1
                and stored[0].get("role") == "human_agent"
                and stored[0].get("text") == o["human_text"],
                f"status={o['reply_status']} delivered={o['delivered']} "
                f"detail={o['delivery_detail']} stored_rows={len(stored)}",
            ),
            (
                "served_by_widget_poll",
                len(served) == 1
                and served[0].get("role") == "human"
                and served[0].get("text") == o["human_text"],
                f"poll_roles={[m.get('role') for m in o['poll']]}",
            ),
            (
                "ended_session_refused",
                o["end_status"] == 204
                and o["refused_status"] == 409
                and "session_ended" in str(o["refused_message"]),
                f"end={o['end_status']} take_over={o['refused_status']} "
                f"message={str(o['refused_message'])[:80]!r}",
            ),
        ]
    )


# --- M5 leads: own vs third-party address ---------------------------------------- #
def _lead_ids(lead: dict) -> set[str]:
    return {str(lead.get(k) or "").lower() for k in ("contactIdentifier", "email", "phone")} - {""}


def m5_drive(t: Tenant) -> dict:
    own = _email(f"{t.role}-own").lower()
    colleague = _email(f"{t.role}-colleague").lower()
    out = widget_turn(
        t.client,
        t.embed_key,
        f"Hi, I'm Jordan Lee. My email is {own}. Please also copy my colleague {colleague} "
        "on the quote for a refurbished laptop.",
    )
    leads: list[dict] = []
    for _ in range(10):
        data = _check(_api(t, "GET", "/api/v1/dashboard/leads"), "leads")
        leads = [lead for lead in data if isinstance(lead, dict)] if isinstance(data, list) else []
        if any(own in _lead_ids(lead) for lead in leads):
            break
        time.sleep(1)
    keep = ("leadId", "contactIdentifier", "email", "phone", "crmStatus", "crmDetail")
    return {
        "render": out.get("renderState"),
        "own": own,
        "colleague": colleague,
        "leads": [{k: lead.get(k) for k in keep} for lead in leads],
    }


def judge_m5(o: dict) -> tuple[bool, str]:
    own, colleague = str(o["own"]), str(o["colleague"])
    leads = list(o["leads"])
    mine = [lead for lead in leads if own in _lead_ids(lead)]
    theirs = [lead for lead in leads if colleague in _lead_ids(lead)]
    lead = mine[0] if len(mine) == 1 else {}
    return verdict(
        [
            (
                "own_email_is_the_identifier",
                len(mine) == 1 and str(lead.get("contactIdentifier") or "").lower() == own,
                f"leads={len(leads)} identifier={lead.get('contactIdentifier')!r}",
            ),
            (
                "third_party_is_not",
                not theirs,
                f"leads_keyed_on_the_colleague={len(theirs)}",
            ),
            (
                "crm_never_attempted",
                lead.get("crmStatus") == "never_attempted"
                and lead.get("crmDetail") == "crm_not_connected",
                f"crmStatus={lead.get('crmStatus')} crmDetail={lead.get('crmDetail')}",
            ),
        ]
    )


# --- M6 lifecycle ----------------------------------------------------------------- #
def m6_drive(t: Tenant) -> dict:
    key0 = t.embed_key
    paused = _state_of(_api(t, "POST", "/api/v1/admin/luciel/pause"))
    boot_paused = widget_boot(t.client, key0)
    send_paused = widget_turn(t.client, key0, "Hello, is anyone there?")
    resumed = _state_of(_api(t, "POST", "/api/v1/admin/luciel/resume"))
    boot_resumed = widget_boot(t.client, key0)
    deleted = _state_of(_api(t, "POST", "/api/v1/admin/luciel/delete"))
    boot_deleted = widget_boot(t.client, key0)
    restored = _state_of(_api(t, "POST", "/api/v1/admin/luciel/restore"))
    boot_restored = widget_boot(t.client, key0)
    return {
        "key0": key0,
        "pause_state": paused.get("state"),
        "boot_paused": boot_paused.get("renderState"),
        "boot_paused_opening": boot_paused.get("openingMessage"),
        "send_paused": send_paused.get("renderState"),
        "send_paused_reply": _reply(send_paused),
        "resume_state": resumed.get("state"),
        "boot_resumed": boot_resumed.get("renderState"),
        "delete_state": deleted.get("state"),
        "grace_started": deleted.get("graceWindowStartedAt"),
        "boot_deleted": boot_deleted.get("renderState"),
        "restore_state": restored.get("state"),
        "key_after": restored.get("embedKeyPublicId"),
        "boot_restored": boot_restored.get("renderState"),
    }


def judge_m6(o: dict) -> tuple[bool, str]:
    return verdict(
        [
            (
                "pause_renders_nothing",
                o["pause_state"] == "paused"
                and o["boot_paused"] == "paused"
                and not o["boot_paused_opening"]
                and o["send_paused"] == "paused"
                and not o["send_paused_reply"],
                f"state={o['pause_state']} bootstrap={o['boot_paused']} "
                f"send={o['send_paused']} reply={str(o['send_paused_reply'])[:40]!r}",
            ),
            (
                "resume_active",
                o["resume_state"] == "active" and o["boot_resumed"] == "active",
                f"state={o['resume_state']} bootstrap={o['boot_resumed']}",
            ),
            (
                "delete_grace_renders_nothing",
                o["delete_state"] == "luciel_grace_window"
                and bool(o["grace_started"])
                and o["boot_deleted"] == "paused",
                f"state={o['delete_state']} grace_started={bool(o['grace_started'])} "
                f"bootstrap={o['boot_deleted']}",
            ),
            (
                "restore_keeps_embed_key",
                o["restore_state"] == "active"
                and bool(o["key0"])
                and o["key_after"] == o["key0"]
                and o["boot_restored"] == "active",
                f"state={o['restore_state']} key_kept={o['key_after'] == o['key0']} "
                f"bootstrap={o['boot_restored']}",
            ),
        ]
    )


# --- M7 free cap ------------------------------------------------------------------ #
def m7_drive(t: Tenant) -> dict:
    from app.config import get_settings
    from app.runtime.orchestrator import AT_CAP_REPLY

    cap = get_settings().free_allowance
    admin_id = _require_admin_id(t)
    seeded = asyncio.run(seed_counted_conversations(t.email, cap - 1))
    last_free = widget_turn(t.client, t.embed_key, "Question number fifty please")
    fiftieth = str(last_free.get("sessionId") or "")
    counted_at_cap = usage_conversations(t.client)
    calls_before = await_quiet_llm_window(admin_id)
    over = widget_turn(t.client, t.embed_key, "One more question over the cap?")
    calls_after = llm_calls_in_window(admin_id) if calls_before is not None else None
    counted_after_over = usage_conversations(t.client)
    mid = widget_turn(
        t.client,
        t.embed_key,
        "Sorry, one follow-up: how long is the return window?",
        session_id=fiftieth,
    )
    return {
        "cap": cap,
        "seeded": seeded,
        "last_free_render": last_free.get("renderState"),
        "counted_at_cap": counted_at_cap,
        "calls_before": calls_before,
        "calls_after": calls_after,
        "over_render": over.get("renderState"),
        "over_reply": _reply(over),
        "counted_after_over": counted_after_over,
        "mid_render": mid.get("renderState"),
        "mid_reply": _reply(mid),
        "mid_same_session": bool(fiftieth) and str(mid.get("sessionId")) == fiftieth,
        "counted_after_mid": usage_conversations(t.client),
        "at_cap_reply": AT_CAP_REPLY,
    }


def judge_m7(o: dict) -> tuple[bool, str]:
    cap = int(o["cap"])
    at_cap_reply = str(o["at_cap_reply"])
    over_reply, mid_reply = str(o["over_reply"]), str(o["mid_reply"])
    before, after = o["calls_before"], o["calls_after"]
    if before is None:
        zero: Check = (
            "zero_model_calls",
            o["counted_after_over"] == o["counted_at_cap"],
            "proof=counter-only (no Redis-backed LLM cap store in this process): the "
            "fire-once counter did not advance",
        )
    else:
        zero = (
            "zero_model_calls",
            after == before,
            f"proof=llmcap window {before}->{after} (shared Redis, the API's own counter)",
        )
    return verdict(
        [
            (
                "fiftieth_served",
                o["last_free_render"] == "active" and o["counted_at_cap"] == cap,
                f"render={o['last_free_render']} counted={o['counted_at_cap']}/{cap}",
            ),
            (
                "fifty_first_at_cap",
                o["over_render"] == "at_cap"
                and over_reply.startswith(at_cap_reply)
                and o["counted_after_over"] == cap,
                f"render={o['over_render']} counted={o['counted_after_over']} "
                f"reply={over_reply[:60]!r}",
            ),
            zero,
            (
                "counted_conversation_not_cut",
                o["mid_render"] == "active"
                and bool(mid_reply.strip())
                and not mid_reply.startswith(at_cap_reply)
                and bool(o["mid_same_session"])
                and o["counted_after_mid"] == cap,
                f"render={o['mid_render']} same_session={o['mid_same_session']} "
                f"counted={o['counted_after_mid']} reply={mid_reply[:60]!r}",
            ),
        ]
    )


# --- M8 closure ------------------------------------------------------------------- #
#: The export response's link key. ExportResult is a plain BaseModel, so the link is
#: served snake_case (``download_url``) while ``emailOutcome`` is aliased — caught by a
#: local run against the real app. ``downloadUrl`` is read too, so a later camelCase
#: fix of the wire does not fail M8 (a unit test pins the served key).
EXPORT_LINK_KEYS = ("download_url", "downloadUrl")


def export_link(body: dict) -> str:
    return next((str(body[k]) for k in EXPORT_LINK_KEYS if body.get(k)), "")


def m8_drive(t: Tenant) -> dict:
    close = _api(t, "POST", "/api/v1/admin/account/close", json_body={"confirmDeleteLuciel": True})
    # The pre-closure session: the closure bumped the epoch (security contract).
    stale = _api(t, "GET", "/api/v1/admin/luciel")
    login(t.client, t.email)  # a closed account still signs in, to its reduced surface
    account = _api(t, "GET", "/api/v1/admin/account")
    export = _api(t, "POST", "/api/v1/admin/account/export")
    refused: dict[str, list[object]] = {}
    for method, path, body in CLOSED_REFUSED_ROUTES:
        resp = _api(t, method, path, json_body=body)
        refused[f"{method} {path}"] = [resp.status_code, _obj(resp).get("code")]
    reopen = _api(t, "POST", "/api/v1/admin/account/reopen")
    luciel = _api(t, "GET", "/api/v1/admin/luciel")
    leads = _api(t, "GET", "/api/v1/dashboard/leads")
    boot = widget_boot(t.client, t.embed_key)
    return {
        "close_status": close.status_code,
        "stale_status": stale.status_code,
        "account_status": account.status_code,
        "account": _obj(account),
        "export_status": export.status_code,
        "export_has_link": bool(export_link(_obj(export))),
        "refused": refused,
        "reopen_status": reopen.status_code,
        "reopen": _obj(reopen),
        "luciel_status": luciel.status_code,
        "luciel_state": _obj(luciel).get("state"),
        "leads_status": leads.status_code,
        "boot": boot.get("renderState"),
    }


def judge_m8(o: dict) -> tuple[bool, str]:
    account, reopen = o.get("account") or {}, o.get("reopen") or {}
    refused = o.get("refused") or {}
    leaked = {route: got for route, got in refused.items() if list(got) != [403, "account_closed"]}
    return verdict(
        [
            ("closed", o["close_status"] == 204, f"status={o['close_status']}"),
            (
                "old_session_revoked",
                o["stale_status"] == 401,
                f"status={o['stale_status']} (closure bumps the session epoch)",
            ),
            (
                "reduced_surface_answers",
                o["account_status"] == 200
                and account.get("state") == "closed"
                and account.get("canReopen") is True
                and o["export_status"] == 200
                and bool(o["export_has_link"]),
                f"account={o['account_status']}/{account.get('state')} "
                f"canReopen={account.get('canReopen')} export={o['export_status']} "
                f"link={o['export_has_link']}",
            ),
            (
                "everything_else_403_account_closed",
                bool(refused) and not leaked,
                f"routes={len(refused)} leaked={leaked}",
            ),
            (
                "reopen",
                o["reopen_status"] == 200
                and reopen.get("state") == "verified"
                and reopen.get("lucielRestored") is True,
                f"status={o['reopen_status']} state={reopen.get('state')} "
                f"lucielRestored={reopen.get('lucielRestored')}",
            ),
            (
                "reopen_restores",
                o["luciel_status"] == 200
                and o["luciel_state"] == "active"
                and o["leads_status"] == 200
                and o["boot"] == "active",
                f"luciel={o['luciel_status']}/{o['luciel_state']} leads={o['leads_status']} "
                f"widget={o['boot']}",
            ),
        ]
    )


@dataclass(frozen=True)
class Combination:
    key: str
    name: str
    asserts: str
    drive: Callable[[Tenant], dict]
    judge: Callable[[dict], tuple[bool, str]]


MATRIX: tuple[Combination, ...] = (
    Combination(
        "M1",
        "widget-only+pasted-knowledge",
        "a grounded widget answer; the Today card counts the conversation; at the cap the "
        "bootstrap and the next NEW conversation are at_cap with the canned reply, uncounted",
        m1_drive,
        judge_m1,
    ),
    Combination(
        "M2",
        "widget+channel-off(D9)",
        "widget toggled off: a new visitor's bootstrap reads channel_off (D9) - or, on a build "
        "without D9, the current always-on contract, said so - and an in-flight session keeps "
        "answering",
        m2_drive,
        judge_m2,
    ),
    Combination(
        "M3",
        "escalation:hot-lead+ungrounded",
        "the honest hand-off ack (no real-time claim); ONE escalation row carrying the lead "
        "AND the answer reasons; the notification row agrees with the dispatcher's audit trail",
        m3_drive,
        judge_m3,
    ),
    Combination(
        "M4",
        "takeover",
        "take-over -> a human reply persisted (delivered=false, widget_poll_only) and served by "
        "the widget poll as the team; take-over of an ENDED session refused session_ended",
        m4_drive,
        judge_m4,
    ),
    Combination(
        "M5",
        "leads:own-vs-third-party",
        "a volunteered own email becomes the lead's identifier, a mentioned colleague's does "
        "not; never_attempted / crm_not_connected with no CRM",
        m5_drive,
        judge_m5,
    ),
    Combination(
        "M6",
        "lifecycle:pause-resume-delete-restore",
        "pause -> the widget renders nothing; resume -> active; delete -> grace (renders "
        "nothing) -> restore -> active on the SAME embed key",
        m6_drive,
        judge_m6,
    ),
    Combination(
        "M7",
        "free-cap:51st+mid-conversation",
        "the 51st conversation of a tenant at 50 gets the at-cap reply with ZERO model calls "
        "(the API's own Redis call counter); the 50th conversation is not cut mid-way",
        m7_drive,
        judge_m7,
    ),
    Combination(
        "M8",
        "closure:close-reduced-surface-reopen",
        "close -> the old session revoked, account read + export answer, every other admin "
        "route 403 account_closed; reopen restores the account, the Luciel and the widget",
        m8_drive,
        judge_m8,
    ),
)
MATRIX_KEYS = tuple(c.key for c in MATRIX)


def run_combination(r: Results, combo: Combination, *, keep: bool = False) -> None:
    """One combination on its own throwaway tenant: seed, drive, judge, record, purge."""
    tenant = Tenant(combo.key.lower())
    started = time.monotonic()
    try:
        tenant.seed()
        tenant.admin_id = asyncio.run(_admin_id_for(tenant.email))
        ok, evidence = combo.judge(combo.drive(tenant))
    except Exception as exc:  # noqa: BLE001 — a combination that cannot run is a FAIL
        ok, evidence = False, f"aborted: {type(exc).__name__}: {str(exc)[:300]}"
    seconds = time.monotonic() - started
    r.record(f"{combo.key} {combo.name}", ok, evidence, kind="matrix", seconds=seconds)
    try:
        if not keep:
            asyncio.run(_teardown([tenant]))
    finally:
        tenant.client.close()


def run_matrix(r: Results, keys: Sequence[str] = ()) -> None:
    combos = [c for c in MATRIX if not keys or c.key in keys]
    keep = os.environ.get("LUCIEL_SEEDTEST_KEEP") == "1"
    print(f"[matrix] {len(combos)} combination(s): {' '.join(c.key for c in combos)}", flush=True)
    for combo in combos:
        run_combination(r, combo, keep=keep)
    print_manual_walk_items()


def print_manual_walk_items() -> None:
    print(
        "[matrix] NOT in the matrix (each needs an OWNER asset) - the manual live walk:", flush=True
    )
    for item, what in MANUAL_WALK_ITEMS:
        print(f"[matrix]   - {item}: {what}", flush=True)


def print_matrix_catalogue() -> None:
    for c in MATRIX:
        print(f"{c.key} {c.name}: {c.asserts}", flush=True)
    print_manual_walk_items()


# --------------------------------------------------------------------------- #
# Persisted walk tenant (--persist NAME)                                       #
# --------------------------------------------------------------------------- #
# Every other tenant this harness touches is minted per run (seed+<RUN_ID>-<role>@...)
# and purged. --persist keeps ONE named tenant for the orchestrator's customer-style
# walk: an existing seed-domain account is ADOPTED (never re-created, never purged), a
# missing one is created through the real signup path. Only the synthetic seed domain
# may ever be adopted, and never outside dev.
_PERSIST_NAME = re.compile(r"^[a-z0-9][a-z0-9._+-]{0,62}$")
PERSIST_REFUSED_ENVS = frozenset({"prod", "production", "preprod"})


def persist_email(name: str) -> str:
    """``e2e-walk`` -> ``e2e-walk@seed.vantagemind.ai``. A full address must already be on
    the seed domain: --persist adopts an EXISTING account, and only a synthetic seed
    account may ever be adopted — never a customer's."""
    local, at, domain = (name or "").strip().lower().partition("@")
    if at and domain != EMAIL_DOMAIN:
        raise ValueError(f"--persist adopts only {EMAIL_DOMAIN} accounts, not {domain!r}")
    if not _PERSIST_NAME.match(local):
        raise ValueError(f"--persist name {name!r} is not a plain mailbox name")
    return f"{local}@{EMAIL_DOMAIN}"


def persist_allowed(env_name: str) -> bool:
    return env_name.strip().lower() not in PERSIST_REFUSED_ENVS


def minted_by_this_run(email: str) -> bool:
    """Only the accounts THIS run signed up (``_email``) are ever purged."""
    return email.lower().startswith(f"seed+{RUN_ID}-") and email.lower().endswith(
        f"@{EMAIL_DOMAIN}"
    )


def teardown_plan(tenants: Sequence[Tenant]) -> tuple[list[Tenant], list[Tenant]]:
    """(purge, keep). A persisted tenant is never purged, and neither is any account this
    run did not mint itself (the walk account, another run's tenant) — whatever list it
    ended up in."""
    purge: list[Tenant] = []
    keep: list[Tenant] = []
    for tenant in tenants:
        if tenant.persist or not minted_by_this_run(tenant.email):
            keep.append(tenant)
        elif tenant.embed_key or tenant.email:
            purge.append(tenant)
    return purge, keep


async def _account_for(email: str) -> dict | None:
    """What adoption needs to know about an existing account, or None if there is none."""
    from app.auth.lookup import lookup_account_by_email
    from app.db.engine import dispose_engine
    from app.db.session import tenant_session, untenanted_session
    from app.models.instance import Account
    from sqlalchemy import select

    try:
        async with untenanted_session() as s:
            found = await lookup_account_by_email(s, email)
        if found is None:
            return None
        async with tenant_session(found.account_id) as s:
            user_id = (
                await s.execute(select(Account.user_id).where(Account.id == found.account_id))
            ).scalar_one()
        return {
            "admin_id": found.account_id,
            "user_id": user_id,
            "epoch": int(found.session_epoch or 0),
            "state": str(found.state),
            "verified": bool(found.email_verified),
        }
    finally:
        await dispose_engine()


def adopt_session(t: Tenant, account: dict) -> None:
    """Sign the harness in AS an existing seed account without its password: an ordinary
    session token minted in-process with the app's own signing key at the account's
    CURRENT epoch — the same in-process minting the harness already does for the verify
    link (``mint_verify_token``). Nothing about the account changes (no password reset, no
    epoch bump), so the owner's own sign-in keeps working."""
    from urllib.parse import urlparse

    from app.auth.tokens import mint_session_token
    from app.config import get_settings

    token = mint_session_token(account["admin_id"], account["user_id"], int(account["epoch"]))
    t.client.cookies.set(
        get_settings().session_cookie_name, token, domain=urlparse(API_BASE).hostname or ""
    )


def provision_persisted(t: Tenant) -> dict:
    """Adopt (or create) the named tenant and make sure it can be walked: a Luciel with
    knowledge whose widget serves. An existing Luciel is left exactly as it is."""
    account = asyncio.run(_account_for(t.email))
    created_account = account is None
    if account is None:
        # A fresh environment: the real signup path, with THIS run's random password
        # (never printed). Later runs adopt it like any existing walk account.
        t.seed()
        account = asyncio.run(_account_for(t.email))
        if account is None:
            raise RuntimeError(f"{t.email} not found after signup")
    elif account["state"] != "verified" or not account["verified"]:
        return {
            "email": t.email,
            "adopted": True,
            "account_state": account["state"],
            "verified": account["verified"],
            "created_luciel": False,
            "luciel_state": None,
            "embed_key": "",
            "sources": 0,
            "boot": None,
        }
    else:
        adopt_session(t, account)
    t.admin_id = account["admin_id"]
    luciel = _check(_api(t, "GET", "/api/v1/admin/luciel"), "luciel")
    created_luciel = False
    if not luciel:
        t.embed_key = create_luciel(t.client, "SeedTest walk")
        paste_knowledge(t.client, KNOWN_FACT)
        created_luciel = True
        luciel = _check(_api(t, "GET", "/api/v1/admin/luciel"), "luciel") or {}
    t.embed_key = str(luciel.get("embedKeyPublicId") or t.embed_key or "")
    sources = _check(_api(t, "GET", "/api/v1/admin/knowledge/sources"), "knowledge sources")
    boot = widget_boot(t.client, t.embed_key) if t.embed_key else {}
    return {
        "email": t.email,
        "adopted": not created_account,
        "account_state": account["state"],
        "verified": account["verified"],
        "created_luciel": created_luciel,
        "luciel_state": luciel.get("state"),
        "embed_key": t.embed_key,
        "sources": len(sources) if isinstance(sources, list) else 0,
        "boot": boot.get("renderState"),
    }


def judge_persist(o: dict) -> tuple[bool, str]:
    return verdict(
        [
            (
                "account_ready",
                o["account_state"] == "verified" and o["verified"] is True,
                f"{'adopted' if o['adopted'] else 'created'} state={o['account_state']} "
                f"verified={o['verified']}",
            ),
            (
                "luciel_active",
                o["luciel_state"] == "active",
                f"state={o['luciel_state']} created_now={o['created_luciel']}",
            ),
            ("knowledge_present", int(o["sources"]) >= 1, f"sources={o['sources']}"),
            (
                "widget_serving",
                o["boot"] == "active",
                f"bootstrap={o['boot']} embed_key={o['embed_key']}",
            ),
        ]
    )


def run_persist(r: Results, name: str) -> None:
    """Provision the named walk tenant, record its readiness, and leave it in place."""
    started = time.monotonic()
    email = name
    try:
        email = persist_email(name)
        env_name = os.environ.get("LUCIEL_ENV", "")
        if not persist_allowed(env_name):
            raise RuntimeError(f"--persist is a dev instrument; refused in LUCIEL_ENV={env_name}")
        tenant = Tenant(f"persist-{email.partition('@')[0]}", email=email)
        tenant.persist = True
        try:
            ok, evidence = judge_persist(provision_persisted(tenant))
        finally:
            tenant.client.close()
    except Exception as exc:  # noqa: BLE001 — an unprovisioned walk tenant is a FAIL
        ok, evidence = False, f"aborted: {type(exc).__name__}: {str(exc)[:300]}"
    r.record(f"P persist {email}", ok, evidence, seconds=time.monotonic() - started)
    print(
        f"[persist] {email} is kept: this harness never purges a persisted tenant",
        flush=True,
    )


# --------------------------------------------------------------------------- #
# Entry point                                                                  #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Options:
    strict: bool
    cases: bool = True
    matrix: bool = False
    only: tuple[str, ...] = ()
    list_matrix: bool = False
    persist: str | None = None


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
    p.add_argument(
        "--matrix",
        action="store_true",
        help="run the configuration combinations (M1-M8), each on its own throwaway tenant",
    )
    p.add_argument(
        "--cases",
        action="store_true",
        help="run T1-T7 too (the default when --matrix is not given)",
    )
    p.add_argument(
        "--only",
        default="",
        metavar="M1,M3",
        help="with --matrix: run only these combinations",
    )
    p.add_argument(
        "--list-matrix",
        action="store_true",
        help="print the combinations and the manual live walk's items, then exit",
    )
    p.add_argument(
        "--persist",
        metavar="NAME",
        default=None,
        help=f"adopt (or create) the tenant NAME@{EMAIL_DOMAIN}, make it walkable and keep it",
    )
    ns = p.parse_args(list(argv))
    if ns.persist is not None:
        try:
            persist_email(ns.persist)
        except ValueError as exc:
            p.error(str(exc))
    strict = ns.strict if ns.strict is not None else env.get("LUCIEL_SEEDTEST_STRICT") == "1"
    only = tuple(dict.fromkeys(k.strip().upper() for k in ns.only.split(",") if k.strip()))
    unknown = [k for k in only if k not in MATRIX_KEYS]
    if unknown:
        p.error(f"unknown combination(s) {', '.join(unknown)}; known: {', '.join(MATRIX_KEYS)}")
    if only and not ns.matrix:
        p.error("--only selects matrix combinations; add --matrix")
    return Options(
        strict=bool(strict),
        cases=ns.cases or not (ns.matrix or ns.persist),
        matrix=ns.matrix,
        only=only,
        list_matrix=ns.list_matrix,
        persist=ns.persist,
    )


def _guarded(r: Results, label: str, block: Callable[[Results], None]) -> None:
    """Run one block; an exception that escapes it is a recorded FAIL, so an aborted
    run still ends with its verdict and summary instead of a bare traceback exit."""
    try:
        block(r)
    except Exception as exc:  # noqa: BLE001 — an aborted run still reports its verdict
        import traceback

        traceback.print_exc()
        r.record(f"{label} aborted", False, f"{type(exc).__name__}: {str(exc)[:300]}")


def main(argv: Sequence[str] | None = None) -> int:
    opts = parse_args(sys.argv[1:] if argv is None else argv)
    if opts.list_matrix:
        print_matrix_catalogue()
        return 0
    if not API_BASE:
        print(
            "::error::seedtest: no API to prove — set LUCIEL_API_BASE (the SeedTest task "
            "injects it) or LUCIEL_ENV (dev|preprod|prod); there is no public-host default",
            flush=True,
        )
        return 2
    started = time.monotonic()
    print(
        f"[seedtest] API_BASE={API_BASE} RUN_ID={RUN_ID} strict={'on' if opts.strict else 'off'} "
        f"cases={'on' if opts.cases else 'off'} "
        f"matrix={(','.join(opts.only) or 'all') if opts.matrix else 'off'} "
        f"persist={opts.persist or 'off'}",
        flush=True,
    )
    _harness_process_posture()
    r = Results()
    if opts.persist:
        persist_name = opts.persist
        _guarded(r, "persist", lambda results: run_persist(results, persist_name))
    if opts.cases:
        _guarded(r, "T-cases", run_cases)
    if opts.matrix:
        _guarded(r, "matrix", lambda results: run_matrix(results, opts.only))
    return finish(r, strict=opts.strict, seconds=time.monotonic() - started)


#: The harness's LAST line. scripts/run-seedtest-task.sh finds it in the task's log and
#: prints it again after the task (and into the GitHub job summary): keep it one line.
SUMMARY_PREFIX = "[seedtest] SUMMARY"


def summary_line(rows: Sequence[Row], *, strict: bool, seconds: float) -> str:
    """One line: the verdict, every recorded check (T-cases, the persisted tenant, matrix
    combinations) by outcome, the matrix's own tally, strict, and the wall-clock."""
    passed = sum(1 for row in rows if row.status == "pass")
    failed = sum(1 for row in rows if row.status == "fail")
    skipped = sum(1 for row in rows if row.status == "skip")
    matrix = [row for row in rows if row.kind == "matrix"]
    matrix_passed = sum(1 for row in matrix if row.status == "pass")
    result = "PASS" if exit_code(rows, strict=strict) == 0 else "FAIL"
    return (
        f"{SUMMARY_PREFIX} result={result} cases={len(rows)} passed={passed} failed={failed} "
        f"skipped={skipped} matrix={len(matrix)} matrix_passed={matrix_passed} "
        f"matrix_failed={len(matrix) - matrix_passed} strict={'on' if strict else 'off'} "
        f"unexpected_skips={len(strict_violations(rows))} seconds={seconds:.1f}"
    )


def finish(r: Results, *, strict: bool, seconds: float = 0.0) -> int:
    """Print the verdict, end with the one-line SUMMARY, return the process exit code."""
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
    print(summary_line(r.rows, strict=strict, seconds=seconds), flush=True)
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

    purge, keep = teardown_plan(tenants)
    for tenant in keep:
        print(f"[seedtest] teardown: kept '{tenant.role}' ({tenant.email})", flush=True)
    try:
        for tenant in purge:
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
