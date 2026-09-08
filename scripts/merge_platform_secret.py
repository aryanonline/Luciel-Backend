"""Merge provider app credentials into the platform secret WITHOUT printing the blob.

Round 6 (2026-09-08) — the owner-at-the-keyboard half of every provider registration
(Google, HubSpot, Salesforce, Calendly, Notion, Meta, Instagram, Microsoft, Twilio OAuth).
The platform secret is ONE JSON object in Secrets Manager (``luciel/<env>/platform``,
security-stack.ts) that ECS injects as LUCIEL_PLATFORM_SECRET; every key name the runtime
reads is listed in app/config.py (parse_platform_secret's mapping). This script:

  1. reads KEY=VALUE pairs from stdin (one per line; blank lines and ``#`` comments
     ignored) — the owner pastes secrets here, never on a command line where a shell
     history would keep them;
  2. refuses any key the runtime does not read (a typo would otherwise be stored and
     silently ignored, and the provider would stay "not configured");
  3. merges them into the existing blob (other keys untouched) and writes it back;
  4. prints ONLY the key names written and the secret's new version id — never a value;
  5. with --redeploy, forces a new deployment of every ECS service in the cluster so the
     running tasks pick the blob up (sweep tasks read it fresh at every start).

Usage (from the backend repo, with AWS credentials for the target account):

    printf 'HUBSPOT_CLIENT_ID=...\\nHUBSPOT_CLIENT_SECRET=...\\n' | \\
        python scripts/merge_platform_secret.py --env dev --redeploy

    python scripts/merge_platform_secret.py --env dev --list      # key names present, no values
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_REGION = "ca-central-1"  # residency lock (DEVOPS §2.1, LD 21)


_READ_RE = re.compile(r'(?:blob|secret|platform)(?:\.get\(|\[)\s*"([A-Z][A-Z0-9_]+)"')
_MAP_RE = re.compile(r'"([A-Z][A-Z0-9_]+)":\s*"[a-z_]+"')


def _known_keys() -> set[str]:
    """Every platform-secret key the runtime reads: scanned from the app source (the
    config.py blob->field mapping plus every direct ``blob.get("KEY")`` read)."""
    from pathlib import Path  # noqa: PLC0415

    root = Path(__file__).resolve().parent.parent / "app"
    keys: set[str] = set()
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        keys.update(_READ_RE.findall(text))
        if path.name == "config.py":
            keys.update(_MAP_RE.findall(text))
    return keys


def _read_pairs(stream: Any) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for raw in stream:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise SystemExit(f"refusing line without '=': {line[:16]}…")
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not _KEY_RE.match(key):
            raise SystemExit(f"refusing malformed key name: {key!r}")
        if not value:
            raise SystemExit(f"refusing empty value for {key}")
        pairs[key] = value
    return pairs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--env", default="dev", help="environment name in the secret id (dev|prod)")
    ap.add_argument("--region", default=_REGION)
    ap.add_argument("--list", action="store_true", help="print the key names present and exit")
    ap.add_argument(
        "--redeploy", action="store_true", help="force a new deployment of every ECS service"
    )
    ap.add_argument(
        "--cluster", default=None, help="ECS cluster name (default: the only luciel-<env> cluster)"
    )
    ap.add_argument(
        "--allow-unknown", action="store_true", help="store a key the runtime does not read"
    )
    args = ap.parse_args(argv)

    import boto3  # noqa: PLC0415 — keep the app import surface small

    sm = boto3.client("secretsmanager", region_name=args.region)
    secret_id = f"luciel/{args.env}/platform"
    current = sm.get_secret_value(SecretId=secret_id)
    blob = json.loads(current.get("SecretString") or "{}")
    if not isinstance(blob, dict):
        raise SystemExit("the platform secret is not a JSON object; refusing to overwrite it")

    if args.list:
        for key in sorted(blob):
            print(key)
        return 0

    pairs = _read_pairs(sys.stdin)
    if not pairs:
        raise SystemExit("nothing to merge: paste KEY=VALUE lines on stdin")
    unknown = sorted(k for k in pairs if k not in _known_keys())
    if unknown and not args.allow_unknown:
        raise SystemExit(
            "these keys are not read by the runtime (typo?): "
            + ", ".join(unknown)
            + " — see parse_platform_secret in app/config.py, or pass --allow-unknown"
        )

    merged = {**blob, **pairs}
    resp = sm.put_secret_value(
        SecretId=secret_id, SecretString=json.dumps(merged, separators=(",", ":"))
    )
    print(f"wrote {len(pairs)} key(s) to {secret_id}: {', '.join(sorted(pairs))}")
    print(f"version {resp.get('VersionId', '?')}")

    if args.redeploy:
        ecs = boto3.client("ecs", region_name=args.region)
        cluster = args.cluster
        if cluster is None:
            arns = ecs.list_clusters().get("clusterArns", [])
            candidates = [
                a for a in arns if f"luciel-{args.env}" in a.lower() or args.env in a.lower()
            ]
            if len(candidates) != 1:
                raise SystemExit(f"pass --cluster; found {len(candidates)} candidate cluster(s)")
            cluster = candidates[0]
        services = ecs.list_services(cluster=cluster).get("serviceArns", [])
        for svc in services:
            ecs.update_service(cluster=cluster, service=svc, forceNewDeployment=True)
            print(f"redeploying {svc.rsplit('/', 1)[-1]}")
        if not services:
            print("no services found in the cluster; nothing redeployed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
