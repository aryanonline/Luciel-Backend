"""SecretStore — swappable credential storage for connections (Architecture §3.8.3).

The §3.8.3 contract is absolute: **secret material never touches Postgres.** Only
the opaque ``secret_ref`` pointer lives in ``instance_connections.secret_ref``; the
credential itself (OAuth access/refresh tokens, API keys, webhook auth headers)
lives behind this interface. Production binds the AWS Secrets Manager / SSM
SecureString backend; everything goes through ``SecretStore`` so the API, health
worker, and sync layer never see a concrete backend.

The ``secret_ref`` convention mirrors the §3.8.3 tenant-isolation path
``vm/{admin_id}/{instance_id}/{connection_type}`` so that — even under the AWS
backend — a request for another tenant's secret targets a path outside the
requesting tenant's IAM boundary (§3.8.3 rule 5).

AWS SEAM (GATE 2 cutover): the real ``SecretsManagerStore`` (boto3 + KMS-encrypted,
least-privilege IAM per §3.8.3 rules 2–3) implements this same Protocol and is bound
by ``get_secret_store`` ONLY when ``integration_mode == "live"``. The default
(``mock``) mode and all tests continue to use :class:`LocalSecretStore`; boto3 is
imported lazily so CI (no AWS) imports this module cleanly and makes no AWS call.
"""

from __future__ import annotations

import json
import threading
import uuid
from typing import Protocol, runtime_checkable


def secret_ref_for(admin_id: uuid.UUID, instance_id: uuid.UUID, connection_type: str) -> str:
    """Build the opaque pointer (§3.8.3 path convention).

    The same shape the AWS backend uses for its ARN/SSM key prefix, so tenant
    isolation holds independent of which backend is bound (§3.8.3 rule 5)."""
    return f"vm/{admin_id}/{instance_id}/{connection_type}"


def tenant_secret_prefix(admin_id: uuid.UUID) -> str:
    """Every secret this tenant can own lives under this prefix (§3.8.3 rule 5).

    Deleting a tenant by walking ``instance_connections.secret_ref`` only reaches secrets
    a surviving row still points at. A row deleted earlier, or re-keyed to a different
    connection type, leaves its credential behind in the backend with nothing left in
    Postgres to name it — so tenant deletion sweeps the prefix instead (P0-9).
    """
    return f"vm/{admin_id}/"


@runtime_checkable
class SecretStore(Protocol):
    """Put/get/delete a credential by opaque ref (§3.8.3).

    Implementations MUST NOT return secret material through any API surface — they
    are server-side only, consumed by the health/refresh worker and connect flow."""

    async def put(self, secret_ref: str, material: dict[str, str]) -> None: ...

    async def get(self, secret_ref: str) -> dict[str, str] | None: ...

    async def delete(self, secret_ref: str) -> bool:
        """Delete by ref. Idempotent: deleting an absent ref returns False, not an
        error (§3.6.3 cleanup pattern — a revoke that runs twice must not fault)."""
        ...

    async def delete_prefix(self, prefix: str) -> int:
        """Delete every secret whose ref starts with ``prefix``; return how many.

        Used by tenant deletion to reach credentials no surviving row points at.
        Idempotent for the same reason ``delete`` is."""
        ...


class LocalSecretStore:
    """In-process, deterministic SecretStore for dev/test (§3.8.3 local backend).

    Holds material in a process-global dict so the connect flow, the health worker,
    and the revoke path share one view within a test. This is the seam the AWS
    Secrets Manager backend replaces 1:1 — NEVER used in production. Secret material
    held here is never serialized to the API, the audit log, or the export (§3.8.3
    rule 4); the secret-hygiene test asserts this."""

    # Process-global so independent sessions in one test see the same store; a real
    # backend is naturally shared (it's a remote service).
    _store: dict[str, dict[str, str]] = {}
    _lock = threading.Lock()

    async def put(self, secret_ref: str, material: dict[str, str]) -> None:
        with self._lock:
            type(self)._store[secret_ref] = dict(material)

    async def get(self, secret_ref: str) -> dict[str, str] | None:
        with self._lock:
            found = type(self)._store.get(secret_ref)
            return dict(found) if found is not None else None

    async def delete(self, secret_ref: str) -> bool:
        with self._lock:
            return type(self)._store.pop(secret_ref, None) is not None

    async def delete_prefix(self, prefix: str) -> int:
        with self._lock:
            store = type(self)._store
            doomed = [ref for ref in store if ref.startswith(prefix)]
            for ref in doomed:
                del store[ref]
            return len(doomed)

    @classmethod
    def reset(cls) -> None:
        """Test hook: clear the process-global store between tests."""
        with cls._lock:
            cls._store.clear()


class SecretsManagerStore:
    """AWS Secrets Manager backend implementing the ``SecretStore`` Protocol (§3.8.3).

    Binds the real backend behind the same Protocol ``LocalSecretStore`` satisfies, so
    the connect flow / health worker / revoke path are unchanged. Material is stored as
    a JSON ``SecretString`` per ``secret_ref``; KMS encryption is handled by the
    secret's own associated key (§3.8.3 rules 2–3) — no explicit KMS API call is made.
    The ``secret_ref`` path convention (``vm/{admin_id}/{instance_id}/{type}``) doubles
    as the secret name so least-privilege IAM scopes to a tenant prefix (§3.8.3 rule 5).

    boto3 is imported LAZILY so CI (no AWS, boto3 may be absent) still imports this
    module and runs the LocalSecretStore tests. Only selected in ``live`` mode by
    ``get_secret_store``; never the default."""

    def __init__(self, *, region_name: str) -> None:
        self._region_name = region_name
        self._client: object | None = None

    def _get_client(self) -> object:
        if self._client is None:
            import boto3  # noqa: PLC0415 — lazy/guarded import (CI has no AWS)

            self._client = boto3.client("secretsmanager", region_name=self._region_name)
        return self._client

    async def put(self, secret_ref: str, material: dict[str, str]) -> None:
        client = self._get_client()
        payload = json.dumps(dict(material))
        # Create-or-update: PutSecretValue requires the secret to exist, so create on
        # first write and fall back to a value update on subsequent writes.
        try:
            client.create_secret(Name=secret_ref, SecretString=payload)  # type: ignore[attr-defined]
        except client.exceptions.ResourceExistsException:  # type: ignore[attr-defined]
            client.put_secret_value(SecretId=secret_ref, SecretString=payload)  # type: ignore[attr-defined]

    async def get(self, secret_ref: str) -> dict[str, str] | None:
        client = self._get_client()
        try:
            resp = client.get_secret_value(SecretId=secret_ref)  # type: ignore[attr-defined]
        except client.exceptions.ResourceNotFoundException:  # type: ignore[attr-defined]
            return None
        raw = resp.get("SecretString")
        if raw is None:
            return None
        decoded = json.loads(raw)
        return {str(k): str(v) for k, v in decoded.items()}

    async def delete(self, secret_ref: str) -> bool:
        client = self._get_client()
        try:
            client.delete_secret(  # type: ignore[attr-defined]
                SecretId=secret_ref, ForceDeleteWithoutRecovery=True
            )
        except client.exceptions.ResourceNotFoundException:  # type: ignore[attr-defined]
            # Idempotent revoke: deleting an absent ref returns False, not an error
            # (§3.6.3 cleanup pattern — a revoke that runs twice must not fault).
            return False
        return True

    async def delete_prefix(self, prefix: str) -> int:
        client = self._get_client()
        paginator = client.get_paginator("list_secrets")  # type: ignore[attr-defined]
        # ListSecrets has no prefix operator, only "begins-with on any word", so the
        # server-side filter narrows the page set and the exact prefix is re-checked
        # here. Deleting on a looser match than the caller asked for would cross the
        # tenant boundary this prefix exists to draw.
        refs = [
            name
            for page in paginator.paginate(
                Filters=[{"Key": "name", "Values": [prefix.rstrip("/")]}]
            )
            for secret in page.get("SecretList", [])
            if (name := secret.get("Name", "")).startswith(prefix)
        ]
        deleted = 0
        for ref in refs:
            if await self.delete(ref):
                deleted += 1
        return deleted


_default: SecretStore | None = None


def get_secret_store() -> SecretStore:
    """Return the bound SecretStore. Defaults to the local backend (§3.8.3).

    In ``live`` integration mode this binds the AWS ``SecretsManagerStore``; in every
    other mode (the default ``mock``) it stays ``LocalSecretStore`` so all existing
    tests are unaffected. The connect flow / worker depend only on the Protocol."""
    global _default
    if _default is None:
        from app.config import get_settings  # noqa: PLC0415 — avoid import cycle

        settings = get_settings()
        if settings.integration_mode == "live":
            _default = SecretsManagerStore(region_name=settings.aws_region)
        else:
            _default = LocalSecretStore()
    return _default
