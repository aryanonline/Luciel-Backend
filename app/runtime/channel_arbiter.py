"""Channel arbiter — picks the outbound channel per response (Architecture §3.4.2)."""

from __future__ import annotations

# Immediacy ordering: real-time voice > messaging (sms/whatsapp/instagram/messenger) > email.
_IMMEDIACY = {
    "voice": 0,
    "sms": 1,
    "whatsapp": 1,
    "instagram_messenger": 1,
    "widget": 1,
    "email": 2,
}

SMS_LONG_RESPONSE_CHARS = 500


def choose_outbound_channel(
    *,
    inbound_channel: str,
    enabled_channels: set[str],
    response_length: int,
    customer_requested_channel: str | None = None,
    is_urgent_escalation: bool = False,
) -> str:
    """Decision tree (§3.4.2). Arbiter only selects from enabled channels; falls back
    to the inbound channel if a preferred channel is disabled."""
    # 1. Explicit customer channel request.
    if customer_requested_channel and customer_requested_channel in enabled_channels:
        return customer_requested_channel

    # 2. Long response over SMS → switch to email if enabled (prompt for permission upstream).
    if (
        inbound_channel == "sms"
        and response_length > SMS_LONG_RESPONSE_CHARS
        and "email" in enabled_channels
    ):
        return "email"

    # 3. Urgent escalation → highest-immediacy enabled channel.
    if is_urgent_escalation:
        candidates = sorted(enabled_channels, key=lambda c: _IMMEDIACY.get(c, 99))
        if candidates:
            return candidates[0]

    # 4. Default: same channel as inbound (fallback to inbound if preferred disabled).
    if inbound_channel in enabled_channels:
        return inbound_channel
    # If inbound somehow disabled, pick the most immediate enabled channel.
    candidates = sorted(enabled_channels, key=lambda c: _IMMEDIACY.get(c, 99))
    return candidates[0] if candidates else inbound_channel
