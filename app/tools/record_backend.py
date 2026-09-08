"""Record-source backends — the effect layer behind ``lookup_record`` (§3.3.6).

The ``csv`` provider's live system of record is the admin's uploaded rows
(``record_source_records``), queried AT CALL TIME through the tool's own session
— never a knowledge chunk, never a cached snapshot (§3.2 boundary). Outcomes
follow the honest vocabulary exactly: 0 matches → ``empty`` (a true statement
about the admin's data), 1 → ``ok``, several → ``ambiguous`` with candidates.

Matching is deterministic and explainable: a row matches when EVERY word of the
query appears (case-insensitive substring) somewhere in the row's field values.
No fuzzy scoring — an exact-record tool that "kind of" matches would put the
loop in the position of narrating a guess (§3.3.4 faithful readback).
"""

from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy import select

from app.models.record_source import RecordSourceRecord
from app.tools.contract import ToolContext

# ambiguous candidates are for the customer to disambiguate between — a page of
# them is a search result, not a disambiguation. Cap small, deterministically.
AMBIGUOUS_CANDIDATE_CAP = 5


class RecordSourceUnavailable(RuntimeError):
    """The record source cannot be read right now — surfaces as ``failed``."""


class CsvRecordBackend:
    """``RecordBackend`` over the tenant's uploaded CSV rows."""

    async def lookup(self, ctx: ToolContext, query: str) -> list[dict[str, Any]]:
        if ctx.session is None:
            raise RecordSourceUnavailable("record_source_session_unavailable")
        rows = (
            (
                await ctx.session.execute(
                    select(RecordSourceRecord)
                    .where(
                        RecordSourceRecord.admin_id == ctx.admin_id,
                        RecordSourceRecord.instance_id == ctx.instance_id,
                    )
                    .order_by(RecordSourceRecord.ordinal)
                )
            )
            .scalars()
            .all()
        )
        tokens = [t for t in query.lower().split() if t]
        if not tokens:
            return []
        matches: list[dict[str, Any]] = []
        for row in rows:
            # Keys AND values: the admin's column names carry meaning ("package",
            # "price") and queries naturally mention them ("the starter package").
            haystack = " ".join(
                f"{str(k).lower()} {str(v).lower()}" for k, v in (row.fields or {}).items()
            )
            if all(token in haystack for token in tokens):
                matches.append(dict(row.fields or {}))
                if len(matches) > AMBIGUOUS_CANDIDATE_CAP:
                    # Already unambiguously ambiguous; a bounded list is enough
                    # for the loop to ask the customer to narrow down.
                    break
        return matches


# --------------------------------------------------------------------------- #
# the live connector (round 6 WP-H, audit F062)
# --------------------------------------------------------------------------- #
#: The owner's endpoint answers ``GET <url>?q=<query>`` with a JSON array of records.
LIVE_RECORD_TIMEOUT_SECONDS = 10.0
LIVE_RECORD_MAX_BYTES = 256 * 1024


class RecordTransport(Protocol):
    async def get(self, url: str, *, headers: dict[str, str]) -> tuple[int, bytes]:
        """(status, body) — never raises for an HTTP status; raises for transport failure."""
        ...


class MockRecordTransport:
    """Mock/CI: answers from a seeded table keyed by the endpoint URL (without the
    query), so a test controls exactly what the owner's system returns."""

    responses: dict[str, object] = {}

    @classmethod
    def reset(cls) -> None:
        cls.responses.clear()

    @classmethod
    def seed(cls, url: str, rows: object) -> None:
        cls.responses[url.rstrip("/")] = rows

    async def get(self, url: str, *, headers: dict[str, str]) -> tuple[int, bytes]:
        import json  # noqa: PLC0415

        base = url.split("?", 1)[0].rstrip("/")
        if base not in self.responses:
            return 200, b"[]"
        payload = self.responses[base]
        if isinstance(payload, int):
            return payload, b""
        if isinstance(payload, (bytes, str)):
            return 200, payload if isinstance(payload, bytes) else payload.encode("utf-8")
        return 200, json.dumps(payload).encode("utf-8")


class HttpRecordTransport:
    """Live: connect to the address that was validated, with the owner's hostname as
    Host + SNI (the crawl / webhook pin, audit F090 / R-WP4-1)."""

    async def get(self, url: str, *, headers: dict[str, str]) -> tuple[int, bytes]:
        import httpx  # noqa: PLC0415

        from app.connections import sandbox  # noqa: PLC0415

        pinned_url, host = sandbox.pin_public_target(url)
        async with httpx.AsyncClient(timeout=LIVE_RECORD_TIMEOUT_SECONDS) as client:
            async with client.stream(
                "GET",
                pinned_url,
                headers={**headers, "Host": host},
                extensions={"sni_hostname": host},
            ) as resp:
                body = b""
                async for chunk in resp.aiter_bytes():
                    body += chunk
                    if len(body) > LIVE_RECORD_MAX_BYTES:
                        raise RecordSourceUnavailable("record_source_response_too_large")
                return resp.status_code, body


def build_record_transport() -> RecordTransport:
    from app.config import get_settings  # noqa: PLC0415

    if get_settings().integration_mode == "live":
        return HttpRecordTransport()
    return MockRecordTransport()


def parse_record_response(body: bytes) -> list[dict[str, Any]]:
    """A JSON array of objects → records; anything else is the endpoint's fault, said so."""
    import json  # noqa: PLC0415

    try:
        data = json.loads(body.decode("utf-8") or "null")
    except (ValueError, UnicodeDecodeError) as exc:
        raise RecordSourceUnavailable("record_source_not_json") from exc
    if isinstance(data, dict) and isinstance(data.get("records"), list):
        data = data["records"]
    if not isinstance(data, list):
        raise RecordSourceUnavailable("record_source_not_an_array")
    out: list[dict[str, Any]] = []
    for item in data:
        if isinstance(item, dict):
            out.append({str(k): v for k, v in item.items()})
        if len(out) > AMBIGUOUS_CANDIDATE_CAP:
            break
    return out


async def query_live_records(
    *,
    url: str,
    query: str,
    api_key: str | None,
    allowlist: frozenset[str],
    transport: RecordTransport,
) -> list[dict[str, Any]]:
    """One lookup against the owner's endpoint through the egress seam."""
    from urllib.parse import quote  # noqa: PLC0415

    from app.connections import sandbox  # noqa: PLC0415

    if allowlist:
        # Host membership always; the public-address resolution only where a real
        # connect follows (live) — in mock the transport never touches the network.
        from urllib.parse import urlparse  # noqa: PLC0415

        from app.config import live_mode  # noqa: PLC0415

        if (urlparse(url).hostname or "") not in allowlist:
            raise RecordSourceUnavailable("record_source_egress_refused")
        if live_mode() and not sandbox.is_egress_allowed(url, allowlist):
            raise RecordSourceUnavailable("record_source_egress_refused")
    separator = "&" if "?" in url else "?"
    target = f"{url}{separator}q={quote(query, safe='')}"
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        status, body = await transport.get(target, headers=headers)
    except RecordSourceUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 — transport detail must not leak
        raise RecordSourceUnavailable(f"record_source_transport_{type(exc).__name__}") from exc
    if status >= 400:
        raise RecordSourceUnavailable(f"record_source_http_{status}")
    return parse_record_response(body)


class LiveRecordBackend:
    """``RecordBackend`` over the owner's own lookup endpoint (provider ``api``)."""

    def __init__(self, transport: RecordTransport | None = None) -> None:
        self._transport = transport if transport is not None else build_record_transport()

    async def lookup(self, ctx: ToolContext, query: str) -> list[dict[str, Any]]:
        if ctx.session is None:
            raise RecordSourceUnavailable("record_source_session_unavailable")
        from app.connections.secrets import get_secret_store  # noqa: PLC0415
        from app.models.connection import InstanceConnection  # noqa: PLC0415

        row = (
            await ctx.session.execute(
                select(InstanceConnection.non_secret_config, InstanceConnection.secret_ref).where(
                    InstanceConnection.admin_id == ctx.admin_id,
                    InstanceConnection.instance_id == ctx.instance_id,
                    InstanceConnection.connection_type == "record_source",
                    InstanceConnection.status != "revoked",
                )
            )
        ).one_or_none()
        if row is None:
            raise RecordSourceUnavailable("record_source_not_configured")
        config = dict(row[0]) if isinstance(row[0], dict) else {}
        url = str(config.get("url") or "")
        if not url:
            raise RecordSourceUnavailable("record_source_url_not_configured")
        api_key: str | None = None
        if row[1]:
            material = await get_secret_store().get(row[1]) or {}
            api_key = material.get("api_key") or None
        raw_allowlist = config.get("egressAllowlist")
        hosts = raw_allowlist if isinstance(raw_allowlist, list) else []
        if not query.strip():
            return []
        return await query_live_records(
            url=url,
            query=query.strip(),
            api_key=api_key,
            allowlist=frozenset(str(h) for h in hosts if str(h)),
            transport=self._transport,
        )
