"""Website origins for the embeddable widget (round 6 WP-I, audit F066, §3.1.5).

The embed key is public by design: anyone who reads a customer's page source has it,
and until now any page could load the chat with it — the edge WAF never enforced the
Origin/Referer allowlist its header claimed. The owner can now name the sites the
chat may load on (``instances.allowed_origins``); the widget data plane compares the
page origin of every bootstrap / send / history call against that list and refuses
the rest with an honest 403. Unset means what it always meant: any page.

An origin is ``scheme://host[:port]`` — no path, query, fragment or credentials —
lower-cased, with the scheme's default port dropped, so ``https://Acme.com/`` and
``https://acme.com:443`` compare equal to ``https://acme.com``.
"""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlsplit

#: How many sites one Luciel may be embedded on. A business has a site, maybe a
#: staging copy and a landing page; ten is room, not a target.
MAX_ALLOWED_ORIGINS = 10

_DEFAULT_PORTS = {"http": 80, "https": 443}


def normalize_origin(raw: str) -> str | None:
    """``scheme://host[:port]`` for a website address, or None when ``raw`` is not one.

    Accepts a bare origin or a page URL with an empty path (a trailing slash is
    forgiven); refuses paths, queries, fragments, credentials, non-http(s) schemes
    and anything with whitespace — an origin with a path is a mistake the owner
    would otherwise never notice until the chat refused their own site.
    """
    value = (raw or "").strip()
    if not value or any(ch.isspace() for ch in value):
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme.lower() not in _DEFAULT_PORTS or not parts.hostname:
        return None
    if parts.username or parts.password or parts.query or parts.fragment:
        return None
    if parts.path not in ("", "/"):
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = parts.hostname.lower()
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != _DEFAULT_PORTS[scheme]:
        netloc = f"{netloc}:{port}"
    return f"{scheme}://{netloc}"


def request_origin(headers: Mapping[str, str]) -> str | None:
    """The page origin a browser request came from: ``Origin``, else the ``Referer``'s
    origin (a same-origin GET may omit ``Origin``). None when neither is usable —
    an opaque ``null`` origin (a sandboxed frame, a file:// page) counts as none."""
    for name in ("origin", "referer"):
        raw = headers.get(name)
        if not raw:
            continue
        candidate = raw.strip()
        if candidate.lower() == "null":
            return None
        parts = urlsplit(candidate)
        # A Referer carries the page path; only its origin matters here.
        origin = normalize_origin(f"{parts.scheme}://{parts.netloc}") if parts.netloc else None
        if origin is not None:
            return origin
    return None
