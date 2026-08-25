"""Public escalation-contact confirmation (round 5B item 13).

The contact may not be the account owner (a manager's inbox, a shared address),
so this route is PUBLIC and authorized by the signed single-use token in the
confirmation email — the same posture as the OAuth browser callback: possession
of a short-lived, purpose-bound, jti-pinned token IS the authorization. The jti
is consumed on first use (the `pending_verify_jti` pattern); an expired,
replayed, or superseded link is a clean 400 with a typed reason so the frontend
page can offer "ask the owner to re-send".
"""

from __future__ import annotations

import uuid

import jwt
from fastapi import APIRouter

from app.api.errors import validation_error
from app.audit import log as audit
from app.auth.tokens import decode_token
from app.db.session import tenant_session
from app.models.audit import ActorType
from app.notifications.delivery_incidents import confirm_verification
from app.schemas_api.luciel import ConfirmEscalationContactRequest

router = APIRouter(prefix="/api/v1/escalation-contact", tags=["escalation-contact"])


@router.post("/confirm")
async def confirm_escalation_contact(req: ConfirmEscalationContactRequest) -> dict[str, str]:
    try:
        payload = decode_token(req.token)
    except jwt.ExpiredSignatureError:
        raise validation_error("This confirmation link has expired — ask for a new one.") from None
    except jwt.PyJWTError:
        raise validation_error("This confirmation link is not valid.") from None
    if payload.get("typ") != "escalation_contact":
        raise validation_error("This confirmation link is not valid.")
    try:
        admin_id = uuid.UUID(str(payload.get("sub")))
    except (ValueError, TypeError):
        raise validation_error("This confirmation link is not valid.") from None
    jti = str(payload.get("jti") or "")
    if not jti:
        raise validation_error("This confirmation link is not valid.")
    async with tenant_session(admin_id) as s:
        row = await confirm_verification(s, admin_id=admin_id, jti=jti)
        if row is None:
            raise validation_error(
                "This confirmation link was already used or replaced — ask for a new one."
            )
        await audit.emit(
            s,
            admin_id=admin_id,
            actor_type=ActorType.platform,
            event_type="escalation_contact_verified",
            payload={"recipient": row.email_lower},
        )
    return {"status": "confirmed"}
