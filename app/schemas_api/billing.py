"""Wire models for the billing API (frozen contract: schemas/billing.ts).

Mirror the frozen Zod schemas EXACTLY (camelCase via WireModel):
  Budget, PaymentMethod, BillingInfo, CheckoutSession.

Card hygiene (HARD CONSTRAINT #2): PaymentMethodOut carries ONLY the display tuple
(brand + last4 + expMonth + expYear). There is deliberately NO field that can
carry a PAN/CVV — these models physically cannot leak card data (a test asserts
the response key set). ``freeAllowance`` serializes literally 50 (z.literal(50)).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from app.schemas_api.common import WireModel


class BudgetOut(WireModel):
    """Conversation budget (frozen billing.ts ``budget``). at_cap is DATA (§3.4.1b)."""

    billing_state: Literal["free_cap", "payg_enabled"]
    dunning_state: Literal["none", "retrying", "reduced"]
    conversations_this_period: int
    # z.literal(50) on the wire — the field must serialize exactly 50 (Vision §7).
    free_allowance: Literal[50] = 50
    billed_this_period: int
    period_resets_at: datetime
    at_cap: bool


class PaymentMethodOut(WireModel):
    """Display-only card tuple (frozen billing.ts ``paymentMethod``). NO PAN."""

    brand: str
    last4: str
    exp_month: int
    exp_year: int


class BillingInfoOut(WireModel):
    """{budget, paymentMethod?} — paymentMethod present only when payg_enabled."""

    budget: BudgetOut
    payment_method: PaymentMethodOut | None = None


class CheckoutSessionOut(WireModel):
    """Hosted Stripe Checkout result (frozen billing.ts ``checkoutSession``)."""

    url: str
