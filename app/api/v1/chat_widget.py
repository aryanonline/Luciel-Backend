"""/api/v1/chat-widget/* — the embeddable widget DATA PLANE (Architecture §1.2).

ZERO admin surface (Space Instructions §1, §6.3): this router has no session-cookie
auth and no admin dependency. A request authenticates by its embed key, which
resolves to exactly one tenant (§3.1.3) BEFORE a tenant subject exists — the
resolution runs on an untenanted session, then every tenant read/write runs inside
a tenant_session so the three-layer isolation holds (§3.7.2b).

Render directives (the widget renders what it is told):
  - paused  → empty render, no chat (Luciel paused/soft-deleted, §3.6.2). No LLM.
  - at_cap  → graceful no-LLM reply, message passed along (§3.4.1b).
  - active  → normal agentic turn.

AI-identity disclosure (§3.4.16) and the "Powered by VantageMind" chrome (§3.4.17)
are ALWAYS present on bootstrap — admin wording may shape the opening but cannot
remove the FACT of AI nor the platform attribution (frozen literals in widget.ts).

FLAGGED: the widget identifier is a WEAK identity (§3.4.9) — the widget session does
NOT inherit cross-session memory (sessions.can_carry_cross_session_memory == False
for 'widget'); the loop enforces this in memory.load_cross_session_summaries.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Query, Request, Response
from sqlalchemy import select

from app.api.errors import not_found, rate_limited
from app.audit import log as audit
from app.billing import metering
from app.channels import rate_limit
from app.channels.base import InboundRejected, InstanceContext
from app.channels.routing import resolve_widget
from app.db.session import tenant_session, untenanted_session
from app.models.audit import ActorType
from app.models.conversation import Session as ConversationSession
from app.models.conversation import Transcript
from app.models.instance import Instance, is_paused
from app.runtime import budget, session_close
from app.runtime.input_safety import _compose_greeting
from app.runtime.knowledge_retrieval import make_chunk_attributor, make_retriever
from app.runtime.orchestrator import AT_CAP_REPLY, Orchestrator, persist_turn
from app.runtime.provider_factory import build_llm_router
from app.runtime.sessions import effective_config, find_open_session, resolve_or_open_session
from app.schemas_api.widget import (
    WidgetBootstrapOut,
    WidgetBootstrapRequest,
    WidgetMessageOut,
    WidgetSendRequest,
    WidgetSendResult,
)
from app.tools.registry import make_tool_resolver

router = APIRouter(prefix="/api/v1/chat-widget", tags=["chat-widget"])

CHANNEL = "widget"


# AI-identity disclosure wording (§3.4.16). Admin-shaped in production via the
# personality pillar; the FACT of AI ("AI assistant") is non-removable. Delegates to
# the shared composer so the widget and the cross-channel backstop never drift into
# two different concatenation bugs again (see _compose_greeting docstring).
def _disclosure(business: str, assistant: str) -> str:
    return _compose_greeting(business_name=business, assistant_name=assistant)


async def _resolve_embed_key(embed_key: str) -> InstanceContext:
    async with untenanted_session() as s:
        try:
            return await resolve_widget(s, embed_key)
        except InboundRejected as exc:
            # Unresolvable embed key → 404 (never routed on a guess, §3.1.3).
            raise not_found("Unknown widget.") from exc


def _client_ip(request: Request) -> str:
    """The ALB-reported client address (uvicorn --forwarded-allow-ips = VPC CIDR).

    Trusted precisely because the launch line trusts X-Forwarded-For only from the
    VPC: uvicorn then resolves the client to the entry the ALB appended, never to a
    leftmost value the caller wrote. Behind no proxy (local), it is the peer address.
    """
    return request.client.host if request.client else "unknown"


async def _deny_widget(ctx: InstanceContext, reason: str | None) -> None:
    """Audit an admission denial (no raw PII) and answer the documented 429 (§3.1.5)."""
    async with tenant_session(ctx.admin_id) as s:
        await audit.emit(
            s,
            admin_id=ctx.admin_id,
            instance_id=ctx.instance_id,
            actor_type=ActorType.platform,
            event_type="widget_abuse_blocked",
            # No raw PII: channel + reason only (§3.1.5 / §3.4.11).
            payload={"channel": "widget", "reason": reason},
        )
    raise rate_limited("Too many messages — please slow down.", retry_after_seconds=3)


async def _widget_ip_gate(ctx: InstanceContext, request: Request, bucket: str) -> str:
    """Stage-one widget admission: per TRUSTED client IP per instance, before any DB work.

    The widget is a direct browser POST (no channel webhook), so it does not flow
    through channels.pipeline.ingest(); this is the widget's equivalent pre-budget
    gate. It MUST run before the budget gate and before any LLM call, so a flood can
    never drain a tenant's conversation budget or run up cost (denial-of-wallet).

    The key is the ALB-reported client IP — never a caller-supplied value. The
    previous gate keyed on ``body.sessionId``, so rotating a made-up id minted a
    fresh full bucket per request AND opened a new billed session each time
    (audit F046). Returns the client IP for the stage-two gate.
    """
    client_ip = _client_ip(request)
    decision = rate_limit.admit(bucket, f"{ctx.instance_id}:{client_ip}", str(ctx.admin_id))
    if not decision.allowed:
        await _deny_widget(ctx, decision.reason)
    return client_ip


def _widget_session_gate(
    ctx: InstanceContext, client_ip: str, existing_session_id: uuid.UUID | None
) -> rate_limit.RateLimitResult:
    """Stage-two widget admission, once the supplied session id has been checked.

    - A session id that resolved to an OPEN session of this tenant gets the per-session
      bucket (§3.1.5: burst 5, sustained 1 msg / 3 s) keyed on the server-side id.
    - Anything else is a NEW conversation: it must pass the per-IP new-session cap
      (opening a session is the billable event, §3.4.1b) and then shares one
      per-IP "new" bucket — a fabricated id buys no tokens.
    """
    store = rate_limit.get_store()
    if existing_session_id is not None:
        return store.check_source("widget", f"{existing_session_id}:{client_ip}")
    opened = store.check_source("widget_session_open", f"{ctx.instance_id}:{client_ip}")
    if not opened.allowed:
        return opened
    return store.check_source("widget", f"new:{ctx.instance_id}:{client_ip}")


def _names(inst: Instance) -> tuple[str, str]:
    """Luciel display name + assistant speaker name from the instance config.

    NOTE ON NAMING: despite the historical local variable name "business", the
    first element returned here is ``inst.name`` -- the LUCIEL's own configured
    name ("The one Luciel per account", see app.models.instance.Instance), NOT
    the business's name. It feeds the frozen wire field
    ``WidgetBootstrapOut.business_name`` (contract-tested to equal ``inst.name``,
    e.g. "Sarah Chen") and the widget header -- it must NEVER be fed into the
    AI-identity disclosure clause's business noun; see ``_disclosure_business_name``
    for that.

    When no assistantName is configured, the Luciel name itself is used
    VERBATIM as the speaker -- never suffixed with "'s assistant". That suffix is
    exactly what produced "...Assistant's assistant" when a business/Luciel name
    already ends in the word "Assistant" (e.g. "Aryan's Front-Desk Assistant").
    """
    business = inst.name or "this business"
    personality = inst.personality or {}
    assistant = str(personality.get("assistantName") or business)
    return business, assistant


def _disclosure_business_name(inst: Instance) -> str:
    """Resolve the BUSINESS name for the AI-identity disclosure clause (Sec 3.4.16).

    Bug fixed here: the disclosure previously named the LUCIEL (``inst.name``,
    e.g. "Aryan's Front-Desk Assistant") instead of the business, producing
    "I'm an AI assistant for Aryan's Front-Desk Assistant" -- the Luciel name must
    NEVER appear in this clause.

    Checked the Instance model and the Personality pillar for a usable SHORT
    business-name field: there isn't one. ``personality.businessContext``
    (app.runtime.personality.business_context_of) and the signup
    ``Instance.business_one_liner`` are both free-text up to 280 chars --
    descriptive one-liners meant for the system prompt, not short display
    names -- so neither is safe to drop into a one-line greeting. With no
    usable short name, this falls back to the generic "this business".
    """
    return "this business"


@router.post("/bootstrap", response_model=WidgetBootstrapOut)
async def bootstrap(body: WidgetBootstrapRequest, request: Request) -> WidgetBootstrapOut:
    """Resolve the host page's embed key → render directive + disclosure (§3.6.2/§3.4.16)."""
    ctx = await _resolve_embed_key(body.embed_key)
    # Unauthenticated and DB-backed: a page-view storm from one address is bounded
    # at the edge before it reaches the tenant's tables (§3.1.5).
    await _widget_ip_gate(ctx, request, "widget_bootstrap")
    async with tenant_session(ctx.admin_id) as s:
        inst = (
            await s.execute(select(Instance).where(Instance.id == ctx.instance_id))
        ).scalar_one_or_none()
        if inst is None:
            raise not_found("Unknown widget.")
        business, assistant = _names(inst)
        if is_paused(inst):
            # Paused → the widget renders an empty <div>; still honest literals.
            return WidgetBootstrapOut(
                render_state="paused",
                business_name=business,
                assistant_name=assistant,
                opening_message="",
                ai_assistant_label="AI assistant",
                powered_by_vantage_mind=True,
            )
        # A card on file lifts the free-50 cap (Vision §7 PAYG). Hardcoding False here
        # rendered a paying tenant's widget as at_cap, inverting the PAYG promise (§12.9).
        state = await budget.check_budget(
            s,
            admin_id=ctx.admin_id,
            instance_id=ctx.instance_id,
            has_payment_method=await metering.has_live_card(s, admin_id=ctx.admin_id),
        )
        return WidgetBootstrapOut(
            render_state="at_cap" if state.at_cap else "active",
            business_name=business,
            assistant_name=assistant,
            # Disclosure clause names the BUSINESS, never the Luciel (`business` above
            # is inst.name, the Luciel's own name -- see _disclosure_business_name).
            opening_message=_disclosure(_disclosure_business_name(inst), assistant),
            ai_assistant_label="AI assistant",
            powered_by_vantage_mind=True,
        )


@router.post("/messages", response_model=WidgetSendResult)
async def send_message(body: WidgetSendRequest, request: Request) -> WidgetSendResult:
    """Run one inbound widget message through the agentic loop (§3.4.1).

    Paused → empty render, no LLM. At-cap is handled inside the loop (graceful
    no-LLM reply, §3.4.1b) and surfaces here as renderState 'at_cap'.
    """
    # TRUE arrival time, captured before any gate/DB work: the lead transcript row
    # carries it so reply-time analytics measure real latency (the widget has no
    # adapter InboundMessage.received_at; this is its equivalent).
    received_at = datetime.now(UTC)
    ctx = await _resolve_embed_key(body.embed_key)
    # Pre-budget abuse gate, stage one (§3.1.5): per trusted client IP, BEFORE the
    # budget gate / any DB or LLM work, so a widget flood can never drain budget or
    # run up cost (denial-of-wallet). 429 on deny.
    client_ip = await _widget_ip_gate(ctx, request, "widget_ip")
    deny_reason: str | None = None
    async with tenant_session(ctx.admin_id) as s:
        inst = (
            await s.execute(select(Instance).where(Instance.id == ctx.instance_id))
        ).scalar_one_or_none()
        if inst is None:
            raise not_found("Unknown widget.")

        # Does the supplied session id belong to a conversation of THIS tenant that is
        # still in flight? Read-only. It decides three things at once: the §3.6.2 /
        # §3.6.3 drain (a paused or deleting Luciel finishes in-flight chats but opens
        # none), the stage-two admission bucket, and whether this turn opens a session.
        existing = await find_open_session(
            s,
            admin_id=ctx.admin_id,
            instance_id=ctx.instance_id,
            channel=CHANNEL,
            session_id=body.session_id,
        )

        if is_paused(inst) and existing is None:
            # Without a live in-window session this is the empty render directive —
            # no session opened, no LLM.
            return WidgetSendResult(
                session_id=body.session_id or uuid.uuid4(),
                reply=WidgetMessageOut(
                    message_id=uuid.uuid4(),
                    role="assistant",
                    text="",
                    at=datetime.now(UTC),
                ),
                render_state="paused",
            )

        # Stage two (§3.1.5): the per-session bucket only for a PROVEN open session;
        # otherwise the new-session cap — a rotating made-up id opens nothing for free.
        gate = _widget_session_gate(ctx, client_ip, existing.id if existing else None)
        if not gate.allowed:
            deny_reason = gate.reason or "rate_limited"
    if deny_reason is not None:
        await _deny_widget(ctx, deny_reason)
    async with tenant_session(ctx.admin_id) as s:
        inst = (
            await s.execute(select(Instance).where(Instance.id == ctx.instance_id))
        ).scalar_one_or_none()
        if inst is None:
            raise not_found("Unknown widget.")

        # Widget identity is WEAK (§3.4.9): no resolved lead is inherited from a prior
        # session. A lead is created/linked by the identity arc; here resolved_lead_id
        # stays None so cross-session memory is never carried (HARD RULE §3.4.9).
        #
        # The client-supplied sessionId is how a weak-identity visitor resumes its OWN
        # conversation across turns; without it every message opened a fresh session, so
        # the widget had no multi-turn memory and each turn counted as a new conversation
        # (§9.5). Matching is scoped to this tenant/instance/channel, and the §3.4.9 rule
        # still holds — a resumed session replays only its own transcript.
        sess, is_new = await resolve_or_open_session(
            s,
            admin_id=ctx.admin_id,
            instance_id=ctx.instance_id,
            channel=CHANNEL,
            resolved_lead_id=None,
            session_id=body.session_id,
        )

        # Double-disclosure fix (audit round 3, C5): the visitor ALREADY saw the
        # bootstrap opening bubble (the §3.4.16 disclosure), but nothing persisted
        # it — so the loop's first-message backstop saw no prior assistant turn and
        # prepended the same greeting again onto the first real reply. Persist the
        # greeting as the new session's first `luciel` row: the transcript now
        # matches what the visitor actually saw, and the backstop stands down.
        # Stamped 1ms before the inbound so ordering is deterministic; reply-time
        # analytics measure lead→NEXT-luciel gaps, which this row never is.
        if is_new:
            await persist_turn(
                s,
                admin_id=ctx.admin_id,
                instance_id=ctx.instance_id,
                session_id=sess.id,
                role="luciel",
                text=_disclosure(_disclosure_business_name(inst), _names(inst)[1]),
                at=received_at - timedelta(milliseconds=1),
                deterministic=True,
            )

        # Arc 16 binds the production hybrid retriever (vector + intent-gated graph,
        # §3.2.1) behind the orchestrator's injected seam — closing the Arc 14/14b empty
        # "Arc 11/16" stub. Retrieval is real over the tenant's ingested knowledge.
        # Content flows through wrap_untrusted in the loop (§3.4.14).
        retriever = make_retriever(s, admin_id=ctx.admin_id, instance_id=ctx.instance_id)
        # Harmony wave 2, item 2a: bind the real chunk-attribution seam alongside the
        # retriever so persist_turn can attach real source_chunks instead of leaving
        # attribution uncaptured for every widget reply.
        chunk_attributor = make_chunk_attributor(
            s, admin_id=ctx.admin_id, instance_id=ctx.instance_id
        )

        # §3.8.7 rule E (Decision #39): source the additive/neutral effective config
        # (enabled tools, business-context, enabled channels) from the config snapshot
        # frozen at session-open, so an additive pillar edit made mid-conversation
        # applies to the NEXT conversation — not this in-flight one. NULL snapshot
        # (older sessions) falls back to the LIVE instance config. Connection health +
        # disable/revoke stay LIVE via the broker gates (safety half wins).
        effective = effective_config(sess.config_snapshot, inst, always_include_channel=CHANNEL)
        business, assistant = _names(inst)

        # LLM router is mode-aware (GATE 2 cutover, §3.4.3): mock (default) returns the
        # deterministic EchoModel exactly as before; live binds the real Anthropic/
        # OpenAI providers from the platform secret. admin_id flows through so the
        # per-tenant LLM cap applies (§9 C9).
        orch = Orchestrator(
            session=s,
            router=build_llm_router(admin_id=ctx.admin_id),
            retriever=retriever,
            chunk_attributor=chunk_attributor,
            admin_id=ctx.admin_id,
            instance_id=ctx.instance_id,
            enabled_channels=effective.enabled_channels,
            # Names for the first-message AI-identity disclosure backstop (§3.4.16).
            # `business` here is inst.name (the Luciel's own name) -- never pass it as
            # business_name for the disclosure; use _disclosure_business_name (never
            # the Luciel name) so the backstop never names the Luciel as the business.
            business_name=_disclosure_business_name(inst),
            assistant_name=assistant,
            # 280-char admin business-context, composed UNDER the platform invariants
            # by Defense 6 (§3.4.14) — it tunes tone/facts, never overrides identity.
            business_context=effective.business_context,
            # Vision §3.5: the selected preset / custom axes compose into the system
            # prompt, so the personality pillar actually changes how Luciel talks.
            personality=effective.personality,
            # Real card state, not a literal False: a PAYG tenant must keep serving past
            # the free 50 (§12.9 / Vision §7).
            has_payment_method=await metering.has_live_card(s, admin_id=ctx.admin_id),
            snapshot_tool_ids=effective.snapshot_tool_ids,
            tool_resolver=make_tool_resolver(),
        )
        result = await orch.run_turn(
            session_id=sess.id,
            resolved_lead_id=sess.resolved_lead_id,
            channel=CHANNEL,
            mode=sess.mode,
            inbound_text=body.text,
            received_at=received_at,
        )

        # Only the at-cap turn gets the canned at-cap line (§3.4.1b). A session a
        # person has taken over answers with an EMPTY reply: the widget shows its own
        # "a member of the team has this conversation" note and polls for the human
        # reply — the fabricated "passed along" line it used to get was never
        # persisted and never true (audit F145).
        reply_text = result.reply_text or (AT_CAP_REPLY if result.at_cap else "")
        render_state = "at_cap" if result.at_cap else "active"
        return WidgetSendResult(
            session_id=sess.id,
            reply=WidgetMessageOut(
                message_id=uuid.uuid4(),
                role="assistant",
                text=reply_text,
                at=sess.last_message_at,
            ),
            render_state=render_state,
        )


@router.post("/sessions/{session_id}/end", status_code=204)
async def end_session(
    session_id: uuid.UUID,
    # Query key is camelCase to match widget-adapters.ts (?embedKey=...). A query
    # param (not a body) because the widget sends this via navigator.sendBeacon on
    # pagehide, which cannot set JSON content types reliably across browsers.
    embed_key: str = Query(alias="embedKey"),
) -> Response:
    """Widget close beacon — the §3.4.8 explicit end signal for the widget channel.

    Ends THIS widget session (finalizes + summarizes, §3.4.7) instead of leaving it
    to the 30-minute inactivity sweep. Idempotent: repeat beacons and races with the
    sweep are no-ops. Scoped to the embed key's tenant + the widget channel, so the
    public key can only ever end its own tenant's widget sessions — and an unknown
    session id acks 204 rather than confirming existence to an unauthenticated
    caller (same non-enumeration posture as the rest of the data plane).
    """
    ctx = await _resolve_embed_key(embed_key)
    async with tenant_session(ctx.admin_id) as s:
        await session_close.finalize_session(
            s,
            admin_id=ctx.admin_id,
            instance_id=ctx.instance_id,
            channel="widget",
            session_id=session_id,
        )
    return Response(status_code=204)


@router.get("/sessions/{session_id}", response_model=list[WidgetMessageOut])
async def get_history(
    session_id: uuid.UUID,
    request: Request,
    # Query key is camelCase to match widget-adapters.ts history() (?embedKey=...).
    embed_key: str = Query(alias="embedKey"),
) -> list[WidgetMessageOut]:
    """Visitor-facing transcript for a widget session (data plane only).

    Roles are mapped to the visitor/assistant vocabulary — the widget never sees the
    internal lead/luciel/human_agent roles. embed_key authenticates the request
    (query param, mirroring widget-adapters.ts history()). This is also how a
    takeover reply reaches the visitor: the shipped widget polls it (§3.4.12).

    Scoped to WIDGET sessions of THIS instance: a public embed key plus a guessed
    session id must never read an SMS/email/voice transcript of the same tenant
    (audit F075) — the widget can only ever replay conversations it could have had.
    """
    ctx = await _resolve_embed_key(embed_key)
    await _widget_ip_gate(ctx, request, "widget_poll")
    async with tenant_session(ctx.admin_id) as s:
        rows = (
            (
                await s.execute(
                    select(Transcript)
                    .join(ConversationSession, ConversationSession.id == Transcript.session_id)
                    .where(
                        Transcript.admin_id == ctx.admin_id,
                        Transcript.instance_id == ctx.instance_id,
                        Transcript.session_id == session_id,
                        ConversationSession.channel == CHANNEL,
                    )
                    .order_by(Transcript.at.asc())
                )
            )
            .scalars()
            .all()
        )
        out: list[WidgetMessageOut] = []
        for r in rows:
            role = "visitor" if r.role == "lead" else "assistant"
            out.append(WidgetMessageOut(message_id=r.id, role=role, text=r.text, at=r.at))
        return out
