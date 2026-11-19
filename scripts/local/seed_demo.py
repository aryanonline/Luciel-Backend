"""Create the ready-to-use demo account for the local Docker stack (the ``seed`` service).

Signs up a demo owner through the real API, verifies the email, creates the Luciel for a
fictional dental clinic and pastes its three documents (``demo_knowledge/``), so someone
trying Luciel can sign in and chat straight away. Safe to run again: each step checks
what already exists and only adds what is missing, and the last lines it prints are the
URLs and sign-in to use.

Development only: it refuses to run unless ``LUCIEL_ENVIRONMENT=development`` and
``LUCIEL_INTEGRATION_MODE=mock``, the local stack's settings.

Environment (all optional): ``LUCIEL_DEMO_API`` (default http://api:8000),
``LUCIEL_DEMO_EMAIL`` (demo@example.com), ``LUCIEL_DEMO_PASSWORD`` (luciel-demo),
``LUCIEL_DEMO_DASHBOARD_URL`` (http://localhost:3000), ``LUCIEL_DEMO_SITE_URL``
(http://localhost:8080), ``LUCIEL_DEMO_INBOX_URL`` (http://localhost:8025).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

KNOWLEDGE_DIR = Path(__file__).resolve().parent / "demo_knowledge"

API = os.environ.get("LUCIEL_DEMO_API", "http://api:8000").rstrip("/")
EMAIL = os.environ.get("LUCIEL_DEMO_EMAIL", "demo@example.com")
PASSWORD = os.environ.get("LUCIEL_DEMO_PASSWORD", "luciel-demo")  # pragma: allowlist secret
DASHBOARD_URL = os.environ.get("LUCIEL_DEMO_DASHBOARD_URL", "http://localhost:3000")
DEMO_SITE_URL = os.environ.get("LUCIEL_DEMO_SITE_URL", "http://localhost:8080")
INBOX_URL = os.environ.get("LUCIEL_DEMO_INBOX_URL", "http://localhost:8025")

BUSINESS = {
    "name": "Ava",
    "websiteUrl": "http://localhost:8080",
    "businessOneLiner": (
        "Maple Lane Dental is a family dental clinic in Toronto: check-ups, cleanings, "
        "whitening, fillings and crowns, and same-day emergency visits."
    ),
}
BUSINESS_SHORT_NAME = "Maple Lane Dental"
TIMEZONE = "America/Toronto"
PERSONALITY_PRESET = "warm_concierge"


class ApiError(RuntimeError):
    def __init__(self, status: int, what: str, body: str) -> None:
        super().__init__(f"{what}: HTTP {status}: {body[:300]}")
        self.status = status


class Client:
    """A tiny cookie-keeping JSON client (the session cookie is httpOnly, Secure=false
    on the local stack)."""

    def __init__(self, base: str) -> None:
        self.base = base
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))

    def call(self, method: str, path: str, what: str, body: object | None = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(  # noqa: S310 - the API's own http(s) URL
            self.base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with self.opener.open(request, timeout=120) as response:
                raw = response.read().decode() or "null"
        except urllib.error.HTTPError as exc:
            raise ApiError(exc.code, what, exc.read().decode(errors="replace")) from None
        return json.loads(raw)


def require_local() -> None:
    environment = os.environ.get("LUCIEL_ENVIRONMENT", "development")
    mode = os.environ.get("LUCIEL_INTEGRATION_MODE", "mock")
    if environment != "development" or mode != "mock":
        raise SystemExit(
            "seed_demo.py only runs on a developer's own computer "
            f"(LUCIEL_ENVIRONMENT=development, LUCIEL_INTEGRATION_MODE=mock); got "
            f"{environment!r} / {mode!r}."
        )


def wait_for_api(client: Client, seconds: int = 180) -> None:
    deadline = time.monotonic() + seconds
    while True:
        try:
            if client.call("GET", "/healthz", "health").get("status") == "ok":
                return
        except (OSError, ApiError, ValueError):
            pass
        if time.monotonic() > deadline:
            raise SystemExit(f"the API at {API} did not become healthy in {seconds} s")
        time.sleep(3)


async def _mint_verify_token(email: str) -> str:
    """Re-mint the verification link's token from the account's pending id, with the
    API's own signing key, then use the real /verify-email route (the email itself also
    reaches the local inbox)."""
    from app.auth.lookup import lookup_account_by_email
    from app.auth.tokens import mint_link_token
    from app.config import get_settings
    from app.db.engine import dispose_engine
    from app.db.session import tenant_session, untenanted_session
    from app.models.instance import Account
    from sqlalchemy import select

    try:
        async with untenanted_session() as s:
            found = await lookup_account_by_email(s, email)
        if found is None:
            raise SystemExit(f"no account for {email} after sign-up")
        async with tenant_session(found.account_id) as s:
            account = (
                await s.execute(select(Account).where(Account.id == found.account_id))
            ).scalar_one()
            jti = account.pending_verify_jti
        if not jti:
            raise SystemExit(f"{email} has no pending verification to complete")
        ttl = get_settings().verification_link_ttl_seconds
        return mint_link_token(found.account_id, "verify", ttl, jti)
    finally:
        await dispose_engine()


def sign_in(client: Client) -> None:
    """Log in, signing up and verifying first when the account does not exist yet."""
    try:
        session = client.call(
            "POST", "/api/v1/auth/login", "login", {"email": EMAIL, "password": PASSWORD}
        )
    except ApiError as exc:
        if exc.status not in (400, 401, 404):
            raise
        print(f"[seed] creating the demo owner {EMAIL}", flush=True)
        client.call(
            "POST",
            "/api/v1/auth/signup",
            "signup",
            # No hCaptcha secret on the local stack, so the check is skipped; the field is
            # still required to be non-empty.
            {"email": EMAIL, "password": PASSWORD, "captchaToken": "local-demo"},
        )
        session = client.call(
            "POST", "/api/v1/auth/login", "login", {"email": EMAIL, "password": PASSWORD}
        )
    if session.get("nextRoute") == "verify_wall":
        print("[seed] verifying the demo owner's email", flush=True)
        token = asyncio.run(_mint_verify_token(EMAIL))
        client.call("POST", "/api/v1/auth/verify-email", "verify", {"token": token})
        client.call("POST", "/api/v1/auth/login", "login", {"email": EMAIL, "password": PASSWORD})


def ensure_luciel(client: Client) -> dict[str, Any]:
    luciel = client.call("GET", "/api/v1/admin/luciel", "read luciel")
    if not luciel:
        print("[seed] creating the Luciel for Maple Lane Dental", flush=True)
        luciel = client.call("POST", "/api/v1/admin/luciel", "create luciel", BUSINESS)
        client.call(
            "PUT",
            "/api/v1/admin/luciel/business-name",
            "business name",
            {"businessShortName": BUSINESS_SHORT_NAME},
        )
        client.call(
            "PUT",
            "/api/v1/admin/luciel/team-availability",
            "timezone",
            {"timezone": TIMEZONE, "enabled": False},
        )
        luciel = client.call(
            "PUT",
            "/api/v1/admin/luciel/personality",
            "personality",
            {"preset": PERSONALITY_PRESET, "businessContext": BUSINESS["businessOneLiner"]},
        )
    return luciel


def ensure_knowledge(client: Client) -> None:
    sources = client.call("GET", "/api/v1/admin/knowledge/sources", "sources")
    existing = {s.get("name") for s in sources}
    for path in sorted(KNOWLEDGE_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        name = text.splitlines()[0].lstrip("# ").strip()
        if name in existing:
            continue
        print(f"[seed] adding knowledge: {name}", flush=True)
        client.call(
            "POST", "/api/v1/admin/knowledge/sources/paste", "paste", {"name": name, "text": text}
        )


def main() -> int:
    require_local()
    client = Client(API)
    wait_for_api(client)
    sign_in(client)
    luciel = ensure_luciel(client)
    ensure_knowledge(client)
    key = luciel.get("embedKeyPublicId") or ""
    demo_link = f"{DEMO_SITE_URL}/?{urllib.parse.urlencode({'key': key})}" if key else DEMO_SITE_URL
    print(
        "\n"
        "==============================================================================\n"
        " Luciel is ready.\n\n"
        f"   Dashboard     {DASHBOARD_URL}\n"
        f"                 sign in as {EMAIL} / {PASSWORD}\n"
        f"   Demo website  {demo_link}\n"
        f"   Email inbox   {INBOX_URL}\n"
        f"   API docs      {API.replace('http://api:', 'http://localhost:')}/docs\n"
        "==============================================================================\n",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
