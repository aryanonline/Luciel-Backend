"""/api/v1/billing/* — the admin's own billing data plane (Architecture §3.4.1b).

NOTE the path: ``/api/v1/billing`` (NOT under /admin) — the billing data plane for
the admin's own account, matching the frozen client (http-admin.ts billing.*):
  * GET    /api/v1/billing                 → BillingInfo {budget, paymentMethod?}
  * POST   /api/v1/billing/checkout        → CheckoutSession {url}  (no charge at save)
  * DELETE /api/v1/billing/payment-method  → 204 (revert toward free_cap)

All endpoints are admin-authed (verified account, §3.7.2), run inside
``tenant_session`` so Layer-3 RLS scopes every row to the caller's tenant, and are
audited. Card hygiene (HARD CONSTRAINT #2): only the display tuple is ever read or
returned — Stripe holds the card.

at_cap is surfaced as DATA in the budget read; this endpoint does NOT throw on
at_cap (objective). 402 ``payment_required`` is reserved for actual
payment-required rejections per transport.ts, not for this read.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Subject, current_admin
from app.api.errors import not_found
from app.audit import log as audit
from app.billing import metering
from app.billing.stripe_gateway import StripeGateway, get_stripe_gateway
from app.db.session import tenant_session
from app.models.audit import ActorType
from app.models.instance import Account, Instance
from app.schemas_api.billing import (
    BillingInfoOut,
    BudgetOut,
    CheckoutSessionOut,
    PaymentMethodOut,
)

router = APIRouter(prefix="/api/v1/billing", tags=["billing"])


async def _instance_id(s: AsyncSession, admin_id: uuid.UUID) -> uuid.UUID:
    inst = (
        await s.execute(select(Instance.id).where(Instance.admin_id == admin_id))
    ).scalar_one_or_none()
    if inst is None:
        raise not_found("No Luciel.")
    return inst


def _budget_out(b: metering.MeteredBudget) -> BudgetOut:
    return BudgetOut.model_validate(
        {
            "billingState": b.billing_state,
            "dunningState": b.dunning_state,
            "conversationsThisPeriod": b.conversations_this_period,
            "freeAllowance": b.free_allowance,
            "billedThisPeriod": b.billed_this_period,
            "periodResetsAt": b.period_resets_at,
            "atCap": b.at_cap,
            "nearNextBlock": b.near_next_block,
        }
    )


@router.get("", response_model=BillingInfoOut)
async def get_billing(subject: Subject = Depends(current_admin)) -> BillingInfoOut:
    async with tenant_session(subject.admin_id) as s:
        instance_id = await _instance_id(s, subject.admin_id)
        budget = await metering.build_budget(s, admin_id=subject.admin_id, instance_id=instance_id)
        account = await metering.get_or_create_billing_account(s, admin_id=subject.admin_id)

        # paymentMethod present ONLY when payg_enabled (frozen billingInfo). Built
        # from the DISPLAY tuple only — no PAN ever (§3.4.1b card hygiene).
        pm: PaymentMethodOut | None = None
        if account.billing_state == "payg_enabled" and account.pm_brand is not None:
            pm = PaymentMethodOut.model_validate(
                {
                    "brand": account.pm_brand,
                    "last4": account.pm_last4,
                    "expMonth": account.pm_exp_month,
                    "expYear": account.pm_exp_year,
                }
            )
        return BillingInfoOut.model_validate({"budget": _budget_out(budget), "paymentMethod": pm})


@router.post("/checkout", response_model=CheckoutSessionOut)
async def start_checkout(
    subject: Subject = Depends(current_admin),
    gateway: StripeGateway = Depends(get_stripe_gateway),
) -> CheckoutSessionOut:
    """Begin Stripe Checkout to add a card — NO charge at save (CJ §6)."""
    async with tenant_session(subject.admin_id) as s:
        account = await metering.get_or_create_billing_account(s, admin_id=subject.admin_id)
        if account.stripe_customer_id is None:
            acct = (
                await s.execute(select(Account).where(Account.id == subject.admin_id))
            ).scalar_one()
            account.stripe_customer_id = await gateway.create_customer(
                admin_id=subject.admin_id, email=acct.email
            )
            await s.flush()

        session = await gateway.create_checkout_session(
            customer_id=account.stripe_customer_id,
            success_url="https://app.luciel.ai/billing?checkout=success",
            cancel_url="https://app.luciel.ai/billing?checkout=cancel",
        )
        # No charge at save (CJ §6): the card attach + billingState flip is driven by
        # the Stripe payment_method webhook, not by this call.
        await audit.emit(
            s,
            admin_id=subject.admin_id,
            actor_type=ActorType.admin_user,
            actor_user_id=subject.user_id,
            event_type="billing_checkout_started",
            payload={"checkout_session_id": session.session_id},
        )
        return CheckoutSessionOut.model_validate({"url": session.url})


@router.delete("/payment-method", response_model=BillingInfoOut)
async def remove_payment_method(
    subject: Subject = Depends(current_admin),
    gateway: StripeGateway = Depends(get_stripe_gateway),
) -> BillingInfoOut:
    """Remove the card → revert toward free_cap behavior (§3.4.1b / Legal §A3).

    Axis10-F01: returns the updated BillingInfo (200) so the wire return type matches
    the frontend client (`Promise<BillingInfo>`) and the mock adapter — previously this
    returned 204 (no body) while both sides expected the billing object.
    """
    async with tenant_session(subject.admin_id) as s:
        account = await metering.get_or_create_billing_account(s, admin_id=subject.admin_id)
        if account.stripe_customer_id is not None:
            await gateway.remove_payment_method(customer_id=account.stripe_customer_id)
        # Reverting to free_cap: above 50 now blocks (at-cap data), not metered.
        account.billing_state = "free_cap"
        account.pm_brand = None
        account.pm_last4 = None
        account.pm_exp_month = None
        account.pm_exp_year = None
        await s.flush()
        await audit.emit(
            s,
            admin_id=subject.admin_id,
            actor_type=ActorType.admin_user,
            actor_user_id=subject.user_id,
            event_type="payment_method_removed",
            payload={"reason": "admin_removed"},
        )
        instance_id = await _instance_id(s, subject.admin_id)
        budget = await metering.build_budget(s, admin_id=subject.admin_id, instance_id=instance_id)
        # After removal the account is free_cap, so paymentMethod is omitted.
        return BillingInfoOut.model_validate({"budget": _budget_out(budget), "paymentMethod": None})
