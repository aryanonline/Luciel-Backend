"""Auth wire shapes — mirror packages/api-client/src/schemas/auth.ts exactly."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import EmailStr, Field

from app.schemas_api.common import WireModel

AccountStateWire = Literal["unverified", "verified", "paused", "pending_deletion", "closed"]
NextRoute = Literal["first_run", "dashboard", "verify_wall"]


class AccountOut(WireModel):
    admin_id: uuid.UUID
    user_id: uuid.UUID
    email: EmailStr
    state: AccountStateWire
    created_at: datetime
    email_verified: bool
    has_completed_first_run: bool
    has_luciel: bool


AccountNoticeKind = Literal["allowance_reduction", "price_change", "terms_change"]


class AccountNoticeOut(WireModel):
    notice_id: str
    kind: AccountNoticeKind
    title: str
    body: str
    effective_at: datetime | None = None
    learn_more_href: str | None = None


class SessionOut(WireModel):
    account: AccountOut
    next_route: NextRoute
    # In-dashboard notices (Legal §A5, §A10, §B10). Defaults empty, which is the normal
    # state — a notice appears only while a platform-wide change is pending.
    notices: list[AccountNoticeOut] = Field(default_factory=list)


class SignupRequest(WireModel):
    email: EmailStr
    password: str = Field(min_length=1)
    captcha_token: str = Field(min_length=1)


class SignupResult(WireModel):
    account: AccountOut
    # True when the verification mail did not leave the system. The account exists but
    # is hard-gated until verified, so the UI must offer a resend instead of implying
    # "check your inbox". Never carries the provider error.
    email_delivery_degraded: bool = False


class LoginRequest(WireModel):
    email: EmailStr
    password: str = Field(min_length=1)


class ForgotPasswordRequest(WireModel):
    email: EmailStr


class ResetPasswordRequest(WireModel):
    token: str = Field(min_length=1)
    new_password: str = Field(min_length=1)


class VerifyEmailRequest(WireModel):
    token: str = Field(min_length=1)


class ResendVerificationResult(WireModel):
    ok: bool
    cooldown_seconds_remaining: int | None = None
    email_delivery_degraded: bool = False


class OkResult(WireModel):
    ok: bool
