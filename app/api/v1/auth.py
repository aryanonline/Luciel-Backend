"""/api/v1/auth/* — account lifecycle endpoints (frozen contract: http-admin.ts).

Session is carried by an httpOnly + Secure + SameSite cookie (Space Instr §3.3);
the raw JWT is never returned in the body. Login/verify set the cookie; logout
clears it; reset clears it (all sessions revoked via epoch bump).
"""

from __future__ import annotations

import uuid

import jwt
from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select

from app.api.deps import Subject, current_session_subject
from app.api.errors import rate_limited, unauthorized, validation_error
from app.audit import log as audit
from app.auth import access
from app.auth.captcha import verify_captcha
from app.auth.tokens import decode_token
from app.billing import notices
from app.config import get_settings
from app.db.session import tenant_session
from app.models.audit import ActorType
from app.models.instance import Account
from app.notifications import email_dispatcher
from app.schemas_api.auth import (
    AccountNoticeOut,
    AccountOut,
    ForgotPasswordRequest,
    LoginRequest,
    OkResult,
    ResendVerificationResult,
    ResetPasswordRequest,
    SessionOut,
    SignupRequest,
    SignupResult,
    VerifyEmailRequest,
)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def _set_session_cookie(response: Response, token: str) -> None:
    s = get_settings()
    response.set_cookie(
        key=s.session_cookie_name,
        value=token,
        httponly=True,
        secure=s.cookie_secure,
        samesite=s.cookie_samesite,  # type: ignore[arg-type]
        max_age=s.session_ttl_seconds,
        path="/",
        # Parent-domain cookie so BOTH the frontend host (www./apex) and the API host
        # can read the session. Empty in local dev => host-only (Starlette omits Domain).
        domain=s.cookie_domain or None,
    )


def _clear_session_cookie(response: Response) -> None:
    s = get_settings()
    # Domain must match the one used when setting, or the browser won't clear it.
    response.delete_cookie(key=s.session_cookie_name, path="/", domain=s.cookie_domain or None)


def _account_out(acct: Account, *, has_luciel: bool) -> AccountOut:
    return AccountOut(
        admin_id=acct.id,
        user_id=acct.user_id,
        email=acct.email,
        state=acct.state.value,
        created_at=acct.created_at,
        email_verified=acct.email_verified,
        has_completed_first_run=acct.has_completed_first_run,
        has_luciel=has_luciel,
    )


def _notices_out() -> list[AccountNoticeOut]:
    """Notices ride the session payload the dashboard shell already reads, so they need no
    second request and are there on next login (Legal §A5). Normally empty."""
    return [AccountNoticeOut.model_validate(vars(n)) for n in notices.account_notices()]


async def _has_luciel(admin_id: uuid.UUID) -> bool:
    from app.models.instance import Instance

    async with tenant_session(admin_id) as s:
        return (
            await s.execute(select(Instance.id).where(Instance.admin_id == admin_id).limit(1))
        ).scalar_one_or_none() is not None


@router.post("/signup", response_model=SignupResult)
async def signup(req: SignupRequest, response: Response) -> SignupResult:
    # Verify the captcha with the provider (hCaptcha) BEFORE creating the account
    # (§3.7.1a bot-protection). In mock/unconfigured mode this is a no-op pass.
    await verify_captcha(req.captcha_token)
    acct, token = await access.signup(req.email, req.password)
    # Email the verification link (never returned in the body). Degrades to audit-
    # only if email isn't configured (NullEmailSender).
    degraded = False
    if token:
        settings = get_settings()
        # Frontend route is /verify?token=... (apps/web (auth)/verify/page.tsx), served
        # on the public web host (app_base_url), NOT the API host. Must match the
        # frontend path exactly or the emailed link 404s.
        verify_url = f"{settings.app_base_url}/verify?token={token}"
        async with tenant_session(acct.id) as s:
            outcome = await email_dispatcher.send_verify_email(
                s, admin_id=acct.id, to=acct.email, verify_url=verify_url
            )
        # Unverified accounts are hard-gated (deps.py), so a silently-undelivered
        # verification mail strands the customer with a 200 and no way forward. Tell
        # the client the mail did not leave the system so it can offer a resend —
        # the provider error itself stays in the log/audit trail, never on the wire.
        degraded = outcome.degraded
        # 2026-09-05 audit F148: the verify wall's "resend" is a session-authed call,
        # and until now signup issued no session — so the wall's copy promised a
        # resend that could only 401. A PENDING account (a verify token was minted)
        # gets its session now; it stays hard-gated everywhere else until verified.
        # A re-signup on an already-verified email mints no token and gets no
        # session: that path must never be a password-less login.
        from app.auth.tokens import (
            mint_session_token,  # noqa: PLC0415 — same lazy import verify_email uses
        )

        _set_session_cookie(response, mint_session_token(acct.id, acct.user_id, acct.session_epoch))
    return SignupResult(
        account=_account_out(acct, has_luciel=False), email_delivery_degraded=degraded
    )


def _throttle(bucket: str, key: str, message: str) -> None:
    """Sign-in / reset throttle (2026-09-05 audit F056/F068): the same per-source
    token buckets the channels use, keyed per email and per client address. A
    refusal is a 429 with a wait — never a hint about whether the account exists."""
    from app.channels import rate_limit  # noqa: PLC0415

    if not rate_limit.get_store().check_source(bucket, key).allowed:
        raise rate_limited(message, retry_after_seconds=60)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.post("/login", response_model=SessionOut)
async def login(req: LoginRequest, response: Response, request: Request) -> SessionOut:
    wait = "Too many sign-in attempts. Please wait a minute and try again."
    _throttle("auth_login_ip", _client_ip(request), wait)
    _throttle("auth_login_email", req.email.strip().lower(), wait)
    result = await access.login(req.email, req.password)
    if result is None:
        raise unauthorized("Invalid email or password.")
    acct, token, next_route = result
    _set_session_cookie(response, token)
    has_luciel = await _has_luciel(acct.id)
    return SessionOut(
        account=_account_out(acct, has_luciel=has_luciel),
        next_route=next_route,
        notices=_notices_out(),
    )


@router.post("/logout", status_code=204)
async def logout(
    response: Response,
    subject: Subject = Depends(current_session_subject),
) -> Response:
    """Clears the session cookie and emits the logout audit event (§3.7.1a)."""
    from app.db.session import tenant_session

    async with tenant_session(subject.admin_id) as s:
        await audit.emit(
            s,
            admin_id=subject.admin_id,
            actor_type=ActorType.admin_user,
            actor_user_id=subject.user_id,
            event_type="logout",
        )
    _clear_session_cookie(response)
    response.status_code = 204
    return response


@router.get("/me", response_model=SessionOut)
async def me(subject: Subject = Depends(current_session_subject)) -> SessionOut:
    async with tenant_session(subject.admin_id) as s:
        acct = (await s.execute(select(Account).where(Account.id == subject.admin_id))).scalar_one()
    has_luciel = await _has_luciel(subject.admin_id)
    next_route = access.next_route_for_state(acct.state, acct.email_verified, has_luciel)
    return SessionOut(
        account=_account_out(acct, has_luciel=has_luciel),
        next_route=next_route,
        notices=_notices_out(),
    )


@router.post("/verify-email", response_model=SessionOut)
async def verify_email(req: VerifyEmailRequest, response: Response) -> SessionOut:
    try:
        claims = decode_token(req.token)
    except jwt.PyJWTError as exc:
        raise validation_error("Invalid or expired verification link.") from exc
    if claims.get("typ") != "verify":
        raise validation_error("Wrong token type.")
    admin_id = uuid.UUID(claims["sub"])
    try:
        acct = await access.verify_email(admin_id, claims["jti"])
    except ValueError as exc:
        raise validation_error("Invalid or already-used verification link.") from exc
    # On verification, sign the owner in and route to first-run.
    from app.auth.tokens import mint_session_token

    token = mint_session_token(acct.id, acct.user_id, acct.session_epoch)
    _set_session_cookie(response, token)
    return SessionOut(account=_account_out(acct, has_luciel=False), next_route="first_run")


@router.post("/resend-verification", response_model=ResendVerificationResult)
async def resend_verification(
    subject: Subject = Depends(current_session_subject),
) -> ResendVerificationResult:
    token, cooldown = await access.reissue_verification(subject.admin_id)
    if cooldown is not None:
        return ResendVerificationResult(ok=False, cooldown_seconds_remaining=cooldown)
    if token is None:
        # Already verified (or no such account) — nothing to resend, still non-enumerating.
        return ResendVerificationResult(ok=True)
    settings = get_settings()
    verify_url = f"{settings.app_base_url}/verify?token={token}"
    async with tenant_session(subject.admin_id) as s:
        acct = (await s.execute(select(Account).where(Account.id == subject.admin_id))).scalar_one()
        outcome = await email_dispatcher.send_verify_email(
            s, admin_id=subject.admin_id, to=acct.email, verify_url=verify_url
        )
    return ResendVerificationResult(ok=True, email_delivery_degraded=outcome.degraded)


@router.post("/forgot-password", response_model=OkResult)
async def forgot_password(req: ForgotPasswordRequest, request: Request) -> OkResult:
    wait = "Too many reset requests. Please wait a few minutes and try again."
    _throttle("auth_forgot_ip", _client_ip(request), wait)
    _throttle("auth_forgot_email", req.email.strip().lower(), wait)
    token = await access.request_reset(req.email)  # non-enumerating
    if token is not None:
        settings = get_settings()
        # Frontend route is /reset?token=... (apps/web (auth)/reset/page.tsx), served
        # on the public web host. Must match the frontend path exactly or the link 404s.
        reset_url = f"{settings.app_base_url}/reset?token={token}"
        claims = decode_token(token)
        admin_id = uuid.UUID(claims["sub"])
        async with tenant_session(admin_id) as s:
            await email_dispatcher.send_reset_email(
                s, admin_id=admin_id, to=req.email, reset_url=reset_url
            )
    # Always the same shape: a delivery signal here would leak whether the address
    # is registered (§3.7.1a non-enumeration).
    return OkResult(ok=True)


@router.post("/reset-password", response_model=OkResult)
async def reset_password(req: ResetPasswordRequest, response: Response) -> OkResult:
    try:
        claims = decode_token(req.token)
    except jwt.PyJWTError as exc:
        raise validation_error("Invalid or expired reset link.") from exc
    if claims.get("typ") != "reset":
        raise validation_error("Wrong token type.")
    admin_id = uuid.UUID(claims["sub"])
    try:
        await access.complete_reset(admin_id, claims["jti"], req.new_password)
    except ValueError as exc:
        raise validation_error("Invalid or already-used reset link.") from exc
    _clear_session_cookie(response)  # all sessions revoked
    return OkResult(ok=True)
