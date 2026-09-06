"""The export bucket the app writes to has to exist, and has to expire (P1-14).

``S3ExportStore`` uploaded a full tenant data dump to a bucket that appeared in no CDK
stack. Two consequences, both invisible from either side alone: the live ``put_object``
failed AccessDenied because the task role was granted only the knowledge bucket, and the
docstring's claim that "the bucket's own lifecycle rule bounds the object's life" was
false — nothing expired those ZIPs once the 7-day presigned link went stale.

Same shape of wiring assertion as ``test_sweep_scheduling.py``: neither half fails on its
own, so the test reads both.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.config import Settings

_INFRA = Path(__file__).resolve().parents[2] / "infra" / "lib"
_DATA_STACK = (_INFRA / "data-stack.ts").read_text(encoding="utf-8")
_COMPUTE_STACK = (_INFRA / "compute-stack.ts").read_text(encoding="utf-8")


def _export_bucket_block() -> str:
    block = re.search(
        r"this\.exportBucket = residency\.hardenedBucket\((.*?)\n    \}\);", _DATA_STACK, re.DOTALL
    )
    assert block, "the export bundles bucket is not provisioned in data-stack.ts"
    return block.group(1)


def test_the_export_bucket_expires_its_objects_close_to_the_link_ttl():
    """An export ZIP is every table a tenant owns in one object. It must not outlive the
    link that was the only way to fetch it by more than a rounding error."""
    days = re.search(r"expiration: Duration\.days\((\d+)\)", _export_bucket_block())
    assert days, "the export bucket has no expiration rule"
    assert Settings().export_link_ttl_days <= int(days.group(1)) <= 10


def test_the_task_role_can_actually_write_to_it():
    # Granted through the shared app-plane grant (F115): the control plane and the
    # sweeps both receive it; the narrower data plane deliberately does not.
    assert "exportBucket.grantReadWrite(role)" in _COMPUTE_STACK
    assert "grantAppPlaneAccess(taskRole)" in _COMPUTE_STACK


def test_the_bucket_name_is_injected_rather_than_hardcoded_in_the_app():
    """One hardcoded name cannot exist in two accounts, which is how a dev export ends up
    in the prod bucket."""
    assert "LUCIEL_EXPORT_BUCKET: exportBucket.bucketName" in _COMPUTE_STACK
    assert "luciel-${envConfig.envName}-export-bundles" in _export_bucket_block()
