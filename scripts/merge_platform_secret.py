"""Merge provider app credentials into the platform secret WITHOUT printing the blob.

Round 6 (2026-09-08) — the owner-at-the-keyboard half of every provider registration
(Google, HubSpot, Salesforce, Calendly, Notion, Meta, Instagram, Microsoft, Twilio OAuth).
The platform secret is ONE JSON object in Secrets Manager (``luciel/<env>/platform``,
security-stack.ts) that ECS injects as LUCIEL_PLATFORM_SECRET; every key name the runtime
reads is declared ONCE in app/config.py (``PLATFORM_SECRET_KEYS``). This script:

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

    # Pre-deploy: what the runtime will refuse or report degraded (no secret values).
    # The API origins default to the environment's own (api-dev + the public api for
    # dev, the public api for prod — infra/lib/env-config.ts `hostnames`).
    python scripts/merge_platform_secret.py --env dev --check
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_REGION = "ca-central-1"  # residency lock (DEVOPS §2.1, LD 21)

# Run as ``python scripts/merge_platform_secret.py`` the script directory, not the repo
# root, is on sys.path; the accepted key set lives in the app package.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _known_keys() -> frozenset[str]:
    """Every platform-secret key the runtime reads — the ONE explicit list in
    app/config.py, not a scrape of the source.

    The scrape this replaces matched only literal ``blob.get("KEY")`` reads, so the keys
    config.py reads through a loop variable (STRIPE_WEBHOOK_SECRET and the three LLM
    model pins) were refused as typos (round 7 WP-7)."""
    from app.config import PLATFORM_SECRET_KEYS  # noqa: PLC0415 — keep import lazy

    return PLATFORM_SECRET_KEYS


def unknown_keys(pairs: dict[str, str]) -> list[str]:
    """The pasted key names the runtime does not read, sorted (empty = all accepted)."""
    known = _known_keys()
    return sorted(k for k in pairs if k not in known)


#: The API origins each environment serves, as infra/lib/env-config.ts ``hostnames``
#: resolves them (owner decision D6): dev answers on its own name AND the public api
#: name while it still owns the apex; prod on the public name. Pinned here so --check
#: judges a redirect URI the way the deployed task will, without a Node toolchain.
ENV_API_ORIGINS: dict[str, tuple[str, ...]] = {
    "dev": ("https://api-dev.vantagemind.ai", "https://api.vantagemind.ai"),
    "preprod": ("https://api-preprod.vantagemind.ai",),
    "prod": ("https://api.vantagemind.ai",),
}


def check_blob(blob: dict[str, Any], *, api_base: str, api_origins: str = "") -> list[str]:
    """What this blob's PROVIDER posture will look like to the runtime — printed before a
    deploy rather than discovered on /healthz after it (round 7 WP-7).

    Returns one line per finding, naming keys and OAuth client labels. A redirect URI is
    public (it travels in every authorize URL) so it may appear; no secret value does.
    """
    from app.config import PLATFORM_SECRET_KEYS, Settings  # noqa: PLC0415

    findings = [
        f"unread key (the runtime ignores it): {key}"
        for key in sorted(blob)
        if key not in PLATFORM_SECRET_KEYS
    ]
    # A malformed META_GRAPH_VERSION refuses to boot (every Graph URL is built on it):
    # say so here, by key, instead of letting the deploy roll back on it.
    try:
        settings = Settings(
            _env_file=None,
            integration_mode="mock",  # describe the posture; never fail on live-only guards
            public_api_base_url=api_base,
            api_origins=api_origins,
            platform_secret=json.dumps({str(k): str(v) for k, v in blob.items()}),
        )
    except RuntimeError as exc:
        findings.append(f"boot refused: {exc}")
        return findings
    findings += [
        f"oauth_redirect:{label} — {reason}"
        for label, reason in sorted(settings.oauth_redirect_problems().items())
    ]
    if settings.meta_webhook_unconfigured:
        findings.append(
            "meta_webhook:unconfigured — a Meta/Instagram app is provisioned without "
            "META_VERIFY_TOKEN (a live boot refuses this)"
        )
    if settings.meta_app_configured and not settings.meta_app_secret:
        findings.append("META_APP_SECRET missing for a provisioned Meta app (live boot refuses)")
    return findings


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
    ap.add_argument(
        "--check",
        action="store_true",
        help="report unread keys, invalid OAuth redirect URIs and Meta webhook gaps, then exit",
    )
    ap.add_argument(
        "--api-base",
        default="",
        help="with --check: the API origin the task is injected with (default: the "
        "environment's own, see ENV_API_ORIGINS)",
    )
    ap.add_argument(
        "--api-origins",
        default="",
        help="with --check: every API origin the environment serves, comma-separated "
        "(default: ENV_API_ORIGINS for --env); a redirect URI may sit on any of them",
    )
    args = ap.parse_args(argv)
    if args.check:
        origins = ENV_API_ORIGINS.get(args.env, ())
        if not args.api_base:
            args.api_base = origins[0] if origins else ""
        if not args.api_origins:
            args.api_origins = ",".join(origins)
        if not args.api_base:
            # Judging against nothing would silently fall back to the blob's own
            # API_BASE_URL - a check that cannot see the mismatch it exists for.
            raise SystemExit(
                f"--check needs the API origin for --env {args.env!r}: pass --api-base "
                "(and --api-origins), or add the environment to ENV_API_ORIGINS"
            )
    read_only = args.list or args.check

    # Validate the paste BEFORE touching AWS: a typo is refused without reading the
    # secret at all, and nothing is written.
    pairs: dict[str, str] = {}
    if not read_only:
        pairs = _read_pairs(sys.stdin)
        if not pairs:
            raise SystemExit("nothing to merge: paste KEY=VALUE lines on stdin")
        unknown = unknown_keys(pairs)
        if unknown and not args.allow_unknown:
            raise SystemExit(
                "these keys are not read by the runtime (typo?): "
                + ", ".join(unknown)
                + " — see PLATFORM_SECRET_KEYS in app/config.py, or pass --allow-unknown"
            )

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
    if args.check:
        print(f"redirect URIs judged against: {args.api_origins or args.api_base}")
        findings = check_blob(blob, api_base=args.api_base, api_origins=args.api_origins)
        for line in findings:
            print(line)
        print(f"{len(findings)} finding(s) in {secret_id}")
        return 1 if findings else 0

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
