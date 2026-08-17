"""Model-currency watchdog — pinned LLM model IDs verified daily (§3.4.3).

config.py's model bridge made a retirement swap deploy-free ("a platform-secret
rotation + ECS restart, not an image rebuild"), but nothing DETECTED a
retirement: the first sign a pinned model died would be live conversations
quietly riding the cross-provider fallback. This sweep is the detection half —
and, owner decision 2026-08-17, the remediation half for the chat models:

  * every pinned model ID is checked against its provider's live model list
    (Anthropic ``GET /v1/models`` paginated; OpenAI ``GET /v1/models``);
  * a CHAT pin that has disappeared is AUTO-SWAPPED to the newest same-family
    successor (``claude-sonnet-4-6`` → the newest ``claude-sonnet-*``): the new
    ID is written into the platform secret, the app services are restarted to
    pick it up, and the operator is emailed "swapped X → Y" — notification of
    an action taken, not a request for one. The pin is never touched while it
    still exists (no surprise upgrades);
  * the EMBEDDING model is ALERT-ONLY forever: stored knowledge vectors are
    model-specific, so an embedding swap is a re-embedding migration that must
    never happen unattended;
  * when remediation is impossible (no same-family successor, the pin is held
    by an env var that outranks the secret, or the write/restart failed) the
    sweep logs ``MODEL_CURRENCY_ALERT`` at ERROR, emails the operator, and
    RAISES — the nonzero exit plus the ERROR line feed the sweep log group's
    ``SweepErrors`` metric filter and its alarm.

Severity split, deliberately: a provider we cannot REACH is a WARNING and never
an alert — an unreachable models endpoint is not a retired model, and a false
retirement alarm (or worse, a false swap) is the failure mode this design
guards hardest against. Tomorrow's run is the retry.

Statelessness is the idempotence: a successful swap satisfies tomorrow's check;
an unresolved alert repeats daily until fixed, which for an active outage risk
is the honest cadence, not noise.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Protocol

from app.config import Settings, get_settings, parse_platform_secret
from app.notifications.email_sender import EmailSender, build_email_sender

logger = logging.getLogger(__name__)

MODEL_CURRENCY_ALERT = "MODEL_CURRENCY_ALERT"
MODEL_CURRENCY_SWAP = "MODEL_CURRENCY_SWAP"

_ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models"
_OPENAI_MODELS_URL = "https://api.openai.com/v1/models"
_ANTHROPIC_VERSION = "2023-06-01"


@dataclass(frozen=True)
class ModelPin:
    """One configured model ID and how (whether) it can be remediated."""

    setting: str  # Settings attribute holding the model ID
    blob_key: str  # platform-secret key an auto-swap writes (config.py bridge)
    env_var: str  # env var that OUTRANKS the blob — set means we cannot remediate
    provider: str  # "anthropic" | "openai"
    # False = alert-only. The embedding model is never swapped: stored knowledge
    # vectors are model-specific, so a swap silently breaks retrieval until a
    # full re-embed — a migration, not a remediation.
    swappable: bool


PINS: tuple[ModelPin, ...] = (
    ModelPin(
        "llm_anthropic_primary_model",
        "LLM_ANTHROPIC_PRIMARY_MODEL",
        "LUCIEL_LLM_ANTHROPIC_PRIMARY_MODEL",
        "anthropic",
        True,
    ),
    ModelPin(
        "llm_anthropic_fast_model",
        "LLM_ANTHROPIC_FAST_MODEL",
        "LUCIEL_LLM_ANTHROPIC_FAST_MODEL",
        "anthropic",
        True,
    ),
    ModelPin(
        "llm_openai_fallback_model",
        "LLM_OPENAI_FALLBACK_MODEL",
        "LUCIEL_LLM_OPENAI_FALLBACK_MODEL",
        "openai",
        True,
    ),
    ModelPin(
        "knowledge_embedding_model",
        "KNOWLEDGE_EMBEDDING_MODEL",  # informational — not bridged, never written
        "LUCIEL_KNOWLEDGE_EMBEDDING_MODEL",
        "openai",
        False,
    ),
)


# --- HTTP seam (the refresh.py idiom: duck-typed transport, lazy httpx) -------- #


class _Transport(Protocol):  # pragma: no cover - structural typing only
    async def get(self, url: str, *, params: dict | None = None, headers: dict | None = None): ...

    async def __aenter__(self) -> _Transport: ...

    async def __aexit__(self, *exc: object) -> None: ...


def _transport_factory() -> _Transport:
    import httpx  # noqa: PLC0415 — lazy: no network dependency unless the live path runs

    return httpx.AsyncClient(timeout=15.0)


async def anthropic_models(client: _Transport, *, api_key: str) -> list[dict]:
    """Every model the Anthropic account can see (paginated, limit 1000/page)."""
    headers = {"x-api-key": api_key, "anthropic-version": _ANTHROPIC_VERSION}
    out: list[dict] = []
    after_id: str | None = None
    while True:
        params: dict = {"limit": 1000}
        if after_id:
            params["after_id"] = after_id
        resp = await client.get(_ANTHROPIC_MODELS_URL, params=params, headers=headers)
        resp.raise_for_status()
        payload = resp.json()
        items = list(payload.get("data") or [])
        out.extend(items)
        if not payload.get("has_more") or not items:
            return out
        after_id = str(items[-1].get("id"))


async def openai_models(client: _Transport, *, api_key: str) -> list[dict]:
    resp = await client.get(
        _OPENAI_MODELS_URL, headers={"Authorization": f"Bearer {api_key}"}
    )
    resp.raise_for_status()
    return list(resp.json().get("data") or [])


# --- successor resolution ------------------------------------------------------ #


def model_family(model_id: str) -> str:
    """The family a version-suffixed model ID belongs to.

    Trailing PURELY-numeric segments are version/date suffixes
    (``claude-sonnet-4-6`` → ``claude-sonnet``; ``claude-haiku-4-5-20251001`` →
    ``claude-haiku``). A dotted final segment keeps its integer part as family
    identity (``gpt-5.4`` → ``gpt-5``), so a retired 5.4 can move to 5.6 but
    never silently to a 6-class model. A non-numeric segment (``4o``) is part of
    the name and stays."""
    parts = model_id.split("-")
    while len(parts) > 1 and parts[-1].isdigit():
        parts.pop()
    last = parts[-1]
    if "." in last:
        head, _, tail = last.partition(".")
        if head.isdigit() and tail.replace(".", "").isdigit():
            parts[-1] = head
    return "-".join(parts)


def _created_epoch(item: dict) -> int:
    created = item.get("created")
    if isinstance(created, (int, float)):
        return int(created)
    created_at = item.get("created_at")
    if isinstance(created_at, str):
        from datetime import datetime  # noqa: PLC0415

        try:
            return int(datetime.fromisoformat(created_at.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return 0
    return 0


def pick_successor(retired_id: str, available: list[dict]) -> str | None:
    """The newest same-family model, or None (⇒ alert-only, never a cross-family guess)."""
    family = model_family(retired_id)
    candidates = [
        item
        for item in available
        if isinstance(item.get("id"), str)
        and item["id"] != retired_id
        and model_family(item["id"]) == family
    ]
    if not candidates:
        return None
    best = max(candidates, key=lambda item: (_created_epoch(item), item["id"]))
    return str(best["id"])


# --- remediation seams (secret write + service restart) ------------------------ #


class PlatformSecretWriter(Protocol):
    async def apply(self, updates: dict[str, str]) -> None:
        """Merge ``updates`` into the platform-secret JSON blob."""
        ...


class SecretsManagerPlatformSecretWriter:
    """Read-merge-write on the platform secret. boto3 is lazy; runs off-loop."""

    def __init__(self, *, secret_arn: str, region_name: str) -> None:
        self._secret_arn = secret_arn
        self._region_name = region_name

    def _apply_sync(self, updates: dict[str, str]) -> None:
        import json  # noqa: PLC0415

        import boto3  # noqa: PLC0415 — lazy/guarded import (CI has no AWS)

        client = boto3.client("secretsmanager", region_name=self._region_name)
        current = client.get_secret_value(SecretId=self._secret_arn).get("SecretString") or "{}"
        blob = json.loads(current)
        blob.update(updates)
        client.put_secret_value(SecretId=self._secret_arn, SecretString=json.dumps(blob))

    async def apply(self, updates: dict[str, str]) -> None:
        await asyncio.to_thread(self._apply_sync, updates)


class EcsServiceRestarter(Protocol):
    async def restart(self) -> None: ...


class BotoEcsServiceRestarter:
    """forceNewDeployment on the app services so fresh tasks read the new blob."""

    def __init__(self, *, cluster: str, services: list[str], region_name: str) -> None:
        self._cluster = cluster
        self._services = services
        self._region_name = region_name

    def _restart_sync(self) -> None:
        import boto3  # noqa: PLC0415 — lazy/guarded import

        client = boto3.client("ecs", region_name=self._region_name)
        for service in self._services:
            client.update_service(
                cluster=self._cluster, service=service, forceNewDeployment=True
            )

    async def restart(self) -> None:
        await asyncio.to_thread(self._restart_sync)


def _default_secret_writer(settings: Settings) -> PlatformSecretWriter | None:
    arn = os.environ.get("LUCIEL_PLATFORM_SECRET_ARN", "")
    if not arn:
        return None
    return SecretsManagerPlatformSecretWriter(secret_arn=arn, region_name=settings.aws_region)


def _default_restarter(settings: Settings) -> EcsServiceRestarter | None:
    cluster = os.environ.get("LUCIEL_ECS_CLUSTER", "")
    services = [s for s in os.environ.get("LUCIEL_ECS_SERVICES", "").split(",") if s]
    if not cluster or not services:
        return None
    return BotoEcsServiceRestarter(
        cluster=cluster, services=services, region_name=settings.aws_region
    )


# --- the check ----------------------------------------------------------------- #


async def _send(sender: EmailSender, *, to: str, subject: str, body: str) -> None:
    try:
        await sender.send(to=to, subject=subject, body_text=body)
    except Exception as exc:  # noqa: BLE001 — the alert path must not die on email
        # The ERROR line + nonzero exit still reach the metric-filter alarm.
        logger.warning("model_currency_email_failed error=%s", type(exc).__name__)


_REMEDY = (
    "Remedy: set the LLM_* key in the platform secret (or the LUCIEL_LLM_* env var, "
    "which outranks it; LUCIEL_KNOWLEDGE_EMBEDDING_MODEL for the embedder) to a "
    "current model ID, then restart the ECS services — the §3.4.3 deploy-free swap. "
    "This alert repeats daily until resolved."
)


async def run_model_currency_check(
    *,
    settings: Settings | None = None,
    transport_factory=None,
    email_sender: EmailSender | None = None,
    secret_writer: PlatformSecretWriter | None = None,
    restarter: EcsServiceRestarter | None = None,
    now: object | None = None,  # accepted for sweep-runner symmetry; unused
) -> dict[str, object]:
    """Verify every pinned model; auto-swap retired chat pins; alert what remains.

    Raises (⇒ sweep exits nonzero ⇒ alarm) iff something needs a human: a pin is
    gone and could not be safely remediated. A completed auto-swap does NOT raise
    — the condition is fixed and the operator was told."""
    settings = settings or get_settings()
    if settings.integration_mode != "live":
        # Mock mode has no provider keys and nothing real to verify.
        return {"skipped": "mock"}

    secret = parse_platform_secret(settings.platform_secret, live=True)
    keys = {
        "anthropic": secret.get("ANTHROPIC_API_KEY", ""),
        "openai": secret.get("OPENAI_API_KEY", ""),
    }
    missing_keys = sorted(name for name, value in keys.items() if not value)
    if missing_keys:
        # Misconfiguration, not unreachability: the check CANNOT run, loudly.
        logger.error(
            "%s cannot verify model pins: platform secret missing the %s API key(s)",
            MODEL_CURRENCY_ALERT,
            ", ".join(missing_keys),
        )
        raise RuntimeError(f"model_currency: missing provider keys: {missing_keys}")

    factory = transport_factory or _transport_factory
    listings: dict[str, list[dict] | None] = {}
    async with factory() as client:
        for provider, fetch in (("anthropic", anthropic_models), ("openai", openai_models)):
            try:
                listings[provider] = await fetch(client, api_key=keys[provider])
            except Exception as exc:  # noqa: BLE001 — unreachable ≠ retired; never false-alarm
                logger.warning(
                    "model_currency_provider_unreachable provider=%s error=%s",
                    provider,
                    type(exc).__name__,
                )
                listings[provider] = None

    sender = email_sender or build_email_sender()
    operator = settings.platform_email_sender
    checked: list[str] = []
    swapped: list[dict[str, str]] = []
    alerts: list[str] = []
    unreachable = sorted(p for p, listing in listings.items() if listing is None)

    for pin in PINS:
        listing = listings[pin.provider]
        if listing is None:
            continue  # provider unreachable this run — no verdict, no false alarm
        model_id = str(getattr(settings, pin.setting))
        ids = {item.get("id") for item in listing}
        if model_id in ids:
            checked.append(pin.setting)
            continue

        # The pin is GONE at the provider. Remediate if this pin allows it.
        env_pinned = bool(os.environ.get(pin.env_var))
        successor = pick_successor(model_id, listing) if pin.swappable else None
        writer = secret_writer if secret_writer is not None else _default_secret_writer(settings)

        if pin.swappable and successor and not env_pinned and writer is not None:
            try:
                await writer.apply({pin.blob_key: successor})
            except Exception as exc:  # noqa: BLE001 — a failed write is an alert, not a crash
                logger.warning("model_currency_secret_write_failed error=%s", type(exc).__name__)
                alerts.append(
                    f"{pin.setting} = '{model_id}' retired; successor '{successor}' found "
                    f"but the platform-secret write failed"
                )
                continue
            restart_note = "restarted"
            restart = restarter if restarter is not None else _default_restarter(settings)
            if restart is None:
                restart_note = "restart_skipped_no_service_env"
            else:
                try:
                    await restart.restart()
                except Exception as exc:  # noqa: BLE001 — swap persisted; restart is recoverable
                    logger.warning("model_currency_restart_failed error=%s", type(exc).__name__)
                    restart_note = "restart_failed"
            logger.warning(
                "%s setting=%s old=%s new=%s services=%s",
                MODEL_CURRENCY_SWAP,
                pin.setting,
                model_id,
                successor,
                restart_note,
            )
            if operator:
                await _send(
                    sender,
                    to=operator,
                    subject=(
                        f"Luciel swapped a retired model automatically "
                        f"({settings.environment}): {model_id} → {successor}"
                    ),
                    body=(
                        f"The pinned model for {pin.setting} — '{model_id}' — is no longer "
                        f"offered by {pin.provider}. The platform swapped it to the newest "
                        f"same-family model, '{successor}', wrote it to the platform secret, "
                        + (
                            "and restarted the app services."
                            if restart_note == "restarted"
                            else f"but could not restart the app services ({restart_note}) "
                            "— restart them when convenient."
                        )
                        + "\n\nNo action is required; review the choice at your leisure. The "
                        "pin is never changed while it still exists — this happens only on "
                        "a provider retirement (§3.4.3)."
                    ),
                )
            swapped.append({"setting": pin.setting, "old": model_id, "new": successor})
            continue

        reason = (
            "the embedding model is never auto-swapped (stored vectors are "
            "model-specific — re-embedding is a migration)"
            if not pin.swappable
            else "pinned by env var, which outranks the platform secret"
            if env_pinned
            else "no same-family successor exists at the provider"
            if successor is None
            else "no platform-secret writer is configured in this environment"
        )
        alerts.append(f"{pin.setting} = '{model_id}' is gone from {pin.provider}; {reason}")

    if alerts:
        logger.error("%s %s", MODEL_CURRENCY_ALERT, " | ".join(alerts))
        if operator:
            await _send(
                sender,
                to=operator,
                subject=(
                    f"Luciel model-currency alert ({settings.environment}): "
                    f"{len(alerts)} pinned model(s) need attention"
                ),
                body="\n".join(f"- {a}" for a in alerts) + f"\n\n{_REMEDY}",
            )
        raise RuntimeError(f"model_currency: pinned models missing: {len(alerts)}")

    return {
        "checked": checked,
        "swapped": swapped,
        "unreachable": unreachable,
    }
