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


class SessionOut(WireModel):
    account: AccountOut
    next_route: NextRoute


class SignupRequest(WireModel):
    email: EmailStr
    password: str = Field(min_length=1)
    captcha_token: str = Field(min_length=1)


class SignupResult(WireModel):
    account: AccountOut


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


class OkResult(WireModel):
    ok: bool
