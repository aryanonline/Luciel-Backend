"""CRM push backends — the effect layer behind ``push_to_crm`` (§3.3.6).

Until this module, BOTH native CRM providers resolved to the raising stub: the
OAuth connect worked, the tool was selectable, and every actual push failed.
This implements the owner-scoped effect layer:

  * ``hubspot``        — real contact upsert against the HubSpot CRM v3 API.
  * ``custom_webhook`` — POST the captured-lead facts to the admin's registered
    endpoint through the SAME sandbox envelope as the BYO-webhook tool (SSRF
    allowlist, hard timeout, subprocess isolation, §3.8.6).
  * ``salesforce``     — honestly unavailable (registry ``unavailable_for``),
    so the choice renders "not available yet" instead of a dead connect button.

THE MAINTENANCE SEMANTICS (owner-reconfirmed): a push is an idempotent upsert
that keeps the SAME lead's record current over the lead's lifetime — never a
duplicate, never two-way sync. Resolution order per push:

  1. ``crm_lead_links`` row for (provider, lead_key) → PATCH that exact record.
     A 404 (record deleted inside the CRM) falls through and the link HEALS.
  2. Search the CRM by the stable lead key (email) → found: PATCH + write link.
  3. Create → write link.

The link table is what makes "the same lead" literally the same record even if
the lead's email later changes — search-by-email alone re-creates on change.

Live/mock seam: ``build_crm_api()`` mirrors ``build_calendar_api`` — the mock is
the TRANSPORT only (an in-memory contact store), so the upsert/link/heal
discipline under test is the production code path, not a simulation of it.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import select

from app.config import live_mode
from app.models.crm_link import CrmLeadLink
from app.tools.contract import ToolContext

logger = logging.getLogger(__name__)

HUBSPOT_BASE = "https://api.hubapi.com"


class CrmUnavailable(RuntimeError):
    """The CRM cannot be reached/authorized right now — surfaces as a structured
    ``failed`` (never ``empty``: "no such lead" would be a false claim)."""


@dataclass
class CrmRecord:
    record_id: str


class CrmApi(Protocol):
    """Provider transport: the four primitive calls the upsert discipline needs."""

    async def find_by_key(self, token: str, lead_key: str) -> CrmRecord | None: ...

    async def get(self, token: str, record_id: str) -> CrmRecord | None: ...

    async def create(self, token: str, lead_key: str, fields: dict[str, Any]) -> CrmRecord: ...

    async def update(
        self, token: str, record_id: str, lead_key: str, fields: dict[str, Any]
    ) -> CrmRecord | None: ...


class InMemoryCrmApi:
    """Deterministic in-process CRM for mock/CI — transport only (module note)."""

    def __init__(self) -> None:
        # record_id -> {"lead_key": ..., "fields": {...}}
        self.records: dict[str, dict[str, Any]] = {}

    async def find_by_key(self, token: str, lead_key: str) -> CrmRecord | None:
        for record_id, rec in self.records.items():
            if rec["lead_key"] == lead_key:
                return CrmRecord(record_id=record_id)
        return None

    async def get(self, token: str, record_id: str) -> CrmRecord | None:
        return CrmRecord(record_id=record_id) if record_id in self.records else None

    async def create(self, token: str, lead_key: str, fields: dict[str, Any]) -> CrmRecord:
        record_id = f"mock-{uuid.uuid4().hex[:10]}"
        self.records[record_id] = {"lead_key": lead_key, "fields": dict(fields)}
        return CrmRecord(record_id=record_id)

    async def update(
        self, token: str, record_id: str, lead_key: str, fields: dict[str, Any]
    ) -> CrmRecord | None:
        rec = self.records.get(record_id)
        if rec is None:
            return None
        rec["fields"].update(fields)
        rec["lead_key"] = lead_key
        return CrmRecord(record_id=record_id)


class HubSpotCrmApi:
    """Real HubSpot CRM v3 contacts transport (httpx; lazy import, live only)."""

    def __init__(self, *, transport_factory: Any = None) -> None:
        self._transport_factory = transport_factory or self._default_transport

    @staticmethod
    def _default_transport() -> Any:
        import httpx  # noqa: PLC0415 — live-path-only import

        return httpx.AsyncClient(timeout=15.0, base_url=HUBSPOT_BASE)

    @staticmethod
    def _properties(lead_key: str, fields: dict[str, Any]) -> dict[str, str]:
        # The stable key IS the email property; captured facts ride alongside.
        # Values are stringified — HubSpot property values are strings on write.
        return {"email": lead_key, **{str(k): str(v) for k, v in fields.items()}}

    async def find_by_key(self, token: str, lead_key: str) -> CrmRecord | None:
        async with self._transport_factory() as client:
            resp = await client.post(
                "/crm/v3/objects/contacts/search",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "filterGroups": [
                        {
                            "filters": [
                                {"propertyName": "email", "operator": "EQ", "value": lead_key}
                            ]
                        }
                    ],
                    "limit": 1,
                },
            )
        if resp.status_code != 200:
            raise CrmUnavailable(f"hubspot_search_http_{resp.status_code}")
        results = resp.json().get("results") or []
        if not results:
            return None
        return CrmRecord(record_id=str(results[0]["id"]))

    async def get(self, token: str, record_id: str) -> CrmRecord | None:
        async with self._transport_factory() as client:
            resp = await client.get(
                f"/crm/v3/objects/contacts/{record_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise CrmUnavailable(f"hubspot_get_http_{resp.status_code}")
        return CrmRecord(record_id=record_id)

    async def create(self, token: str, lead_key: str, fields: dict[str, Any]) -> CrmRecord:
        async with self._transport_factory() as client:
            resp = await client.post(
                "/crm/v3/objects/contacts",
                headers={"Authorization": f"Bearer {token}"},
                json={"properties": self._properties(lead_key, fields)},
            )
        if resp.status_code not in (200, 201):
            raise CrmUnavailable(f"hubspot_create_http_{resp.status_code}")
        return CrmRecord(record_id=str(resp.json()["id"]))

    async def update(
        self, token: str, record_id: str, lead_key: str, fields: dict[str, Any]
    ) -> CrmRecord | None:
        async with self._transport_factory() as client:
            resp = await client.patch(
                f"/crm/v3/objects/contacts/{record_id}",
                headers={"Authorization": f"Bearer {token}"},
                json={"properties": self._properties(lead_key, fields)},
            )
        if resp.status_code == 404:
            return None  # deleted inside the CRM — caller heals the link
        if resp.status_code != 200:
            raise CrmUnavailable(f"hubspot_update_http_{resp.status_code}")
        return CrmRecord(record_id=record_id)


_mock_api: InMemoryCrmApi | None = None


def build_crm_api() -> CrmApi:
    """Live → HubSpot transport; every other mode → the in-memory store (kept as a
    process-global so a test's second push sees the first push's record — the
    idempotency discipline is only provable against a persistent fake)."""
    global _mock_api
    if live_mode():
        return HubSpotCrmApi()
    if _mock_api is None:
        _mock_api = InMemoryCrmApi()
    return _mock_api


class HubSpotCrmBackend:
    """``CrmBackend`` over the tenant's connected HubSpot account."""

    def __init__(self, api: CrmApi | None = None) -> None:
        self._api = api if api is not None else build_crm_api()

    async def upsert_lead(
        self, ctx: ToolContext, lead_key: str, fields: dict[str, Any]
    ) -> dict[str, Any] | None:
        if ctx.session is None:
            raise CrmUnavailable("crm_session_unavailable")
        from app.connections.refresh import ensure_fresh_access_token  # noqa: PLC0415

        token = await ensure_fresh_access_token(
            ctx.session, ctx.admin_id, ctx.instance_id, connection_type="crm"
        )
        if not token:
            raise CrmUnavailable("crm_token_unavailable")

        link = (
            await ctx.session.execute(
                select(CrmLeadLink).where(
                    CrmLeadLink.admin_id == ctx.admin_id,
                    CrmLeadLink.instance_id == ctx.instance_id,
                    CrmLeadLink.provider == "hubspot",
                    CrmLeadLink.lead_key == lead_key,
                )
            )
        ).scalar_one_or_none()

        # 1. Linked record → update IN PLACE (the maintenance semantics).
        if link is not None:
            updated = await self._api.update(token, link.crm_record_id, lead_key, fields)
            if updated is not None:
                link.last_pushed_at = datetime.now(UTC)
                await self._audit(ctx, "updated", updated.record_id)
                return {"crm_record_id": updated.record_id, "action": "updated"}
            # Record deleted inside the CRM: fall through and HEAL the link.
            logger.info("crm_link_heal provider=hubspot")

        # 2. Search by the stable key → update + (re)link.
        found = await self._api.find_by_key(token, lead_key)
        if found is not None:
            updated = await self._api.update(token, found.record_id, lead_key, fields)
            record_id = (updated or found).record_id
            await self._write_link(ctx, link, lead_key, record_id)
            await self._audit(ctx, "updated", record_id)
            return {"crm_record_id": record_id, "action": "updated"}

        # 3. Create + link.
        created = await self._api.create(token, lead_key, fields)
        await self._write_link(ctx, link, lead_key, created.record_id)
        await self._audit(ctx, "created", created.record_id)
        return {"crm_record_id": created.record_id, "action": "created"}

    @staticmethod
    async def _audit(ctx: ToolContext, action: str, record_id: str) -> None:
        from app.audit import log as audit  # noqa: PLC0415
        from app.models.audit import ActorType  # noqa: PLC0415

        # Provider + action + record id — never the lead's key/PII (§3.4.11).
        await audit.emit(
            ctx.session,
            admin_id=ctx.admin_id,
            instance_id=ctx.instance_id,
            actor_type=ActorType.luciel_runtime,
            event_type="crm_record_upserted",
            payload={"provider": "hubspot", "action": action, "crm_record_id": record_id},
        )

    @staticmethod
    async def _write_link(
        ctx: ToolContext, existing: CrmLeadLink | None, lead_key: str, record_id: str
    ) -> None:
        if existing is not None:
            existing.crm_record_id = record_id
            existing.last_pushed_at = datetime.now(UTC)
            return
        ctx.session.add(
            CrmLeadLink(
                admin_id=ctx.admin_id,
                instance_id=ctx.instance_id,
                provider="hubspot",
                lead_key=lead_key,
                crm_record_id=record_id,
                last_pushed_at=datetime.now(UTC),
            )
        )


class WebhookCrmBackend:
    """``CrmBackend`` over the admin's registered endpoint (provider
    ``custom_webhook``) — the same sandbox envelope as the BYO-webhook tool
    (§3.8.6). The payload carries the STABLE lead key so the admin's endpoint
    can dedupe (§3.3.6: for the custom-webhook path, upsert-in-place is the
    endpoint's half of the contract; ours is never omitting the key)."""

    async def upsert_lead(
        self, ctx: ToolContext, lead_key: str, fields: dict[str, Any]
    ) -> dict[str, Any] | None:
        from app.connections import sandbox  # noqa: PLC0415
        from app.connections.secrets import get_secret_store  # noqa: PLC0415
        from app.models.connection import InstanceConnection  # noqa: PLC0415

        if ctx.session is None:
            raise CrmUnavailable("crm_session_unavailable")
        row = (
            await ctx.session.execute(
                select(InstanceConnection.non_secret_config, InstanceConnection.secret_ref).where(
                    InstanceConnection.admin_id == ctx.admin_id,
                    InstanceConnection.instance_id == ctx.instance_id,
                    InstanceConnection.connection_type == "crm",
                )
            )
        ).one_or_none()
        if row is None:
            raise CrmUnavailable("crm_webhook_not_configured")
        config = dict(row[0]) if isinstance(row[0], dict) else {}
        url = str(config.get("url") or "")
        if not url:
            raise CrmUnavailable("crm_webhook_url_not_configured")
        auth_header: str | None = None
        if row[1]:
            material = await get_secret_store().get(row[1]) or {}
            auth_header = material.get("auth_header") or None
        raw_allowlist = config.get("egressAllowlist")
        hosts = raw_allowlist if isinstance(raw_allowlist, list) else []
        result = await sandbox.run_byo_webhook(
            url=url,
            payload={"leadKey": lead_key, "fields": dict(fields)},
            auth_header=auth_header,
            allowlist=frozenset(str(h) for h in hosts if str(h)),
            breaker=sandbox.CircuitBreaker(),
            executor=sandbox.SubprocessWebhookExecutor(),
            now=time.monotonic(),
            context=ctx,
        )
        if result.outcome.value != "ok":
            raise CrmUnavailable(f"crm_webhook_{result.reason or result.outcome.value}")
        # A webhook cannot distinguish create from update; the stable key lets
        # the endpoint dedupe, and we report what actually happened: delivery.
        return {"crm_record_id": lead_key, "action": "delivered"}
