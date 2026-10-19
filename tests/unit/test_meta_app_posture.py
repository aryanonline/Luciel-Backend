"""The Meta app's boot posture follows the half-provisioned rule (round 7 WP-7).

META_VERIFY_TOKEN was optional although it is load-bearing: Meta's webhook subscription
is saved only after ``GET /api/v1/webhooks/meta`` echoes ``hub.challenge`` for the
matching ``hub.verify_token``, and with the token unset that handshake answers 403 to
everyone. A Meta app provisioned without it connects every WhatsApp/Messenger channel
cleanly and then never receives a message — with nothing on /healthz to say why.

At the same time META_APP_SECRET was required UNCONDITIONALLY, so a deployment that had
not provisioned a Meta app at all could not boot live. Both now follow the rule the
Instagram and Microsoft apps already use: absent entirely = honestly unconfigured;
provisioned = every piece required. The empty-key HMAC hazard that made the secret
unconditional (P1-9) is closed where it lives: an empty key verifies nothing.
"""

from __future__ import annotations

import hashlib
import hmac

import pytest
from app.channels.signatures import verify_hmac_shared_secret
from app.config import get_settings
from app.runtime.integration_status import degraded_bindings
from fastapi.testclient import TestClient

from tests.unit.live_settings import live_settings

VERIFY_TOKEN = "unit-test-verify-token"  # noqa: S105 — test fixture
APP_SECRET = "unit-test-meta-app-secret"  # noqa: S105 — test fixture


# --- boot ------------------------------------------------------------------------ #
def test_a_deployment_without_a_meta_app_boots_live() -> None:
    """No Meta app is a supported posture: the Meta connection reports configured=false
    and the inbound route refuses every signature (empty key)."""
    s = live_settings()
    assert s.meta_app_id == ""
    assert s.meta_app_secret == ""
    assert s.meta_webhook_unconfigured is False


@pytest.mark.parametrize("provisioned", [{"META_APP_ID": "fb-cid"}, {"META_REDIRECT_URI": "x"}])
def test_a_meta_app_without_its_secret_refuses_to_boot(provisioned: dict[str, str]) -> None:
    with pytest.raises(RuntimeError, match="META_APP_SECRET"):
        live_settings(**provisioned, META_VERIFY_TOKEN=VERIFY_TOKEN)


def test_a_meta_app_without_the_verify_token_refuses_to_boot() -> None:
    with pytest.raises(RuntimeError, match="META_VERIFY_TOKEN") as refused:
        live_settings(META_APP_ID="fb-cid", META_APP_SECRET=APP_SECRET)
    # Names only — never a provisioned value.
    assert APP_SECRET not in str(refused.value)


def test_an_instagram_app_without_the_verify_token_refuses_to_boot() -> None:
    """The Instagram app subscribes its webhook to the same handshake."""
    with pytest.raises(RuntimeError, match="META_VERIFY_TOKEN"):
        live_settings(INSTAGRAM_APP_ID="ig-cid", INSTAGRAM_APP_SECRET="ig-secret")


def test_a_fully_provisioned_meta_app_boots() -> None:
    s = live_settings(
        META_APP_ID="fb-cid", META_APP_SECRET=APP_SECRET, META_VERIFY_TOKEN=VERIFY_TOKEN
    )
    assert s.meta_app_configured is True
    assert s.meta_webhook_unconfigured is False


def test_mock_mode_still_boots_half_provisioned() -> None:
    from app.config import Settings

    s = Settings(platform_secret='{"META_APP_ID": "fb-cid"}')
    assert s.meta_webhook_unconfigured is True


# --- /healthz ---------------------------------------------------------------------- #
@pytest.fixture
def fresh_settings():
    get_settings.cache_clear()  # type: ignore[attr-defined]
    yield get_settings()
    get_settings.cache_clear()  # type: ignore[attr-defined]


def test_degraded_bindings_names_a_meta_app_without_a_verify_token(
    monkeypatch: pytest.MonkeyPatch, fresh_settings
) -> None:
    assert "meta_webhook:unconfigured" not in degraded_bindings()

    monkeypatch.setattr(fresh_settings, "meta_app_id", "fb-cid")
    monkeypatch.setattr(fresh_settings, "meta_verify_token", "")
    assert "meta_webhook:unconfigured" in degraded_bindings()

    monkeypatch.setattr(fresh_settings, "meta_verify_token", VERIFY_TOKEN)
    assert "meta_webhook:unconfigured" not in degraded_bindings()


def test_healthz_reports_the_unconfigured_meta_webhook(
    monkeypatch: pytest.MonkeyPatch, fresh_settings
) -> None:
    import app.main as main_module

    async def _db_down() -> str:
        # Keep the probe off Postgres: a "down" database short-circuits the
        # backlog/pgvector reads, and the binding list is computed regardless.
        return "SkippedInUnitTest"

    monkeypatch.setattr(main_module, "database_unreachable_reason", _db_down)
    monkeypatch.setattr(fresh_settings, "meta_app_id", "fb-cid")
    monkeypatch.setattr(fresh_settings, "meta_verify_token", "")

    body = TestClient(main_module.create_app()).get("/healthz").json()
    assert "meta_webhook:unconfigured" in body["degraded"]


# --- the empty HMAC key ------------------------------------------------------------ #
def test_an_empty_hmac_key_verifies_nothing() -> None:
    """With the boot check relaxed, an absent Meta app leaves meta_app_secret empty — and
    an HMAC keyed with "" is one anyone can compute. It must never verify."""
    body = b'{"object": "whatsapp_business_account"}'
    forged = "sha256=" + hmac.new(b"", body, hashlib.sha256).hexdigest()
    assert verify_hmac_shared_secret("", body, forged) is False

    real = "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert verify_hmac_shared_secret(APP_SECRET, body, real) is True
