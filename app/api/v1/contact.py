"""POST /api/v1/contact — the marketing site's contact form (ruling 1).

The frontend shipped a form posting ``{name, email, message, captchaToken}`` here and
nothing served it, so every enquiry a prospect typed was lost on submit. Ruling 1: build
it real.

Unauthenticated by necessity — the sender is a stranger who has not signed up, which is
the entire point of a contact form. Two controls stand in for a session:

  * the SAME hCaptcha seam signup uses (``auth.captcha.verify_captcha``), which is a
    no-op outside live-and-configured, so CI and local dev stay hermetic;
  * the SAME token bucket the channels use, keyed on source IP, so a scripted flood is
    refused rather than absorbed into the mailbox.

**The row is the receipt, and it is only accurate if it is written last.** Notification is
best-effort: SES can be down, the platform sender identity can be unconfigured, and in
mock mode nothing leaves the process at all. None of that should lose a message a person
took the trouble to write, and none of it should be reported to them as a failure. So the
email is attempted first and its outcome is stored ON the row as ``notified`` — writing
the row first would mean either an UPDATE the app role deliberately does not have, or a
``notified`` column that is permanently false. ``accepted`` then reflects the INSERT and
nothing else, per the ruling; a store failure is the one thing that becomes a 5xx, and by
then the platform has the message by email anyway.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Request
from pydantic import EmailStr, Field

from app.api.errors import ApiError, rate_limited
from app.auth.captcha import verify_captcha
from app.channels import rate_limit
from app.db.session import untenanted_session
from app.models.contact import MAX_MESSAGE, MAX_NAME, ContactSubmission
from app.schemas_api.common import WireModel

router = APIRouter(prefix="/api/v1/contact", tags=["contact"])

_log = logging.getLogger(__name__)

#: The token-bucket key. Deliberately not one of the §3.1.5 conversational channels; see
#: the note on ``rate_limit.LIMITS``.
_RATE_LIMIT_CHANNEL = "contact"


class ContactIn(WireModel):
    name: str = Field(min_length=1, max_length=MAX_NAME)
    email: EmailStr
    message: str = Field(min_length=1, max_length=MAX_MESSAGE)
    captcha_token: str | None = None


class ContactOut(WireModel):
    accepted: bool
    submission_id: uuid.UUID


async def _notify(submission_id: uuid.UUID, payload: ContactIn) -> bool:
    """Email the platform address. Returns whether anything left the process.

    Swallows provider failures on purpose: the message is about to be stored either way,
    and a 500 here would tell the visitor their message was lost when it was not. The
    failure is logged so an unnoticed outage is still diagnosable.
    """
    from app.config import get_settings
    from app.notifications.email_sender import build_email_sender

    settings = get_settings()
    # The inbox a person reads (info@vantagemind.ai by default; D12), never merely the
    # SES sender identity — sending still needs that identity as the From, which
    # build_email_sender resolves.
    recipient = settings.platform_contact_inbox
    # No sender identity (mock/CI) means nothing can leave the process: say so
    # honestly rather than counting a Null send as a notification.
    if not recipient or not settings.platform_email_sender:
        return False
    body = (
        f"From: {payload.name} <{payload.email}>\n"
        f"Submission: {submission_id}\n"
        f"Received: {datetime.now(UTC).isoformat()}\n\n"
        f"{payload.message}\n"
    )
    try:
        message_id = await build_email_sender().send(
            to=recipient, subject=f"Contact form: {payload.name}", body_text=body
        )
    except Exception:
        _log.exception("contact notification failed for submission %s", submission_id)
        return False
    return message_id is not None


@router.post("", response_model=ContactOut, status_code=202)
async def submit_contact(payload: ContactIn, request: Request) -> ContactOut:
    """Accepted iff the enquiry was stored."""
    source_ip = request.client.host if request.client else "unknown"
    if not rate_limit.get_store().check_source(_RATE_LIMIT_CHANNEL, source_ip).allowed:
        raise rate_limited("Too many messages — please try again shortly.", retry_after_seconds=60)

    await verify_captcha(payload.captcha_token)

    submission_id = uuid.uuid4()
    notified = await _notify(submission_id, payload)
    try:
        async with untenanted_session() as s:
            s.add(
                ContactSubmission(
                    id=submission_id,
                    name=payload.name.strip(),
                    email=str(payload.email),
                    message=payload.message.strip(),
                    notified=notified,
                    source_ip=source_ip,
                )
            )
    except Exception as exc:
        _log.exception("contact submission %s could not be stored", submission_id)
        raise ApiError(500, "Your message could not be saved. Please try again.") from exc

    return ContactOut(accepted=True, submission_id=submission_id)
