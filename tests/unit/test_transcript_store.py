"""LocalTranscriptStore semantics + the archive key convention (Commitment #38)."""

from __future__ import annotations

import uuid

import pytest
from app.lifecycle.transcript_store import (
    LocalTranscriptStore,
    TranscriptStoreUnavailable,
    transcript_key_for,
    transcript_prefix_for,
)

ADMIN = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
OTHER = uuid.UUID("00000000-0000-0000-0000-0000000000bb")
IID = uuid.uuid4()


@pytest.fixture(autouse=True)
def _fresh_store():
    LocalTranscriptStore.reset()
    yield
    LocalTranscriptStore.reset()


def test_key_convention_nests_under_the_tenant_prefix():
    """The cascade deletes by tenant prefix, so every key MUST live under it."""
    sid = uuid.uuid4()
    key = transcript_key_for(ADMIN, IID, sid)
    assert key.startswith(transcript_prefix_for(ADMIN))
    assert key.endswith(f"{sid}.jsonl")


async def test_put_get_delete_roundtrip():
    store = LocalTranscriptStore()
    key = transcript_key_for(ADMIN, IID, uuid.uuid4())
    await store.put(key=key, data=b"line-1\n")
    assert await store.get(key=key) == b"line-1\n"
    await store.delete(key=key)
    assert await store.get(key=key) is None
    # Idempotent delete: an absent key is a no-op, not an error.
    await store.delete(key=key)


async def test_put_is_overwrite_idempotent():
    """The archive sweep is at-least-once; a re-put of the same session key must
    replace, never duplicate."""
    store = LocalTranscriptStore()
    key = transcript_key_for(ADMIN, IID, uuid.uuid4())
    await store.put(key=key, data=b"v1\n")
    await store.put(key=key, data=b"v2\n")
    assert await store.get(key=key) == b"v2\n"
    assert len(LocalTranscriptStore.keys()) == 1


async def test_delete_prefix_removes_only_the_tenants_objects():
    """Tenant-prefix deletion (the cascade's handle) must never cross tenants."""
    store = LocalTranscriptStore()
    mine = [transcript_key_for(ADMIN, IID, uuid.uuid4()) for _ in range(3)]
    theirs = transcript_key_for(OTHER, IID, uuid.uuid4())
    for k in mine:
        await store.put(key=k, data=b"x")
    await store.put(key=theirs, data=b"y")

    deleted = await store.delete_prefix(prefix=transcript_prefix_for(ADMIN))

    assert deleted == 3
    assert LocalTranscriptStore.keys() == [theirs]


def _rebind(monkeypatch, *, bucket: str | None, mode: str):
    """Bind a fresh store under a given deployment shape."""
    from app.config import get_settings
    from app.lifecycle import transcript_store as mod

    monkeypatch.setenv("LUCIEL_INTEGRATION_MODE", mode)
    if bucket is None:
        monkeypatch.delenv("LUCIEL_TRANSCRIPT_ARCHIVE_BUCKET", raising=False)
    else:
        monkeypatch.setenv("LUCIEL_TRANSCRIPT_ARCHIVE_BUCKET", bucket)
    get_settings.cache_clear()  # type: ignore[attr-defined]
    mod.set_transcript_store(None)
    try:
        return mod.get_transcript_store()
    finally:
        mod.set_transcript_store(None)
        get_settings.cache_clear()  # type: ignore[attr-defined]


async def test_a_deployed_environment_archives_for_real_even_in_mock_mode(monkeypatch):
    """Commitment #38 must not be hostage to the third-party integration flag.

    The store used to bind on ``integration_mode == "live"``. Dev is always ``mock``
    and production is ``mock`` until the owner-gated live cutover, so in BOTH the
    sweep bound the in-process fake, refused to move anything (correctly — the fake
    cannot outlive the sweep process), and the 90-day hot→cold offload we promise
    customers never ran. Transcripts sat in hot Postgres indefinitely instead.
    """
    store = _rebind(monkeypatch, bucket="luciel-dev-transcript-archive-000", mode="mock")

    assert type(store).__name__ == "S3TranscriptStore"
    assert store.durable is True  # the archive sweep is allowed to proceed


async def test_a_local_run_keeps_the_in_process_fake(monkeypatch):
    """No injected bucket means no AWS: the suite and docker-compose stay hermetic."""
    store = _rebind(monkeypatch, bucket=None, mode="mock")

    assert type(store).__name__ == "LocalTranscriptStore"
    assert store.durable is False  # and so the sweep refuses to delete hot rows


class _RecordingS3Client:
    """Stub boto3 client modeling a VERSIONED bucket's list/delete surface.

    Seeded with (key, version_id, is_delete_marker) tuples; records every
    delete_objects call so tests can assert exactly what was destroyed.
    """

    def __init__(self, versions: list[tuple[str, str, bool]], *, page_size: int = 2) -> None:
        self._versions = list(versions)
        self._page_size = page_size
        self.deleted: list[dict[str, str]] = []

    def list_object_versions(self, **kwargs):
        prefix = kwargs.get("Prefix", "")
        matching = [v for v in self._versions if v[0].startswith(prefix)]
        start = 0
        if "KeyMarker" in kwargs:
            start = int(kwargs["KeyMarker"])
        page = matching[start : start + self._page_size]
        truncated = start + self._page_size < len(matching)
        out: dict[str, object] = {
            "Versions": [{"Key": k, "VersionId": vid} for k, vid, marker in page if not marker],
            "DeleteMarkers": [{"Key": k, "VersionId": vid} for k, vid, marker in page if marker],
            "IsTruncated": truncated,
        }
        if truncated:
            # Markers are opaque to the caller; an index keeps the stub honest.
            out["NextKeyMarker"] = str(start + self._page_size)
            out["NextVersionIdMarker"] = "-"
        return out

    def delete_objects(self, *, Bucket, Delete):  # noqa: N803 — boto3's casing
        self.deleted.extend(Delete["Objects"])


def _s3_store_with(client) -> object:
    from app.lifecycle.transcript_store import S3TranscriptStore

    store = S3TranscriptStore(bucket="b", region_name="ca-central-1")
    store._client = client  # noqa: SLF001 — inject the stub under the lazy seam
    return store


async def test_delete_destroys_every_version_and_marker_of_the_key():
    """A versioned bucket turns delete_object into a soft delete: the bytes persist
    as a noncurrent version for the 30-day lifecycle tail. Retention and the
    account-closure cascade promise the data is GONE (owner decision 2026-08-10)."""
    key = "vm/a/transcripts/i/s.jsonl"
    client = _RecordingS3Client([(key, "v1", False), (key, "v2", False), (key, "m1", True)])
    store = _s3_store_with(client)

    await store.delete(key=key)

    assert sorted(d["VersionId"] for d in client.deleted) == ["m1", "v1", "v2"]


async def test_delete_never_touches_sibling_keys_sharing_the_prefix():
    """S3 prefix listing would happily return `s.jsonl.bak` for prefix `s.jsonl`."""
    key = "vm/a/transcripts/i/s.jsonl"
    sibling = "vm/a/transcripts/i/s.jsonl.bak"
    client = _RecordingS3Client([(key, "v1", False), (sibling, "x1", False)])
    store = _s3_store_with(client)

    await store.delete(key=key)

    assert [d["Key"] for d in client.deleted] == [key]


class _FailingGetClient:
    """Stub boto3 client whose get_object raises what the test hands it."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def get_object(self, **_kwargs):
        raise self._exc


def _client_error(code: str, *, http_status: int, operation: str = "GetObject"):
    from botocore.exceptions import ClientError

    return ClientError(
        {
            "Error": {"Code": code, "Message": f"{code} for the key"},
            "ResponseMetadata": {"HTTPStatusCode": http_status, "RequestId": "req-1"},
        },
        operation,
    )


@pytest.mark.parametrize(
    "exc",
    [
        _client_error("NoSuchKey", http_status=404),
        _client_error("NotFound", http_status=404, operation="HeadObject"),
        _client_error("404", http_status=404, operation="HeadObject"),
    ],
    ids=["NoSuchKey", "NotFound", "bare-404"],
)
async def test_get_reads_s3s_own_no_such_object_answer_as_absent(exc):
    """The store ANSWERED: the key resolves to nothing. That — and only that — is None."""
    store = _s3_store_with(_FailingGetClient(exc))

    assert await store.get(key="vm/a/transcripts/i/s.jsonl") is None


@pytest.mark.parametrize(
    "exc",
    [
        _client_error("AccessDenied", http_status=403),
        _client_error("NoSuchBucket", http_status=404),
        _client_error("InternalError", http_status=500),
        _client_error("SlowDown", http_status=503),
        ConnectionError("Could not connect to the endpoint URL"),
        TimeoutError("read timed out"),
    ],
    ids=["AccessDenied", "NoSuchBucket", "InternalError", "SlowDown", "transport", "timeout"],
)
async def test_get_raises_unavailable_when_the_store_could_not_answer(exc):
    """Round 7 WP-5, item 7: every one of these used to return None, so a five-second
    S3 hiccup rendered as "this customer said nothing" and the dashboard's copy swore
    nothing had been lost. NoSuchBucket is a binding fault, not an absent transcript:
    the object is sitting in the bucket we were NOT pointed at."""
    store = _s3_store_with(_FailingGetClient(exc))

    with pytest.raises(TranscriptStoreUnavailable) as info:
        await store.get(key="vm/a/transcripts/i/s.jsonl")

    assert info.value.__cause__ is exc
    # The reason names the failure class and S3 code only — never the message, which
    # can quote the bucket, the key or the request id.
    assert "for the key" not in str(info.value)
    assert "req-1" not in str(info.value)


async def test_delete_prefix_takes_all_versions_and_counts_distinct_keys():
    """The cascade's acted-count is KEYS removed, not versions — three versions of
    one transcript are still one conversation."""
    client = _RecordingS3Client(
        [
            ("vm/a/transcripts/i/one.jsonl", "v1", False),
            ("vm/a/transcripts/i/one.jsonl", "v2", False),
            ("vm/a/transcripts/i/one.jsonl", "m1", True),
            ("vm/a/transcripts/i/two.jsonl", "v9", False),
            ("vm/OTHER/transcripts/i/theirs.jsonl", "z1", False),
        ],
        page_size=2,  # forces pagination across three pages
    )
    store = _s3_store_with(client)

    count = await store.delete_prefix(prefix="vm/a/transcripts/")

    assert count == 2
    assert len(client.deleted) == 4  # every version + the marker, nothing cross-tenant
    assert all(d["Key"].startswith("vm/a/") for d in client.deleted)
