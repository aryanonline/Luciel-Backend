"""POST /api/v1/webhooks/stripe — Stripe webhook receiver (Architecture §3.4.1b).

Verify-before-route (HARD CONSTRAINT #4, Arc 13 discipline): the raw body + the
``Stripe-Signature`` header are verified by the gateway BEFORE the event is parsed
or acted on. An unverified/garbled signature → 400 reject, nothing processed.

Tenant resolution: the webhook arrives with no session cookie. We resolve the
tenant from ``metadata.admin_id`` that we set on the Stripe customer/subscription
(carried through onto the event object). With a verified signature this metadata
is trustworthy. We then open a ``tenant_session(admin_id)`` so RLS scopes every
mutation; without the GUC, tenant tables are invisible (safe default).

Idempotent (HARD CONSTRAINT, §3.4.1b): a redelivered event id is a no-op (the
handler stores the last processed event id on the billing account).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request

from app.api.errors import validation_error
from app.billing import webhooks as billing_webhooks
from app.billing.stripe_gateway import SignatureError, StripeGateway, get_stripe_gateway
from app.db.session import tenant_session

router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])


@router.post("/stripe")
async def stripe_webhook(
    request: Request,
    gateway: StripeGateway = Depends(get_stripe_gateway),
) -> dict[str, str]:
    raw = await request.body()
    signature = request.headers.get("Stripe-Signature", "")

    # Verify FIRST (HARD CONSTRAINT #4). Unverified → reject, act on nothing.
    try:
        event = gateway.verify_and_parse(payload=raw, signature_header=signature)
    except SignatureError as exc:
        # 422 maps to validation_error on the frontend; for a server-to-server
        # webhook the body is non-customer-facing. Reject without processing.
        raise validation_error("Invalid Stripe signature.") from exc

    # Resolve tenant from the verified event metadata (admin_id we set on the
    # Stripe object). Absent/garbled → ignore (nothing to apply, not an error).
    admin_id_raw = event.data.get("metadata", {})
    admin_id_str = admin_id_raw.get("admin_id") if isinstance(admin_id_raw, dict) else None
    if admin_id_str is None:
        return {"status": "ignored", "reason": "no_tenant_metadata"}
    try:
        admin_id = uuid.UUID(str(admin_id_str))
    except ValueError:
        return {"status": "ignored", "reason": "bad_tenant_metadata"}

    async with tenant_session(admin_id) as s:
        action = await billing_webhooks.handle_event(s, admin_id=admin_id, event=event)
    return {"status": "ok", "action": action}
