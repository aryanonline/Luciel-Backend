"""Three defaults that fail quietly in the customer's disfavour (BE P2-5, P2-11, P2-12).

None of these is exploitable on its own; all three share a shape. Each one turns a
missing input into a plausible-looking answer instead of an error:

  * P2-5  — ``has_payment_method=False`` caps a PAYG tenant who HAS a card. Refusing a
            paying customer is indistinguishable from them being out of allowance.
  * P2-11 — a ``==`` compare on the Meta verify token leaks its prefix by timing.
  * P2-12 — a malformed platform secret parsed to ``{}`` disables every platform
            credential at once, and every downstream seam reads that as "not
            configured" and degrades past it silently.
"""

from __future__ import annotations

import inspect

import pytest
from app.config import PlatformSecretError, Settings, parse_platform_secret


# --- P2-5: no safe default for card state ---------------------------------------- #
async def test_orchestrator_requires_an_explicit_card_state():
    from app.runtime.orchestrator import Orchestrator

    field = inspect.signature(Orchestrator).parameters["has_payment_method"]
    assert field.default is inspect.Parameter.empty
    assert field.kind is inspect.Parameter.KEYWORD_ONLY


async def test_voice_handle_turn_requires_an_explicit_card_state():
    from app.runtime.voice_pipeline import handle_turn

    field = inspect.signature(handle_turn).parameters["has_payment_method"]
    assert field.default is inspect.Parameter.empty
    assert field.kind is inspect.Parameter.KEYWORD_ONLY


async def test_every_production_caller_resolves_the_real_card_state():
    """The kwarg being required is only half the fix — it must not be satisfied with a
    literal. Both runtime entries have to ask metering for the tenant's actual card."""
    from app.api.v1 import chat_widget
    from app.channels import runtime_entry

    for mod in (runtime_entry, chat_widget):
        src = inspect.getsource(mod)
        assert "has_payment_method=await metering.has_live_card(" in src
        assert "has_payment_method=False" not in src


# --- P2-11: constant-time verify-token compare ------------------------------------ #
async def test_meta_verify_token_is_compared_in_constant_time():
    from app.api.v1.webhooks import channels

    src = inspect.getsource(channels.meta_verify)
    assert "compare_digest" in src
    assert "token == settings.meta_verify_token" not in src


@pytest.mark.parametrize("token", ["", None])
async def test_meta_verify_rejects_an_absent_token_even_with_an_unset_secret(token, monkeypatch):
    """An unset verify token must not make an omitted parameter match. compare_digest
    would happily match "" against "" — the truthiness guard is what prevents it."""
    from app.api.v1.webhooks import channels

    monkeypatch.setattr(channels, "get_settings", lambda: Settings(meta_verify_token=""))

    class Req:
        query_params = {
            k: v
            for k, v in {
                "hub.mode": "subscribe",
                "hub.verify_token": token,
                "hub.challenge": "c",
            }.items()
            if v is not None
        }

    resp = await channels.meta_verify(Req())
    assert resp.status_code == 403


# --- P2-12: a secret we cannot read is not a secret that is absent ---------------- #
@pytest.mark.parametrize("raw", ["not-json", '{"truncated": ', "[1, 2, 3]", '"a string"'])
def test_malformed_platform_secret_is_fatal_in_live_mode(raw):
    with pytest.raises(PlatformSecretError):
        parse_platform_secret(raw, live=True)


@pytest.mark.parametrize("raw", ["not-json", '{"truncated": ', "[1, 2, 3]"])
def test_malformed_platform_secret_still_degrades_in_mock_mode(raw):
    """Mock/CI must stay hermetic: there is nothing to fail closed about when no real
    credential was ever expected."""
    assert parse_platform_secret(raw) == {}


def test_an_absent_platform_secret_is_not_an_error_in_live_mode():
    """Absent is a different condition from corrupt, and it already has its own
    fail-closed guards (JWT key, Stripe secret, HMAC keys). This one must not
    pre-empt them with a less specific error."""
    assert parse_platform_secret("", live=True) == {}
    assert parse_platform_secret("{}", live=True) == {}


def test_live_boot_refuses_a_corrupt_platform_secret():
    """End to end: Settings construction is where a live deploy finds out."""
    with pytest.raises(PlatformSecretError):
        Settings(integration_mode="live", platform_secret="{oops")
