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

AWS-DEFERRED SEAM (binds later on explicit owner approval): the real
``SecretsManagerStore`` (boto3 + KMS-encrypted, least-privilege IAM per §3.8.3
rules 2–3) implements this same Protocol. NO boto3 / real Secrets Manager call is
made in this arc — the owner instruction is "local fully-tested system first, infra
on explicit approval." Tests use :class:`LocalSecretStore`.
"""

from __future__ import annotations

import threading
import uuid
from typing import Protocol, runtime_checkable


def secret_ref_for(admin_id: uuid.UUID, instance_id: uuid.UUID, connection_type: str) -> str:
    """Build the opaque pointer (§3.8.3 path convention).

    The same shape the AWS backend uses for its ARN/SSM key prefix, so tenant
    isolation holds independent of which backend is bound (§3.8.3 rule 5)."""
    return f"vm/{admin_id}/{instance_id}/{connection_type}"


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

    @classmethod
    def reset(cls) -> None:
        """Test hook: clear the process-global store between tests."""
        with cls._lock:
            cls._store.clear()


_default: SecretStore | None = None


def get_secret_store() -> SecretStore:
    """Return the bound SecretStore. Defaults to the local backend (§3.8.3).

    Production swaps in ``SecretsManagerStore`` behind this same seam, on explicit
    owner approval — the connect flow / worker depend only on the Protocol."""
    global _default
    if _default is None:
        _default = LocalSecretStore()
    return _default
