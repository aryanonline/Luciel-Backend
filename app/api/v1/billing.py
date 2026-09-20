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

import logging
import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Subject, current_admin
from app.api.errors import not_found
from app.audit import log as audit
from app.billing import metering
from app.billing.stripe_gateway import StripeGateway, get_stripe_gateway
from app.config import get_settings
from app.db.session import tenant_session
from app.models.audit import ActorType
from app.models.instance import Account, Instance
from app.notifications import notifier as notify
from app.schemas_api.billing import (
    BillingInfoOut,
    BudgetOut,
    CheckoutSessionOut,
    PaymentMethodOut,
)

router = APIRouter(prefix="/api/v1/billing", tags=["billing"])
_log = logging.getLogger(__name__)


def _payments_available() -> bool:
    """Whether a card can actually be saved through this deployment (audit F026).

    In the "mock" Stripe posture the checkout URL is a placeholder that goes nowhere,
    so the dashboard must say so instead of rendering a button that pretends to work.
    """
    return get_settings().stripe_integration_posture != "mock"


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
            "billable": b.billable,
            "accruedBlocks": b.accrued_blocks,
            "reportedBlocks": b.reported_blocks,
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
        return BillingInfoOut.model_validate(
            {
                "budget": _budget_out(budget),
                "paymentMethod": pm,
                "paymentsAvailable": _payments_available(),
            }
        )


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

        # Return URLs derive from app_base_url (the auth.py:109 verify-link rule):
        # the customer must land back on THEIR dashboard's billing page. These were
        # hardcoded to a placeholder domain the product does not serve — live-caught
        # 2026-08-17: after saving a card, Stripe redirected the admin off-site.
        # The path is the REAL frontend route /dashboard/billing ((app) is a route
        # group and contributes no segment; a bare /billing 404s).
        base = get_settings().app_base_url.rstrip("/")
        session = await gateway.create_checkout_session(
            customer_id=account.stripe_customer_id,
            success_url=f"{base}/dashboard/billing?checkout=success",
            cancel_url=f"{base}/dashboard/billing?checkout=cancel",
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
    """Remove the card → revert toward free_cap behavior (§3.4.1b).

    Owner decision D1 (2026-09-14): a voluntary card removal changes NOTHING but the
    50-conversation cap. Every connection — action tools included — keeps its status;
    only the day-7 dunning reduction (``dunning.advance_if_due`` / the
    ``invoice.payment_failed`` webhook) sends action tools dormant. A conversation
    already in flight is served to its end (Legal §A3 #3; ``budget.check_budget``).

    Axis10-F01: returns the updated BillingInfo (200) so the wire return type matches
    the frontend client (`Promise<BillingInfo>`) and the mock adapter — previously this
    returned 204 (no body) while both sides expected the billing object.
    """
    async with tenant_session(subject.admin_id) as s:
        account = await metering.get_or_create_billing_account(s, admin_id=subject.admin_id)
        # Bill what they already ran BEFORE the account stops looking billable — the
        # same order the Stripe-side detach webhook uses (audit F031): flush the
        # accrued residual to the meter, end the metered subscription with a final
        # invoice while the card can still be charged, and only then detach the card.
        try:
            await metering.report_accrued_usage(s, admin_id=subject.admin_id, gateway=gateway)
        except Exception:  # noqa: BLE001 — the watermark stays; the reconcile sweep retries
            _log.exception("accrued usage flush failed at card removal %s", subject.admin_id)
        if account.stripe_subscription_id is not None:
            try:
                if await gateway.cancel_subscription(
                    subscription_id=account.stripe_subscription_id, invoice_now=True
                ):
                    account.stripe_subscription_id = None
            except Exception:  # noqa: BLE001
                _log.exception("subscription cancel failed at card removal %s", subject.admin_id)
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
        # D1: no connection changes hands here — the cap is the whole consequence.
        # The owner is told so (email + escalation SMS when on file; Legal §A3 /
        # §3.6.7, round 7 WP-1 #3c) — audited as lifecycle_notice_sent.
        await notify.notify_payment_method_removed(
            s, admin_id=subject.admin_id, reason="admin_removed"
        )
        instance_id = await _instance_id(s, subject.admin_id)
        budget = await metering.build_budget(s, admin_id=subject.admin_id, instance_id=instance_id)
        # After removal the account is free_cap, so paymentMethod is omitted.
        return BillingInfoOut.model_validate(
            {
                "budget": _budget_out(budget),
                "paymentMethod": None,
                "paymentsAvailable": _payments_available(),
            }
        )
