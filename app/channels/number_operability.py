"""SMS number operability probe (Architecture §3.1.4 BYO-number honesty).

Answers ONE question against Twilio: *can this account actually send/receive on this
number?* — i.e. is the E.164 number present in the account's ``IncomingPhoneNumbers``
(Twilio-owned or hosted into the account). This is the check that makes the BYO-number
flow honest: a number that merely "exists on some carrier" is NOT operable by our Twilio
transport; only a number in/hosted-by the account is. Attaching a non-operable number
must NOT show a "being activated" state (that was the pre-existing silent-failure bug) —
it is rejected with a hosting-required state, and a user-triggered re-verify re-runs this
probe live (stateless; repeatable forever; reads whatever number is currently attached).

HONEST BOUNDARY (constant on every call): ``IncomingPhoneNumbers`` confirms *operability*
(the number is in/hosted by this account). It does NOT confirm A2P 10DLC campaign approval
— that endpoint requires account-level auth this deployment's API key cannot use today
(returns Twilio error 20003). So an ``operable`` result means "we can transact on this
number"; carrier/10DLC registration remains the tenant's responsibility and is stated as
such rather than falsely claimed as verified.

Mirrors the ``SmsSender`` / ``EmailSender`` protocol+Null+factory pattern
(``app/channels/twilio_sender.py``, ``app/notifications/email_sender.py``): a Protocol,
a hermetic ``NullOperabilityProbe`` (mock/CI default — no network), a live
``TwilioOperabilityProbe`` (lazy SDK import), and a ``build_operability_probe`` factory
that returns Null unless fully live-configured (honest degradation, never a crash).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol


class OperabilityResult(str, Enum):
    OPERABLE = "operable"  # number is in/hosted by the account — we can transact on it
    NOT_OPERABLE = "not_operable"  # valid E.164 but NOT in the account — hosting required
    UNKNOWN = "unknown"  # probe could not determine (provider error/unreachable)


class NumberOperabilityProbe(Protocol):
    async def check(self, e164_number: str) -> OperabilityResult: ...


@dataclass
class NullOperabilityProbe:
    """Mock/CI probe — NO network. Deterministic + inspectable.

    ``operable_numbers`` is the set treated as operable (empty by default → every
    number is NOT_OPERABLE, the safe default for hermetic tests). Tests seed this to
    exercise the operable path without touching Twilio. Default probe whenever Twilio
    is not fully live-configured (mirrors ``NullSmsSender``)."""

    operable_numbers: set[str] = field(default_factory=set)
    calls: list[str] = field(default_factory=list)

    async def check(self, e164_number: str) -> OperabilityResult:
        self.calls.append(e164_number)
        return (
            OperabilityResult.OPERABLE
            if e164_number in self.operable_numbers
            else OperabilityResult.NOT_OPERABLE
        )


@dataclass
class TwilioOperabilityProbe:
    """Live probe: is ``e164_number`` in this account's IncomingPhoneNumbers?

    Uses the REST ``IncomingPhoneNumbers`` list filtered by ``PhoneNumber`` — the same
    API verified reachable with the deployment's API-Key creds (a hosted/owned number
    returns a row; a foreign number returns an empty list). Lazy-imports the Twilio SDK
    so mock/CI never needs it (mirrors ``TwilioSmsSender``)."""

    account_sid: str
    api_key_sid: str
    api_key_secret: str

    async def check(self, e164_number: str) -> OperabilityResult:
        try:
            from twilio.rest import Client
        except ImportError:
            return OperabilityResult.UNKNOWN
        try:
            client = Client(self.api_key_sid, self.api_key_secret, self.account_sid)
            # Exact-match filter; a number in/hosted-by the account returns >=1 row.
            matches = await _to_thread(
                lambda: client.incoming_phone_numbers.list(phone_number=e164_number, limit=1)
            )
            return OperabilityResult.OPERABLE if matches else OperabilityResult.NOT_OPERABLE
        except Exception:
            # Provider error / auth / network — cannot determine. NEVER default to
            # operable on error (that would reintroduce the silent-failure bug).
            return OperabilityResult.UNKNOWN


async def _to_thread[T](fn: Callable[[], T]) -> T:  # pragma: no cover - asyncio shim
    import asyncio

    return await asyncio.to_thread(fn)


def build_operability_probe() -> NumberOperabilityProbe:
    """Live Twilio probe when fully configured, else the hermetic Null probe.

    Requires ``integration_mode == "live"`` + Account SID + API-Key SID + secret (same
    prerequisites as ``build_sms_sender``). Honest degradation to Null otherwise — the
    attach gate then treats every number as NOT_OPERABLE in non-live, which is the safe
    default (no live-looking state without a real check)."""
    from app.config import get_settings

    s = get_settings()
    account_sid = getattr(s, "twilio_account_sid", "") or ""
    api_key_sid = getattr(s, "twilio_api_key_sid", "") or ""
    api_key_secret = getattr(s, "twilio_api_key_secret", "") or ""

    if (
        getattr(s, "integration_mode", "mock") == "live"
        and account_sid
        and api_key_sid
        and api_key_secret
    ):
        return TwilioOperabilityProbe(
            account_sid=account_sid,
            api_key_sid=api_key_sid,
            api_key_secret=api_key_secret,
        )
    return NullOperabilityProbe()
