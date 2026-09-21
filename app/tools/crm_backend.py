"""CRM push backends — the effect layer behind ``push_to_crm`` (§3.3.6).

Until this module, BOTH native CRM providers resolved to the raising stub: the
OAuth connect worked, the tool was selectable, and every actual push failed.
This implements the owner-scoped effect layer:

  * ``hubspot``        — real contact upsert against the HubSpot CRM v3 API.
  * ``custom_webhook`` — POST the captured-lead facts to the admin's registered
    endpoint through the SAME sandbox envelope as the BYO-webhook tool (SSRF
    allowlist, hard timeout, subprocess isolation, §3.8.6).
  * ``salesforce``     — real Lead upsert against the TENANT's own Salesforce
    org (``instance_url`` captured at the OAuth exchange), same link/heal
    discipline with ``provider="salesforce"`` (audit round 3, Phase 6.5 —
    owner decision 2026-08-23).

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

    async def create(
        self,
        token: str,
        lead_key: str,
        fields: dict[str, Any],
        notes: dict[str, str] | None = None,
    ) -> CrmRecord: ...

    async def update(
        self,
        token: str,
        record_id: str,
        lead_key: str,
        fields: dict[str, Any],
        notes: dict[str, str] | None = None,
    ) -> CrmRecord | None: ...


class InMemoryCrmApi:
    """Deterministic in-process CRM for mock/CI — transport only (module note).

    Round 7 WP-3c item 15: records are keyed by ``(token, record_id)`` exactly like
    ``InMemoryCalendarApi._store`` — the token IS the tenant's CRM account, so two
    mock tenants have two record stores. The old single dict let tenant B's push
    find and PATCH tenant A's record by email, a tenant-crossing the live APIs can
    never do (each token only reaches its own portal/org)."""

    def __init__(self) -> None:
        # (token, record_id) -> {"lead_key": ..., "fields": {...}, "notes": {...}}
        self._records: dict[tuple[str, str], dict[str, Any]] = {}

    @property
    def records(self) -> dict[str, dict[str, Any]]:
        """Every record across every token, by record id (ids are unique per store).

        Kept for the tests that assert "one record, never a duplicate" without caring
        which token wrote it; ``records_for(token)`` is the per-tenant view."""
        return {record_id: rec for (_token, record_id), rec in self._records.items()}

    def records_for(self, token: str) -> dict[str, dict[str, Any]]:
        return {rid: rec for (tok, rid), rec in self._records.items() if tok == token}

    def forget(self, record_id: str) -> None:
        """Delete a record out of band (a test's "deleted inside the CRM")."""
        for key in [k for k in self._records if k[1] == record_id]:
            del self._records[key]

    async def find_by_key(self, token: str, lead_key: str) -> CrmRecord | None:
        for (tok, record_id), rec in self._records.items():
            if tok == token and rec["lead_key"] == lead_key:
                return CrmRecord(record_id=record_id)
        return None

    async def get(self, token: str, record_id: str) -> CrmRecord | None:
        return CrmRecord(record_id=record_id) if (token, record_id) in self._records else None

    async def create(
        self,
        token: str,
        lead_key: str,
        fields: dict[str, Any],
        notes: dict[str, str] | None = None,
    ) -> CrmRecord:
        record_id = f"mock-{uuid.uuid4().hex[:10]}"
        self._records[(token, record_id)] = {
            "lead_key": lead_key,
            "fields": dict(fields),
            "notes": dict(notes or {}),
        }
        return CrmRecord(record_id=record_id)

    async def update(
        self,
        token: str,
        record_id: str,
        lead_key: str,
        fields: dict[str, Any],
        notes: dict[str, str] | None = None,
    ) -> CrmRecord | None:
        rec = self._records.get((token, record_id))
        if rec is None:
            return None
        del lead_key  # updates never rewrite the key (item 1) — mirror the live APIs
        rec["fields"].update(fields)
        if notes:
            rec["notes"] = dict(notes)
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
    def _mapped(fields: dict[str, Any], notes: dict[str, str] | None = None) -> dict[str, str]:
        """Captured facts → HubSpot's STANDARD contact properties (2026-09-05 audit F128).

        The old push sent our field names verbatim (``name``, ``intent``, …); HubSpot
        rejects unknown properties with a 400, so every push carrying a fact other
        than the email failed. ``name`` splits into firstname/lastname, ``phone`` and
        ``company`` map 1:1, and everything else — intent, lead score, outcome —
        is Luciel-maintained free text on the standard ``message`` property: the
        record's CURRENT facts (``notes``) with this push's changes on top, never
        only the delta, so an outcome push cannot wipe the intent (F130)."""
        working = {str(k): str(v) for k, v in fields.items() if v is not None and str(v) != ""}
        props: dict[str, str] = {}
        name = working.pop("name", "").strip()
        if name:
            first, _, last = name.partition(" ")
            props["firstname"] = first
            if last.strip():
                props["lastname"] = last.strip()
        phone = working.pop("phone", "")
        if phone:
            props["phone"] = phone
        company = working.pop("business", "") or working.pop("company", "")
        if company:
            props["company"] = company
        moved_email = working.pop("email", "")
        if moved_email:
            props["email"] = moved_email
        if working or notes:
            merged = {**(notes or {}), **working}
            props["message"] = "\n".join(f"{k}: {v}" for k, v in sorted(merged.items()))
        return props

    @staticmethod
    def _properties(
        lead_key: str, fields: dict[str, Any], notes: dict[str, str] | None = None
    ) -> dict[str, str]:
        # The stable key IS the email property; captured facts ride alongside.
        # Values are stringified — HubSpot property values are strings on write.
        return {"email": lead_key, **HubSpotCrmApi._mapped(fields, notes)}

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

    async def create(
        self,
        token: str,
        lead_key: str,
        fields: dict[str, Any],
        notes: dict[str, str] | None = None,
    ) -> CrmRecord:
        async with self._transport_factory() as client:
            resp = await client.post(
                "/crm/v3/objects/contacts",
                headers={"Authorization": f"Bearer {token}"},
                json={"properties": self._properties(lead_key, fields, notes)},
            )
        if resp.status_code not in (200, 201):
            raise CrmUnavailable(f"hubspot_create_http_{resp.status_code}")
        return CrmRecord(record_id=str(resp.json()["id"]))

    async def update(
        self,
        token: str,
        record_id: str,
        lead_key: str,
        fields: dict[str, Any],
        notes: dict[str, str] | None = None,
    ) -> CrmRecord | None:
        # lead_key deliberately unused on update (item 1): re-sending the email
        # property on every PATCH reverted a tenant's in-CRM correction; the
        # record id already addresses the contact, so only changed facts ride.
        del lead_key
        props = self._mapped(fields, notes)
        if not props:
            return CrmRecord(record_id=record_id)
        async with self._transport_factory() as client:
            resp = await client.patch(
                f"/crm/v3/objects/contacts/{record_id}",
                headers={"Authorization": f"Bearer {token}"},
                json={"properties": props},
            )
        if resp.status_code == 404:
            return None  # deleted inside the CRM — caller heals the link
        if resp.status_code != 200:
            raise CrmUnavailable(f"hubspot_update_http_{resp.status_code}")
        return CrmRecord(record_id=record_id)


SALESFORCE_API_VERSION = "v59.0"


def _soql_quote(value: str) -> str:
    """Escape a value for interpolation inside SOQL single quotes.

    The lead key is customer-typed text; unescaped quotes/backslashes would be a
    SOQL injection into the TENANT's own org."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


class SalesforceCrmApi:
    """Real Salesforce Lead transport against the TENANT's org (Phase 6.5).

    ``instance_url`` is the org API base captured by the OAuth exchange
    (``token_fields=("instance_url",)`` on the registry spec). Salesforce
    requires ``LastName`` and ``Company`` on Lead create — captured facts map
    onto them with honest "Unknown" fallbacks, stated in the frontend's
    field-mapping disclosure, never invented values."""

    def __init__(self, *, instance_url: str, transport_factory: Any = None) -> None:
        self._base = instance_url.rstrip("/")
        self._transport_factory = transport_factory or self._default_transport

    def _default_transport(self) -> Any:
        import httpx  # noqa: PLC0415 — live-path-only import

        return httpx.AsyncClient(timeout=15.0, base_url=self._base)

    @staticmethod
    def _description(working: dict[str, str], notes: dict[str, str] | None) -> str | None:
        # F130: Description is Luciel-maintained, and it holds the record's CURRENT
        # facts with this push's changes on top — an outcome-only push used to
        # overwrite it with the single line "outcome: converted", erasing the intent.
        merged = {**(notes or {}), **working}
        if not merged:
            return None
        return "\n".join(f"{k}: {v}" for k, v in sorted(merged.items()))

    @staticmethod
    def _create_payload(
        lead_key: str, fields: dict[str, Any], notes: dict[str, str] | None = None
    ) -> dict[str, str]:
        """Captured-lead facts → a NEW Salesforce Lead (the disclosed mapping).

        Salesforce requires LastName and Company on create, so the honest
        "Unknown" placeholder appears HERE and only here — never on update."""
        working = {str(k): str(v) for k, v in fields.items()}
        payload = {
            "Email": lead_key,
            "LastName": working.pop("name", "") or "Unknown",
            "Company": working.pop("business", "") or working.pop("company", "") or "Unknown",
        }
        phone = working.pop("phone", "")
        if phone:
            payload["Phone"] = phone
        description = SalesforceCrmApi._description(working, notes)
        if description:
            payload["Description"] = description
        return payload

    @staticmethod
    def _update_payload(
        fields: dict[str, Any], notes: dict[str, str] | None = None
    ) -> dict[str, str]:
        """ONLY the facts this push changed → PATCH fields (round 5A, item 1).

        The old shared payload builder re-sent Email/LastName/Company on every
        update with the "Unknown" fallback, so a routine intent-only push
        OVERWROTE whatever the tenant had typed into their own org — the exact
        opposite of "keeps each lead's record current". An update never carries
        Email (the tenant's in-CRM correction stands; our link row pins the
        record id, so the key is not needed to address it) and never fabricates
        a fallback for a field that did not change. Description carries the
        latest non-mapped facts and is Luciel-maintained, as the mapping
        disclosure states."""
        working = {str(k): str(v) for k, v in fields.items()}
        payload: dict[str, str] = {}
        # "email" appears in an update's fields ONLY when the identity moved
        # (the link's last-pushed key differs) — the one legitimate Email write.
        moved_email = working.pop("email", "")
        if moved_email:
            payload["Email"] = moved_email
        name = working.pop("name", "")
        if name:
            payload["LastName"] = name
        company = working.pop("business", "") or working.pop("company", "")
        if company:
            payload["Company"] = company
        phone = working.pop("phone", "")
        if phone:
            payload["Phone"] = phone
        description = SalesforceCrmApi._description(working, notes)
        if description:
            payload["Description"] = description
        return payload

    async def find_by_key(self, token: str, lead_key: str) -> CrmRecord | None:
        # SOQL has no parameter binding over the REST query endpoint; the value
        # is escaped by _soql_quote (quotes + backslashes), which is the
        # provider-documented mitigation. Not SQL against our own database.
        soql = f"SELECT Id FROM Lead WHERE Email = '{_soql_quote(lead_key)}' LIMIT 1"  # noqa: S608  # nosec B608
        async with self._transport_factory() as client:
            resp = await client.get(
                f"/services/data/{SALESFORCE_API_VERSION}/query",
                headers={"Authorization": f"Bearer {token}"},
                params={"q": soql},
            )
        if resp.status_code != 200:
            raise CrmUnavailable(f"salesforce_search_http_{resp.status_code}")
        records = resp.json().get("records") or []
        if not records:
            return None
        return CrmRecord(record_id=str(records[0]["Id"]))

    async def get(self, token: str, record_id: str) -> CrmRecord | None:
        async with self._transport_factory() as client:
            resp = await client.get(
                f"/services/data/{SALESFORCE_API_VERSION}/sobjects/Lead/{record_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise CrmUnavailable(f"salesforce_get_http_{resp.status_code}")
        return CrmRecord(record_id=record_id)

    async def create(
        self,
        token: str,
        lead_key: str,
        fields: dict[str, Any],
        notes: dict[str, str] | None = None,
    ) -> CrmRecord:
        async with self._transport_factory() as client:
            resp = await client.post(
                f"/services/data/{SALESFORCE_API_VERSION}/sobjects/Lead",
                headers={"Authorization": f"Bearer {token}"},
                json=self._create_payload(lead_key, fields, notes),
            )
        if resp.status_code not in (200, 201):
            raise CrmUnavailable(f"salesforce_create_http_{resp.status_code}")
        return CrmRecord(record_id=str(resp.json()["id"]))

    async def update(
        self,
        token: str,
        record_id: str,
        lead_key: str,
        fields: dict[str, Any],
        notes: dict[str, str] | None = None,
    ) -> CrmRecord | None:
        # lead_key deliberately unused on update (item 1): the link row already
        # pins the record id, and re-sending Email reverted tenant corrections.
        del lead_key
        payload = self._update_payload(fields, notes)
        if not payload:
            return CrmRecord(record_id=record_id)
        async with self._transport_factory() as client:
            resp = await client.patch(
                f"/services/data/{SALESFORCE_API_VERSION}/sobjects/Lead/{record_id}",
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
            )
        if resp.status_code == 404:
            return None  # deleted inside the CRM — caller heals the link
        if resp.status_code not in (200, 204):
            raise CrmUnavailable(f"salesforce_update_http_{resp.status_code}")
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


async def resolve_owning_lead(ctx: ToolContext, lead_key: str) -> Any:
    """The conversation's resolved lead, proven to OWN ``lead_key`` (round 7 WP-3c item 8).

    A push with NO resolved lead used to skip the F095 key-ownership guard entirely,
    so an anonymous turn could create a CRM contact under any key the model chose.
    Now the guard is unconditional: no resolved lead → ``no_resolved_lead``; a key
    the lead does not own → ``crm_lead_key_not_owned``. Ownership means the lead's
    OWN confirmed columns (email / contact_identifier / phone) — a merely mentioned
    address (item 2) is never a key and never owned."""
    from app.models.lead import Lead  # noqa: PLC0415
    from app.runtime.lead_capture import normalize_identifier  # noqa: PLC0415

    if ctx.session is None:
        raise CrmUnavailable("crm_session_unavailable")
    if ctx.resolved_lead_id is None:
        raise CrmUnavailable("no_resolved_lead")
    lead_row = (
        await ctx.session.execute(
            select(Lead).where(Lead.admin_id == ctx.admin_id, Lead.id == ctx.resolved_lead_id)
        )
    ).scalar_one_or_none()
    if lead_row is None:
        raise CrmUnavailable("no_resolved_lead")
    owned_keys = {
        normalize_identifier(k)
        for k in (lead_row.email, lead_row.contact_identifier, lead_row.phone)
        if k
    }
    if normalize_identifier(lead_key) not in owned_keys:
        # 2026-09-05 audit F095: a key the conversation's lead does not own is a
        # stranger's record — the push is refused outright instead of creating or
        # patching someone else's contact.
        raise CrmUnavailable("crm_lead_key_not_owned")
    return lead_row


def lead_notes(lead_row: Any) -> dict[str, str]:
    """The record's current Luciel-maintained facts, merged into every push (F130)."""
    return {
        k: v
        for k, v in (
            ("intent", lead_row.intent or ""),
            (
                "lead_score",
                f"{lead_row.lead_score:g}" if lead_row.lead_score is not None else "",
            ),
            ("outcome", lead_row.outcome or ""),
        )
        if v
    }


class _NativeCrmBackend:
    """The shared upsert/link/heal discipline over any native ``CrmApi``.

    Subclasses pin ``provider`` (the ``crm_lead_links`` namespace + audit label)
    and how the transport is built. The discipline itself — linked-record PATCH,
    heal-on-404, search-by-key relink, create+link — is provider-agnostic."""

    provider: str = ""

    def __init__(self, api: CrmApi | None = None) -> None:
        self._api = api

    async def _api_for(self, ctx: ToolContext) -> CrmApi:
        return self._api if self._api is not None else build_crm_api()

    async def upsert_lead(
        self, ctx: ToolContext, lead_key: str, fields: dict[str, Any]
    ) -> dict[str, Any] | None:
        try:
            return await self._upsert_lead_inner(ctx, lead_key, fields)
        except CrmUnavailable as exc:
            # Round 6 WP-C: a 401/403 from the CRM is a dead grant, not a bad record.
            # The connection row flips to error through the sweep's own transition
            # (audit + one attention email) the moment it happens, instead of reading
            # Connected until the next sweep — and the push still fails honestly.
            if ctx.session is not None:
                from app.connections import health  # noqa: PLC0415 — import cycle

                await health.flag_if_provider_auth_failure(
                    ctx.session,
                    admin_id=ctx.admin_id,
                    instance_id=ctx.instance_id,
                    connection_type="crm",
                    reason=str(exc),
                )
            raise

    async def _upsert_lead_inner(
        self, ctx: ToolContext, lead_key: str, fields: dict[str, Any]
    ) -> dict[str, Any] | None:
        if ctx.session is None:
            raise CrmUnavailable("crm_session_unavailable")
        from app.connections.refresh import ensure_fresh_access_token  # noqa: PLC0415

        if "@" not in lead_key:
            # The native CRMs dedupe on the email property; a phone number there
            # would create a garbage record the tenant then cleans up.
            raise CrmUnavailable("crm_key_not_email")
        token = await ensure_fresh_access_token(
            ctx.session, ctx.admin_id, ctx.instance_id, connection_type="crm"
        )
        if not token:
            raise CrmUnavailable("crm_token_unavailable")
        api = await self._api_for(ctx)

        # Round 5A item 9 — resolve the link by LEAD first, key second. The
        # key-ownership guard is what makes lead_id trustworthy: a model-proposed
        # push can carry any email, and without the guard a stranger's key would
        # PATCH the current lead's record with someone else's identity.
        lead_row = await resolve_owning_lead(ctx, lead_key)
        verified_lead_id: uuid.UUID = lead_row.id
        # The record's current Luciel-maintained facts ride every push so
        # the CRM's free-text field is merged, never overwritten (F130).
        notes: dict[str, str] | None = lead_notes(lead_row)

        link: CrmLeadLink | None = (
            await ctx.session.execute(
                select(CrmLeadLink).where(
                    CrmLeadLink.admin_id == ctx.admin_id,
                    CrmLeadLink.instance_id == ctx.instance_id,
                    CrmLeadLink.provider == self.provider,
                    CrmLeadLink.lead_id == verified_lead_id,
                )
            )
        ).scalar_one_or_none()
        if link is None:
            link = (
                await ctx.session.execute(
                    select(CrmLeadLink)
                    .where(
                        CrmLeadLink.admin_id == ctx.admin_id,
                        CrmLeadLink.instance_id == ctx.instance_id,
                        CrmLeadLink.provider == self.provider,
                        CrmLeadLink.lead_key == lead_key,
                    )
                    .order_by(CrmLeadLink.last_pushed_at.asc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if link is not None and link.lead_id is not None and link.lead_id != verified_lead_id:
                # That key's link belongs to ANOTHER lead — never adopt it.
                link = None

        # The identity field moved (email change since the last successful push):
        # tell the CRM, exactly once — link.lead_key refreshes only on success,
        # so the discrepancy persists until the CRM actually heard about it.
        if link is not None and link.lead_key != lead_key:
            fields = {**fields, "email": lead_key}

        # 1. Linked record → update IN PLACE (the maintenance semantics).
        if link is not None:
            updated = await api.update(token, link.crm_record_id, lead_key, fields, notes)
            if updated is not None:
                await self._write_link(ctx, link, lead_key, updated.record_id, verified_lead_id)
                await self._audit(ctx, "updated", updated.record_id)
                return {"crm_record_id": updated.record_id, "action": "updated"}
            # Record deleted inside the CRM: fall through and HEAL the link.
            logger.info("crm_link_heal provider=%s", self.provider)

        # 2. Search by the stable key → update + (re)link.
        found = await api.find_by_key(token, lead_key)
        if found is not None:
            updated = await api.update(token, found.record_id, lead_key, fields, notes)
            record_id = (updated or found).record_id
            await self._write_link(ctx, link, lead_key, record_id, verified_lead_id)
            await self._audit(ctx, "updated", record_id)
            return {"crm_record_id": record_id, "action": "updated"}

        # 3. Create + link.
        created = await api.create(token, lead_key, fields, notes)
        await self._write_link(ctx, link, lead_key, created.record_id, verified_lead_id)
        await self._audit(ctx, "created", created.record_id)
        return {"crm_record_id": created.record_id, "action": "created"}

    async def _audit(self, ctx: ToolContext, action: str, record_id: str) -> None:
        from app.audit import log as audit  # noqa: PLC0415
        from app.models.audit import ActorType  # noqa: PLC0415

        # Provider + action + record id — never the lead's key/PII (§3.4.11).
        await audit.emit(
            ctx.session,
            admin_id=ctx.admin_id,
            instance_id=ctx.instance_id,
            actor_type=ActorType.luciel_runtime,
            event_type="crm_record_upserted",
            payload={"provider": self.provider, "action": action, "crm_record_id": record_id},
        )

    async def _write_link(
        self,
        ctx: ToolContext,
        existing: CrmLeadLink | None,
        lead_key: str,
        record_id: str,
        lead_id: uuid.UUID | None,
    ) -> None:
        # Every successful touch refreshes the key (the "identity moved" signal
        # depends on it) and adopts the verified lead id onto legacy rows.
        if existing is not None:
            existing.crm_record_id = record_id
            existing.lead_key = lead_key
            if lead_id is not None:
                existing.lead_id = lead_id
            existing.last_pushed_at = datetime.now(UTC)
            return
        ctx.session.add(
            CrmLeadLink(
                admin_id=ctx.admin_id,
                instance_id=ctx.instance_id,
                provider=self.provider,
                lead_id=lead_id,
                lead_key=lead_key,
                crm_record_id=record_id,
                last_pushed_at=datetime.now(UTC),
            )
        )


async def delete_links_for_lead(session: Any, *, admin_id: uuid.UUID, lead: Any) -> int:
    """Delete every CRM link row belonging to this lead (round 5A item 2).

    Erase/prune deleted the Lead but left its ``crm_lead_links`` rows — including
    the person's email in plaintext — orphaned in our database after a
    right-to-erasure action. Matches by lead_id AND by the lead's identifiers
    (legacy rows without a lead_id). The EXTERNAL CRM record is deliberately not
    touched: §3.3.6 is one-way, and the UI/legal copy now states that the record
    inside the tenant's own CRM is theirs to delete there."""
    from sqlalchemy import delete as sa_delete  # noqa: PLC0415
    from sqlalchemy import or_  # noqa: PLC0415

    keys = [k for k in (lead.contact_identifier, lead.email) if k]
    cond = CrmLeadLink.lead_id == lead.id
    if keys:
        cond = or_(cond, CrmLeadLink.lead_key.in_(keys))
    result = await session.execute(
        sa_delete(CrmLeadLink).where(
            CrmLeadLink.admin_id == admin_id,
            CrmLeadLink.instance_id == lead.instance_id,
            cond,
        )
    )
    return int(result.rowcount or 0)


class HubSpotCrmBackend(_NativeCrmBackend):
    """``CrmBackend`` over the tenant's connected HubSpot account."""

    provider = "hubspot"


class SalesforceCrmBackend(_NativeCrmBackend):
    """``CrmBackend`` over the tenant's own Salesforce org (Phase 6.5).

    The transport is built PER CALL in live mode because its base URL is
    tenant state: the ``instance_url`` the OAuth exchange captured into the
    connection's secret material. Mock mode shares the in-memory store (the
    discipline under test stays the production code path)."""

    provider = "salesforce"

    async def _api_for(self, ctx: ToolContext) -> CrmApi:
        if self._api is not None:
            return self._api
        if not live_mode():
            return build_crm_api()
        from app.connections.secrets import get_secret_store, secret_ref_for  # noqa: PLC0415

        material = (
            await get_secret_store().get(secret_ref_for(ctx.admin_id, ctx.instance_id, "crm")) or {}
        )
        instance_url = str(material.get("instance_url") or "").strip()
        if not instance_url:
            # Round 6 WP-C: login.salesforce.com is the AUTH host, not an org's API
            # base — a push against it answered 401 forever while the row read
            # Connected. No org URL on file is its own honest reason.
            raise CrmUnavailable("salesforce_instance_url_missing")
        return SalesforceCrmApi(instance_url=instance_url)


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
        # Item 8 applies here too: the tenant's endpoint is still THEIR CRM, and a
        # key no resolved lead owns must never be posted to it.
        await resolve_owning_lead(ctx, lead_key)
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
