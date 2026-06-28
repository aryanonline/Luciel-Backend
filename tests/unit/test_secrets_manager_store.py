"""Unit tests for SecretsManagerStore using the botocore Stubber (Architecture §3.8.3).

Hermetic: no real AWS. A botocore Stubber intercepts every API call. We assert the
store satisfies the SecretStore Protocol contract — put (create + update), get (hit +
miss), and idempotent delete (present → True, absent → False).
"""

from __future__ import annotations

import json

import boto3
from app.connections.secrets import SecretsManagerStore, SecretStore
from botocore.stub import Stubber

REF = "vm/00000000-0000-0000-0000-000000000001/00000000-0000-0000-0000-000000000002/slack"


def _store_with_stub() -> tuple[SecretsManagerStore, Stubber]:
    store = SecretsManagerStore(region_name="us-east-1")
    # Build the real boto3 client (no creds needed while stubbed) and inject it.
    client = boto3.client(
        "secretsmanager",
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",  # noqa: S106 — botocore stub placeholder, not a secret
    )
    store._client = client
    return store, Stubber(client)


def test_is_secret_store() -> None:
    store = SecretsManagerStore(region_name="us-east-1")
    assert isinstance(store, SecretStore)


async def test_put_creates_secret() -> None:
    store, stub = _store_with_stub()
    material = {"ANTHROPIC_API_KEY": "fake"}
    stub.add_response(
        "create_secret",
        {"ARN": "arn:aws:secretsmanager:us-east-1:1:secret:x", "Name": REF},
        {"Name": REF, "SecretString": json.dumps(material)},
    )
    with stub:
        await store.put(REF, material)
    stub.assert_no_pending_responses()


async def test_put_updates_when_exists() -> None:
    store, stub = _store_with_stub()
    material = {"OPENAI_API_KEY": "fake"}
    stub.add_client_error("create_secret", service_error_code="ResourceExistsException")
    stub.add_response(
        "put_secret_value",
        {"ARN": "arn:aws:secretsmanager:us-east-1:1:secret:x", "Name": REF},
        {"SecretId": REF, "SecretString": json.dumps(material)},
    )
    with stub:
        await store.put(REF, material)
    stub.assert_no_pending_responses()


async def test_get_returns_material() -> None:
    store, stub = _store_with_stub()
    material = {"ANTHROPIC_API_KEY": "fake", "OPENAI_API_KEY": "fake2"}
    stub.add_response(
        "get_secret_value",
        {"SecretString": json.dumps(material), "Name": REF},
        {"SecretId": REF},
    )
    with stub:
        got = await store.get(REF)
    assert got == material


async def test_get_missing_returns_none() -> None:
    store, stub = _store_with_stub()
    stub.add_client_error("get_secret_value", service_error_code="ResourceNotFoundException")
    with stub:
        got = await store.get(REF)
    assert got is None


async def test_delete_present_returns_true() -> None:
    store, stub = _store_with_stub()
    stub.add_response(
        "delete_secret",
        {"ARN": "arn:aws:secretsmanager:us-east-1:1:secret:x", "Name": REF},
        {"SecretId": REF, "ForceDeleteWithoutRecovery": True},
    )
    with stub:
        assert await store.delete(REF) is True


async def test_delete_absent_returns_false() -> None:
    store, stub = _store_with_stub()
    stub.add_client_error("delete_secret", service_error_code="ResourceNotFoundException")
    with stub:
        assert await store.delete(REF) is False
