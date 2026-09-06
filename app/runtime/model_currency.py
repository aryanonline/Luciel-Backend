"""Model-currency watchdog — pinned LLM model IDs verified daily (§3.4.3).

config.py's model bridge made a retirement swap deploy-free ("a platform-secret
rotation + ECS restart, not an image rebuild"), but nothing DETECTED a
retirement: the first sign a pinned model died would be live conversations
quietly riding the cross-provider fallback. This sweep is the detection half —
and, owner decision 2026-08-17, the remediation half for the chat models:

  * every pinned model ID is checked with the provider's per-id RETRIEVE
    (``GET /v1/models/{id}``) — NOT listing membership. Ground truth from the
    first live run (2026-08-17): ``claude-haiku-4-5`` retrieves 200 while the
    LISTING carries only the dated ``claude-haiku-4-5-20251001`` — alias pins
    are valid without appearing in the list, and judging by the list would
    false-alarm (or worse, false-SWAP) on every aliased pin. The paginated
    listing is fetched only when a pin is actually gone, to resolve the
    successor;
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
import json
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from app.config import Settings, get_settings, parse_platform_secret
from app.notifications.email_sender import EmailSender, build_email_sender

logger = logging.getLogger(__name__)

MODEL_CURRENCY_ALERT = "MODEL_CURRENCY_ALERT"
MODEL_CURRENCY_SWAP = "MODEL_CURRENCY_SWAP"
MODEL_CURRENCY_NUDGE = "MODEL_CURRENCY_NUDGE"
# The provider answered a model call with 401/403: the platform's key is revoked or
# rotated, which is a human-needed condition, never a retirement (F133).
MODEL_CURRENCY_KEY_REJECTED = "MODEL_CURRENCY_KEY_REJECTED"
# Platform-secret key holding the last auto-swap, read by app/billing/notices.py to
# show every admin the in-dashboard 'model updated' notice (F114) and by the
# billing_notices sweep to email it once per tenant. Written by the same secret
# rotation that moves the pin, so the record and the change cannot drift apart.
LAST_SWAP_BLOB_KEY = "LLM_MODEL_UPDATED"
_ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
_OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
# The one-turn smoke a successor must pass before it is written anywhere (F113):
# a listed model that cannot complete a trivial turn is not a successor.
SMOKE_PROMPT = "Reply with the single word OK."

# The newer-same-family nudge (audit round 3, Phase 7 — owner concern #6) goes
# out at most on this weekday (Monday), keeping the module stateless (its
# design doctrine) while bounding the noise to weekly. INFO-class: a nudge is
# never an alarm, never an auto-upgrade — the pin moves only by owner decision.
_NUDGE_WEEKDAY = 0

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
    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any: ...

    async def post(
        self,
        url: str,
        *,
        json: Any = None,
        headers: dict[str, str] | None = None,
    ) -> Any: ...

    async def __aenter__(self) -> _Transport: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        traceback: Any = None,
    ) -> None: ...


def _transport_factory() -> _Transport:
    import httpx  # noqa: PLC0415 — lazy: no network dependency unless the live path runs

    return httpx.AsyncClient(timeout=15.0)


async def anthropic_models(client: _Transport, *, api_key: str) -> list[dict[str, Any]]:
    """Every model the Anthropic account can see (paginated, limit 1000/page)."""
    headers = {"x-api-key": api_key, "anthropic-version": _ANTHROPIC_VERSION}
    out: list[dict[str, Any]] = []
    after_id: str | None = None
    while True:
        params: dict[str, Any] = {"limit": 1000}
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


async def openai_models(client: _Transport, *, api_key: str) -> list[dict[str, Any]]:
    resp = await client.get(_OPENAI_MODELS_URL, headers={"Authorization": f"Bearer {api_key}"})
    resp.raise_for_status()
    return list(resp.json().get("data") or [])


async def model_exists(client: _Transport, *, provider: str, api_key: str, model_id: str) -> bool:
    """Per-id ground truth: ``GET /v1/models/{id}`` — 200 means the pin resolves
    (alias or dated id alike), 404 means it is genuinely gone. This is the ONLY
    honest presence check: a valid alias can be absent from the listing (live
    finding 2026-08-17), so listing membership must never decide a retirement."""
    if provider == "anthropic":
        url = f"{_ANTHROPIC_MODELS_URL}/{model_id}"
        headers = {"x-api-key": api_key, "anthropic-version": _ANTHROPIC_VERSION}
    else:
        url = f"{_OPENAI_MODELS_URL}/{model_id}"
        headers = {"Authorization": f"Bearer {api_key}"}
    resp = await client.get(url, headers=headers)
    if getattr(resp, "status_code", None) == 404:
        return False
    resp.raise_for_status()
    return True


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


def _created_epoch(item: dict[str, Any]) -> int:
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


def pick_successor(retired_id: str, available: list[dict[str, Any]]) -> str | None:
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


def _version_tuple(model_id: str) -> tuple[int, ...]:
    """The numeric version behind the family: ``claude-sonnet-4-6`` → (4, 6);
    ``gpt-5.4`` → (4,) (the family already kept the 5); ``claude-haiku-4-5-20251001``
    → (4, 5, 20251001). Empty when the id carries no version at all."""
    family = model_family(model_id)
    rest = model_id[len(family) :] if model_id.startswith(family) else model_id
    return tuple(int(piece) for piece in rest.replace(".", "-").split("-") if piece.isdigit())


def pick_newer_successor(retired_id: str, available: list[dict[str, Any]]) -> str | None:
    """The newest same-family model that is AT LEAST as new as the retired pin, or None.

    ``pick_successor`` answers 'newest in the family', which for a pin that was
    itself the family's newest returns an OLDER sibling — a silent downgrade the
    2026-09-05 audit refused (F113). Two guards, both required when applicable:
    the candidate's version tuple must not be below the pin's, and when the pin
    still has a listing footprint (its dated snapshots) the candidate must have
    been released after it."""
    family = model_family(retired_id)
    pin_version = _version_tuple(retired_id)
    pin_epoch = _pin_epoch(retired_id, available)
    candidates = []
    for item in available:
        candidate = item.get("id")
        if not isinstance(candidate, str) or candidate == retired_id:
            continue
        if model_family(candidate) != family:
            continue
        if pin_version and _version_tuple(candidate)[: len(pin_version)] < pin_version:
            continue  # an older sibling is never a swap target
        if pin_epoch and _created_epoch(item) and _created_epoch(item) < pin_epoch:
            continue
        candidates.append(item)
    if not candidates:
        return None
    best = max(candidates, key=lambda item: (_created_epoch(item), item["id"]))
    return str(best["id"])


def _pin_epoch(model_id: str, available: list[dict[str, Any]]) -> int:
    """When the PINNED model was released, per the listing.

    An alias pin (``claude-sonnet-4-6``) may be absent from the listing while
    its dated snapshots (``claude-sonnet-4-6-20251101``) are present — those
    snapshots ARE the pin for age purposes. 0 = the pin has no listing footprint
    at all, so no honest age comparison is possible."""
    matches = [
        item
        for item in available
        if isinstance(item.get("id"), str)
        and (item["id"] == model_id or item["id"].startswith(model_id + "-"))
    ]
    return max((_created_epoch(item) for item in matches), default=0)


def newer_same_family(model_id: str, available: list[dict[str, Any]]) -> str | None:
    """A same-family model STRICTLY newer than the pin, or None (Phase 7 nudge).

    Strictness is the no-noise rule: when the pin IS the family's newest,
    ``pick_successor`` would still return an older sibling — that is a swap
    candidate for a retirement, not news. And with no listing footprint for the
    pin there is no honest age comparison, so no claim is made."""
    candidate = pick_successor(model_id, available)
    if candidate is None:
        return None
    pin_epoch = _pin_epoch(model_id, available)
    if pin_epoch == 0:
        return None
    candidate_epoch = max(
        (_created_epoch(item) for item in available if item.get("id") == candidate), default=0
    )
    return candidate if candidate_epoch > pin_epoch else None


# --- smoke turn (a successor must answer before it is pinned) ------------------ #

SmokeTurn = Callable[..., Awaitable[str | None]]
"""``await smoke(provider=..., api_key=..., model_id=...)`` → None when the model
completed a trivial turn, else a short reason it did not."""


def _http_status(exc: BaseException) -> int | None:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def _completion_text(provider: str, body: Any) -> str:
    try:
        if provider == "anthropic":
            return "".join(
                str(block.get("text", ""))
                for block in body.get("content", [])
                if isinstance(block, dict)
            ).strip()
        return str(body["choices"][0]["message"]["content"] or "").strip()
    except (AttributeError, KeyError, IndexError, TypeError):
        return ""


async def _default_smoke(
    client: _Transport, *, provider: str, api_key: str, model_id: str
) -> str | None:
    """One real completion against the candidate through the same transport the
    presence checks use. Any non-2xx, transport error or empty answer fails it."""
    if provider == "anthropic":
        url = _ANTHROPIC_MESSAGES_URL
        headers = {
            "x-api-key": api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
    else:
        url = _OPENAI_CHAT_URL
        headers = {"Authorization": f"Bearer {api_key}", "content-type": "application/json"}
    payload = {
        "model": model_id,
        "max_tokens": 8,
        "messages": [{"role": "user", "content": SMOKE_PROMPT}],
    }
    try:
        resp = await client.post(url, json=payload, headers=headers)
        status = getattr(resp, "status_code", 0)
        if not isinstance(status, int) or status >= 400:
            return f"http {status}"
        body = resp.json()
    except Exception as exc:  # noqa: BLE001 — any failure is a failed smoke, never a crash
        return type(exc).__name__
    return None if _completion_text(provider, body) else "empty completion"


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
            client.update_service(cluster=self._cluster, service=service, forceNewDeployment=True)

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
    transport_factory: Callable[[], _Transport] | None = None,
    email_sender: EmailSender | None = None,
    secret_writer: PlatformSecretWriter | None = None,
    restarter: EcsServiceRestarter | None = None,
    smoke: SmokeTurn | None = None,
    now: object | None = None,  # the sweep runner stamps it; gates the weekly nudge
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
    sender = email_sender or build_email_sender()
    operator = settings.platform_email_sender
    checked: list[str] = []
    swapped: list[dict[str, str]] = []
    alerts: list[str] = []
    nudges: list[dict[str, str]] = []
    healthy_chat_pins: list[tuple[ModelPin, str]] = []
    unreachable: set[str] = set()
    # The paginated listing is fetched LAZILY, only to resolve a successor for a
    # pin the retrieve proved gone — presence is never judged by list membership.
    listings: dict[str, list[dict[str, Any]] | None] = {}

    async def _listing_for(client: _Transport, provider: str) -> list[dict[str, Any]] | None:
        if provider not in listings:
            fetch = anthropic_models if provider == "anthropic" else openai_models
            try:
                listings[provider] = await fetch(client, api_key=keys[provider])
            except Exception as exc:  # noqa: BLE001 — unreachable ≠ retired
                logger.warning(
                    "model_currency_provider_unreachable provider=%s error=%s (listing)",
                    provider,
                    type(exc).__name__,
                )
                listings[provider] = None
        return listings[provider]

    async def _smoke_default(*, provider: str, api_key: str, model_id: str) -> str | None:
        return await _default_smoke(client, provider=provider, api_key=api_key, model_id=model_id)

    smoke_fn: SmokeTurn = smoke if smoke is not None else _smoke_default

    async with factory() as client:
        for pin in PINS:
            if pin.provider in unreachable:
                continue  # no verdicts against a provider we could not reach
            model_id = str(getattr(settings, pin.setting))
            try:
                exists = await model_exists(
                    client, provider=pin.provider, api_key=keys[pin.provider], model_id=model_id
                )
            except Exception as exc:  # noqa: BLE001 — unreachable ≠ retired; never false-alarm
                status = _http_status(exc)
                if status in (401, 403):
                    # Not unreachable: the provider answered and refused the key.
                    # Every runtime call against it is failing the same way (F133).
                    logger.error(
                        "%s provider=%s http=%s", MODEL_CURRENCY_KEY_REJECTED, pin.provider, status
                    )
                    alerts.append(
                        f"{pin.provider} rejected the platform's API key (HTTP {status}) — "
                        f"revoked or rotated? Every {pin.provider} call fails until the "
                        "key in the platform secret is replaced"
                    )
                    unreachable.add(pin.provider)
                    continue
                logger.warning(
                    "model_currency_provider_unreachable provider=%s error=%s",
                    pin.provider,
                    type(exc).__name__,
                )
                unreachable.add(pin.provider)
                continue
            if exists:
                checked.append(pin.setting)
                if pin.swappable:
                    healthy_chat_pins.append((pin, model_id))
                continue

            # The pin is GONE at the provider (a real 404, not a listing quirk).
            # Remediate if this pin allows it.
            env_pinned = bool(os.environ.get(pin.env_var))
            listing = await _listing_for(client, pin.provider) if pin.swappable else None
            successor = pick_newer_successor(model_id, listing) if listing else None
            writer = (
                secret_writer if secret_writer is not None else _default_secret_writer(settings)
            )

            if pin.swappable and successor and not env_pinned and writer is not None:
                # Validate BEFORE writing: the pin only moves to a model that has
                # just answered a turn with the same key the runtime will use.
                failure = await smoke_fn(
                    provider=pin.provider, api_key=keys[pin.provider], model_id=successor
                )
                if failure is not None:
                    logger.warning(
                        "model_currency_smoke_failed setting=%s candidate=%s reason=%s",
                        pin.setting,
                        successor,
                        failure,
                    )
                    alerts.append(
                        f"{pin.setting} = '{model_id}' retired; successor '{successor}' found "
                        f"but failed a smoke turn ({failure}) — not swapped"
                    )
                    continue
                swapped_at = now if isinstance(now, datetime) else datetime.now(UTC)
                swap_record = {
                    "at": swapped_at.isoformat(),
                    "setting": pin.setting,
                    "from": model_id,
                    "to": successor,
                }
                try:
                    await writer.apply(
                        {pin.blob_key: successor, LAST_SWAP_BLOB_KEY: json.dumps(swap_record)}
                    )
                except Exception as exc:  # noqa: BLE001 — a failed write alerts, not crashes
                    logger.warning(
                        "model_currency_secret_write_failed error=%s", type(exc).__name__
                    )
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
                    except Exception as exc:  # noqa: BLE001 — swap persisted; recoverable
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
                            f"The pinned model for {pin.setting} — '{model_id}' — is no "
                            f"longer offered by {pin.provider}. The platform swapped it to "
                            f"the newest same-family model, '{successor}', after it answered "
                            "a smoke turn, wrote it to the platform secret, "
                            + (
                                "and restarted the app services."
                                if restart_note == "restarted"
                                else f"but could not restart the app services ({restart_note}) "
                                "— restart them when convenient."
                            )
                            + "\n\nNo action is required; review the choice at your leisure. "
                            "The pin is never changed while it still exists — this happens "
                            "only on a provider retirement (§3.4.3). Every account sees a "
                            "'model updated' notice on its next login and receives it by "
                            "email once, from the billing_notices sweep."
                        ),
                    )
                swapped.append({"setting": pin.setting, "old": model_id, "new": successor})
                continue

            reason = (
                "the embedding model is never auto-swapped (stored vectors are "
                "model-specific). To move: set LUCIEL_KNOWLEDGE_EMBEDDING_MODEL, redeploy, "
                "and the knowledge_sync sweep handles the re-embedding of every tenant's "
                "chunks whose recorded embedding_model differs, in bounded batches (F121)"
                if not pin.swappable
                else "pinned by env var, which outranks the platform secret"
                if env_pinned
                else "the provider's model list is unavailable to resolve a successor"
                if pin.swappable and listing is None
                else "no same-family successor at least as new as the retired pin exists "
                "at the provider (an older sibling is never a swap target)"
                if successor is None
                else "no platform-secret writer is configured in this environment"
            )
            alerts.append(f"{pin.setting} = '{model_id}' is gone from {pin.provider}; {reason}")

        # --- newer-same-family nudge (Phase 7, owner concern #6) --------------- #
        # A HEALTHY pin with a strictly newer family member gets a weekly INFO
        # email — awareness, never an auto-upgrade (the pin moves only by owner
        # decision; only a provider RETIREMENT auto-swaps, above). Weekly via the
        # weekday gate keeps the module stateless and the mailbox quiet. Gated on
        # an EXPLICIT datetime: the sweep wrapper stamps the real date; direct
        # calls without one (the retirement-check tests) never nudge, so the
        # listing-is-lazy contract holds for them unchanged.
        from datetime import datetime as _datetime  # noqa: PLC0415

        if isinstance(now, _datetime) and now.weekday() == _NUDGE_WEEKDAY:
            for pin, model_id in healthy_chat_pins:
                if pin.provider in unreachable:
                    continue
                listing = await _listing_for(client, pin.provider)
                if not listing:
                    continue
                newer = newer_same_family(model_id, listing)
                if newer:
                    logger.info(
                        "%s setting=%s current=%s newer=%s",
                        MODEL_CURRENCY_NUDGE,
                        pin.setting,
                        model_id,
                        newer,
                    )
                    nudges.append({"setting": pin.setting, "current": model_id, "newer": newer})
            if nudges and operator:
                lines = "\n".join(
                    f"- {n['setting']}: pinned '{n['current']}', newest in family '{n['newer']}'"
                    for n in nudges
                )
                await _send(
                    sender,
                    to=operator,
                    subject=(
                        f"Luciel model check ({settings.environment}): a newer model "
                        "in the same family is available"
                    ),
                    body=(
                        f"{lines}\n\n"
                        "Nothing changed and nothing is wrong — your pinned models all "
                        "work. This is the weekly §3.4.3 heads-up that the provider now "
                        "offers a newer model in the same family. To move, set the LLM_* "
                        "key in the platform secret and restart the app services (the "
                        "deploy-free swap); the pin is never upgraded automatically."
                    ),
                )

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
        "nudges": nudges,
        "unreachable": sorted(unreachable | {p for p, v in listings.items() if v is None}),
    }
