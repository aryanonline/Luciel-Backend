"""Check that a running local Docker stack works end to end (CI's quickstart job runs it).

Run it on the host after ``docker compose up`` (and after the ``seed`` service finished):

    python scripts/local/smoke.py

It only reads, plus one widget conversation: the API is healthy, the dashboard and the
demo website answer, the demo owner can sign in and has a Luciel with its documents,
the widget opens a conversation and gets a reply, and the email inbox caught the
demo owner's verification email. ``SMOKE_EXPECT_POSTURE`` (default ``live``) is the
``llmPosture`` /healthz must report; CI runs without a key and sets ``follow``.
Standard library only, so it runs from any Python 3.12.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from typing import Any

API = os.environ.get("SMOKE_API", "http://localhost:8000")
WEB = os.environ.get("SMOKE_WEB", "http://localhost:3000")
DEMO_SITE = os.environ.get("SMOKE_DEMO_SITE", "http://localhost:8080")
INBOX = os.environ.get("SMOKE_INBOX", "http://localhost:8025")
EMAIL = os.environ.get("LUCIEL_DEMO_EMAIL", "demo@example.com")
PASSWORD = os.environ.get("LUCIEL_DEMO_PASSWORD", "luciel-demo")  # pragma: allowlist secret
EXPECT_POSTURE = os.environ.get("SMOKE_EXPECT_POSTURE", "live")

_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))


def request(
    method: str, url: str, body: object | None = None, headers: dict[str, str] | None = None
) -> tuple[int, str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(  # noqa: S310 - fixed http://localhost URLs
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with _opener.open(req, timeout=120) as response:
            return response.status, response.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")


def check(ok: bool, label: str, detail: Any = "") -> None:
    print(f"{'ok  ' if ok else 'FAIL'} {label}" + (f" ({detail})" if detail and not ok else ""))
    if not ok:
        raise SystemExit(1)


def main() -> int:
    status, body = request("GET", f"{API}/healthz")
    health = json.loads(body) if status == 200 else {}
    check(health.get("status") == "ok", "API healthy", body[:300])
    check(health.get("integrationMode") == "mock", "integration mode is mock", health)
    check(health.get("llmPosture") == EXPECT_POSTURE, f"AI posture is {EXPECT_POSTURE}", health)

    status, _ = request("GET", f"{WEB}/login")
    check(status == 200, "dashboard serves /login", status)
    status, page = request("GET", f"{DEMO_SITE}/")
    check(status == 200 and "Maple Lane Dental" in page, "demo website serves /", status)
    status, bundle = request("GET", f"{DEMO_SITE}/v1/luciel.js")
    check(status == 200 and len(bundle) > 1000, "demo website serves /v1/luciel.js", status)

    status, body = request(
        "POST", f"{API}/api/v1/auth/login", {"email": EMAIL, "password": PASSWORD}
    )
    check(status == 200, "demo owner signs in", body[:300])
    status, body = request("GET", f"{API}/api/v1/admin/luciel")
    luciel = json.loads(body) if status == 200 else None
    key = (luciel or {}).get("embedKeyPublicId")
    check(bool(key), "demo Luciel exists with an embed key", body[:300])
    status, body = request("GET", f"{API}/api/v1/admin/knowledge/sources")
    sources = json.loads(body) if status == 200 else []
    check(len(sources) >= 3, "demo knowledge is in place", f"{len(sources)} sources")

    origin = {"Origin": DEMO_SITE}
    status, body = request("POST", f"{API}/api/v1/chat-widget/bootstrap", {"embedKey": key}, origin)
    check(status == 200 and json.loads(body).get("renderState"), "widget bootstraps", body[:300])
    status, body = request(
        "POST",
        f"{API}/api/v1/chat-widget/messages",
        {"embedKey": key, "text": "What are your opening hours on Saturday?"},
        origin,
    )
    reply = json.loads(body).get("reply", {}) if status == 200 else {}
    check(bool(reply.get("text")), "widget conversation gets a reply", body[:300])
    print(f"     reply: {reply.get('text', '')[:200]!r}")

    status, body = request("GET", f"{INBOX}/api/v1/messages")
    total = json.loads(body).get("total", 0) if status == 200 else 0
    check(total >= 1, "email inbox caught the demo owner's email", f"{total} messages")
    print("local stack: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
