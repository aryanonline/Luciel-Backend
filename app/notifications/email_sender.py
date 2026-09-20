"""Real email delivery via Amazon SES (Architecture §3.1.2 / §3.6.7 / §3.5).

Closes the AWS-DEFERRED notifier seam. Two implementations behind one Protocol:

- ``SesEmailSender``   — live: sends through Amazon SES (SESv2). Bound in live mode
  by ``build_email_sender`` from the platform config.
- ``NullEmailSender``  — mock/CI: records sends in-memory, makes NO network call, so
  the suite stays hermetic and deterministic. This is the default.

The dispatcher (``email_dispatcher.py``) renders templates and calls this; the
notifier/auth/lifecycle/export code call the dispatcher. Secrets are never logged.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class SentEmail:
    to: str
    subject: str
    body: str
    message_id: str
    from_address: str | None = None
    # Round 5B item 13: SES message tags (vm_admin_id / vm_purpose) recorded so
    # tests can assert bounce attribution rides every platform send.
    tags: dict[str, str] | None = None


class EmailSender(Protocol):
    async def send(
        self,
        *,
        to: str,
        subject: str,
        body_text: str,
        from_address: str | None = None,
        tags: dict[str, str] | None = None,
    ) -> str | None:
        """Send an email; return a provider message id, or None when nothing was put on
        the wire. Raises on hard failure.

        ``from_address`` overrides the platform identity for one message. A customer-facing
        channel reply must come FROM the tenant's own provisioned address (§3.1.6a); a
        platform notice (verification, dunning, escalation) omits it and uses the platform
        identity.

        ``tags`` become SES message tags, echoed back on bounce/complaint events —
        that is how a bounce names its tenant without any address→tenant scan
        (round 5B item 13). Values must be tag-safe (alphanumerics, ``-``, ``_``).
        """
        ...


class NullEmailSender:
    """Mock/CI sender — records intent, no network. Inspectable in tests."""

    def __init__(self) -> None:
        self.sent: list[SentEmail] = []

    async def send(
        self,
        *,
        to: str,
        subject: str,
        body_text: str,
        from_address: str | None = None,
        tags: dict[str, str] | None = None,
    ) -> str | None:
        from app.config import live_mode  # noqa: PLC0415 — avoids an import cycle

        mid = f"null-{len(self.sent) + 1}"
        self.sent.append(
            SentEmail(
                to=to,
                subject=subject,
                body=body_text,
                message_id=mid,
                from_address=from_address,
                tags=dict(tags) if tags else None,
            )
        )
        # Live mode with no verified sender identity: nothing left the system, and the
        # tool contract maps a non-None id to ok/accepted_for_delivery, which would have
        # Luciel say out loud that it sent an email it did not send.
        return None if live_mode() else mid


@dataclass
class SesEmailSender:
    """Live SES sender (SESv2). Lazy boto3 import so CI without AWS never imports it."""

    sender_identity: str
    region_name: str
    _client: object | None = field(default=None, repr=False)

    def _get_client(self) -> object:
        if self._client is None:
            import boto3  # noqa: PLC0415 — lazy/guarded (CI has no AWS)

            self._client = boto3.client("sesv2", region_name=self.region_name)
        return self._client

    async def send(
        self,
        *,
        to: str,
        subject: str,
        body_text: str,
        from_address: str | None = None,
        tags: dict[str, str] | None = None,
    ) -> str | None:
        from app.config import get_settings  # noqa: PLC0415

        client = self._get_client()
        kwargs: dict[str, object] = {
            "FromEmailAddress": from_address or self.sender_identity,
            "Destination": {"ToAddresses": [to]},
            "Content": {
                "Simple": {
                    "Subject": {"Data": subject, "Charset": "UTF-8"},
                    "Body": {"Text": {"Data": body_text, "Charset": "UTF-8"}},
                }
            },
        }
        # Round 5B item 13: the configuration set routes bounce/complaint events
        # to SNS, and the tags are echoed back on those events so the webhook can
        # attribute them to a tenant. Both optional: an env without the config
        # set keeps sending exactly as before.
        config_set = get_settings().ses_configuration_set
        if config_set:
            kwargs["ConfigurationSetName"] = config_set
        if tags:
            kwargs["EmailTags"] = [{"Name": k, "Value": v} for k, v in tags.items()]
        # boto3 is synchronous: off the event loop (round 7 WP-2, item 5), so a slow
        # SES call never stalls every other conversation this process is serving.
        resp = await asyncio.to_thread(client.send_email, **kwargs)  # type: ignore[attr-defined]
        return str(resp.get("MessageId", ""))


def build_email_sender() -> EmailSender:
    """Return the SES sender in live mode, else the Null sender (mock/CI).

    Live requires a configured platform sender identity; if absent, we fall back to
    Null and the dispatcher records the send as audit-only (honest degradation rather
    than a crash) — matching the codebase's integration-mode seam pattern.
    """
    from app.config import get_settings

    s = get_settings()
    if s.integration_mode == "live" and s.platform_email_sender:
        return SesEmailSender(sender_identity=s.platform_email_sender, region_name=s.aws_region)
    return NullEmailSender()
