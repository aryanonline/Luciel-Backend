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
from datetime import UTC, datetime

from fastapi import APIRouter, Query, Request
from sqlalchemy import select

from app.api.errors import not_found, rate_limited
from app.audit import log as audit
from app.channels import rate_limit
from app.channels.base import InboundRejected, InstanceContext
from app.channels.routing import resolve_widget
from app.db.session import tenant_session, untenanted_session
from app.models.audit import ActorType
from app.models.conversation import Transcript
from app.models.instance import Instance, LucielState
from app.runtime import budget
from app.runtime.knowledge_retrieval import make_retriever
from app.runtime.orchestrator import AT_CAP_REPLY, Orchestrator
from app.runtime.provider_factory import build_llm_router
from app.runtime.sessions import effective_config, resolve_or_open_session
from app.schemas_api.widget import (
    WidgetBootstrapOut,
    WidgetBootstrapRequest,
    WidgetMessageOut,
    WidgetSendRequest,
    WidgetSendResult,
)

router = APIRouter(prefix="/api/v1/chat-widget", tags=["chat-widget"])

CHANNEL = "widget"


# AI-identity disclosure wording (§3.4.16). Admin-shaped in production via the
# personality pillar; the FACT of AI ("AI assistant") is non-removable.
def _disclosure(business: str, assistant: str) -> str:
    return (
        f"Hi — I'm {assistant}, the AI assistant for {business}. "
        "I can help answer questions, book, or connect you to a person."
    )


async def _resolve_embed_key(embed_key: str) -> InstanceContext:
    async with untenanted_session() as s:
        try:
            return await resolve_widget(s, embed_key)
        except InboundRejected as exc:
            # Unresolvable embed key → 404 (never routed on a guess, §3.1.3).
            raise not_found("Unknown widget.") from exc


async def _widget_abuse_gate(
    ctx: InstanceContext, request: Request, session_id: uuid.UUID | None
) -> None:
    """Pre-budget abuse / rate-limit admission for the widget (§3.1.5).

    The widget is a direct browser POST (no channel webhook), so it does not flow
    through channels.pipeline.ingest(); this is the widget's equivalent pre-budget
    gate. It MUST run before the budget gate and before any LLM call, so a flood can
    never drain a tenant's conversation budget or run up cost (denial-of-wallet).

    Source identifier per §3.1.5: the per-session widget token + client IP. On a
    denial we audit widget_abuse_blocked (no raw PII) and return HTTP 429, which the
    frontend transport maps to `rate_limited` via the documented error contract (§4).
    """
    client_ip = request.client.host if request.client else "unknown"
    source_id = f"{session_id or 'new'}:{client_ip}"
    decision = rate_limit.admit("widget", source_id, str(ctx.admin_id))
    if not decision.allowed:
        async with tenant_session(ctx.admin_id) as s:
            await audit.emit(
                s,
                admin_id=ctx.admin_id,
                instance_id=ctx.instance_id,
                actor_type=ActorType.platform,
                event_type="widget_abuse_blocked",
                # No raw PII: channel + reason only (§3.1.5 / §3.4.11).
                payload={"channel": "widget", "reason": decision.reason},
            )
        raise rate_limited("Too many messages — please slow down.", retry_after_seconds=3)


def _names(inst: Instance) -> tuple[str, str]:
    """Business + assistant display names from the instance config (§3.4.16)."""
    business = inst.name or "this business"
    personality = inst.personality or {}
    assistant = str(personality.get("assistantName") or f"{business}'s assistant")
    return business, assistant


def _is_paused(inst: Instance) -> bool:
    # Paused / soft-deleted Luciel renders an empty widget (§3.6.2) — no chat, no error.
    return inst.state in (
        LucielState.paused,
        LucielState.luciel_grace_window,
        LucielState.luciel_hard_deleted,
    )


@router.post("/bootstrap", response_model=WidgetBootstrapOut)
async def bootstrap(body: WidgetBootstrapRequest) -> WidgetBootstrapOut:
    """Resolve the host page's embed key → render directive + disclosure (§3.6.2/§3.4.16)."""
    ctx = await _resolve_embed_key(body.embed_key)
    async with tenant_session(ctx.admin_id) as s:
        inst = (
            await s.execute(select(Instance).where(Instance.id == ctx.instance_id))
        ).scalar_one_or_none()
        if inst is None:
            raise not_found("Unknown widget.")
        business, assistant = _names(inst)
        if _is_paused(inst):
            # Paused → the widget renders an empty <div>; still honest literals.
            return WidgetBootstrapOut(
                render_state="paused",
                business_name=business,
                assistant_name=assistant,
                opening_message="",
                ai_assistant_label="AI assistant",
                powered_by_vantage_mind=True,
            )
        state = await budget.check_budget(
            s, admin_id=ctx.admin_id, instance_id=ctx.instance_id, has_payment_method=False
        )
        return WidgetBootstrapOut(
            render_state="at_cap" if state.at_cap else "active",
            business_name=business,
            assistant_name=assistant,
            opening_message=_disclosure(business, assistant),
            ai_assistant_label="AI assistant",
            powered_by_vantage_mind=True,
        )


@router.post("/messages", response_model=WidgetSendResult)
async def send_message(body: WidgetSendRequest, request: Request) -> WidgetSendResult:
    """Run one inbound widget message through the agentic loop (§3.4.1).

    Paused → empty render, no LLM. At-cap is handled inside the loop (graceful
    no-LLM reply, §3.4.1b) and surfaces here as renderState 'at_cap'.
    """
    ctx = await _resolve_embed_key(body.embed_key)
    # Pre-budget abuse gate (§3.1.5): runs BEFORE the budget gate / any LLM call so a
    # widget flood can never drain budget or run up cost (denial-of-wallet). 429 on deny.
    await _widget_abuse_gate(ctx, request, body.session_id)
    async with tenant_session(ctx.admin_id) as s:
        inst = (
            await s.execute(select(Instance).where(Instance.id == ctx.instance_id))
        ).scalar_one_or_none()
        if inst is None:
            raise not_found("Unknown widget.")

        if _is_paused(inst):
            # Empty render directive — no session opened, no LLM (§3.6.2).
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

        # Widget identity is WEAK (§3.4.9): no resolved lead is inherited from a prior
        # session. A lead is created/linked by the identity arc; here resolved_lead_id
        # stays None so cross-session memory is never carried (HARD RULE §3.4.9).
        sess, _is_new = await resolve_or_open_session(
            s,
            admin_id=ctx.admin_id,
            instance_id=ctx.instance_id,
            channel=CHANNEL,
            resolved_lead_id=None,
        )

        # Arc 16 binds the production hybrid retriever (vector + intent-gated graph,
        # §3.2.1) behind the orchestrator's injected seam — closing the Arc 14/14b empty
        # "Arc 11/16" stub. Retrieval is real over the tenant's ingested knowledge.
        # Content flows through wrap_untrusted in the loop (§3.4.14).
        retriever = make_retriever(s, admin_id=ctx.admin_id, instance_id=ctx.instance_id)

        # §3.8.7 rule E (Decision #39): source the additive/neutral effective config
        # (enabled tools, business-context, enabled channels) from the config snapshot
        # frozen at session-open, so an additive pillar edit made mid-conversation
        # applies to the NEXT conversation — not this in-flight one. NULL snapshot
        # (older sessions) falls back to the LIVE instance config. Connection health +
        # disable/revoke stay LIVE via the broker gates (safety half wins).
        effective = effective_config(sess.config_snapshot, inst, always_include_channel=CHANNEL)

        # LLM router is mode-aware (GATE 2 cutover, §3.4.3): mock (default) returns the
        # deterministic EchoModel exactly as before; live binds the real Anthropic/
        # OpenAI providers from the platform secret. admin_id flows through so the
        # per-tenant LLM cap applies (§9 C9).
        orch = Orchestrator(
            session=s,
            router=build_llm_router(admin_id=ctx.admin_id),
            retriever=retriever,
            admin_id=ctx.admin_id,
            instance_id=ctx.instance_id,
            enabled_channels=effective.enabled_channels,
            # 280-char admin business-context, composed UNDER the platform invariants
            # by Defense 6 (§3.4.14) — it tunes tone/facts, never overrides identity.
            business_context=effective.business_context,
            has_payment_method=False,
            snapshot_tool_ids=effective.snapshot_tool_ids,
        )
        result = await orch.run_turn(
            session_id=sess.id,
            resolved_lead_id=None,
            channel=CHANNEL,
            mode=sess.mode,
            inbound_text=body.text,
        )

        reply_text = result.reply_text or AT_CAP_REPLY
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


@router.get("/sessions/{session_id}", response_model=list[WidgetMessageOut])
async def get_history(
    session_id: uuid.UUID,
    # Query key is camelCase to match widget-adapters.ts history() (?embedKey=...).
    embed_key: str = Query(alias="embedKey"),
) -> list[WidgetMessageOut]:
    """Visitor-facing transcript for a widget session (data plane only).

    Roles are mapped to the visitor/assistant vocabulary — the widget never sees the
    internal lead/luciel/human_agent roles. embed_key authenticates the request
    (query param, mirroring widget-adapters.ts history()).
    """
    ctx = await _resolve_embed_key(embed_key)
    async with tenant_session(ctx.admin_id) as s:
        rows = (
            (
                await s.execute(
                    select(Transcript)
                    .where(
                        Transcript.admin_id == ctx.admin_id,
                        Transcript.session_id == session_id,
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
