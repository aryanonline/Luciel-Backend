#!/usr/bin/env python3
"""Doctrine-to-code sync gate (Architecture §5.9.3 / §8).

Two checks:
  1. Every doctrine-anchored code artifact named in Architecture §8 exists. A
     missing anchor means the code drifted from the doctrine.
  2. (Advisory) If a doctrine-anchored file changed in this PR, the PR must also
     touch the architecture doc — enforced as a CI step when run with --diff-base.

This is intentionally lightweight: it guarantees the anchors are present and
flags interface changes for the reviewer, per the maintainability contract.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Architecture §8 doctrine anchors (files or package dirs).
DOCTRINE_ANCHORS = [
    "app/policy/entitlements.py",
    "app/runtime/orchestrator.py",
    "app/runtime/llm_router.py",
    "app/runtime/knowledge_retrieval.py",
    "app/api/v1/admin/usage.py",
    "app/billing/metering.py",
    "app/notifications/",
    "app/lifecycle/",
    "app/auth/access.py",
    "app/connections/",
    "app/migrations/",
    "app/analytics/",
    "app/runtime/handoff.py",
    "app/runtime/grounding.py",
    "app/runtime/input_safety.py",
    ".github/workflows/",
    "tests/isolation/",
]

# Anchors expected to exist from a given arc onward. Until an arc lands its anchor
# may be a stub; the check requires presence (a stub with a docstring is fine).
REQUIRED_NOW = {
    "app/auth/access.py",
    "app/migrations/",
    "app/analytics/",
    "tests/isolation/",
    ".github/workflows/",
    "app/lifecycle/",
    "app/connections/",
    "app/notifications/",
    "app/policy/entitlements.py",
    "app/runtime/orchestrator.py",
    "app/runtime/llm_router.py",
    "app/runtime/knowledge_retrieval.py",
    "app/runtime/grounding.py",
    "app/runtime/input_safety.py",
    "app/runtime/handoff.py",
    "app/billing/metering.py",
    "app/api/v1/admin/usage.py",
}


def check_anchors_present() -> list[str]:
    missing = []
    for anchor in DOCTRINE_ANCHORS:
        if anchor not in REQUIRED_NOW:
            continue
        path = ROOT / anchor
        if anchor.endswith("/"):
            if not path.is_dir():
                missing.append(anchor)
        elif not path.is_file():
            missing.append(anchor)
    return missing


def changed_files(base: str) -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]


def main() -> int:
    missing = check_anchors_present()
    if missing:
        print("DOCTRINE SYNC FAILURE — missing doctrine-anchored artifacts:")
        for m in missing:
            print(f"  - {m}")
        return 1

    # Optional diff-based advisory.
    if len(sys.argv) >= 3 and sys.argv[1] == "--diff-base":
        base = sys.argv[2]
        changed = changed_files(base)
        anchor_files = [
            c
            for c in changed
            if any(c == a or c.startswith(a) for a in DOCTRINE_ANCHORS if not a.startswith("."))
        ]
        doc_touched = any(c.endswith(".md") or "ARCHITECTURE" in c.upper() for c in changed)
        if anchor_files and not doc_touched:
            print(
                "DOCTRINE SYNC WARNING: doctrine-anchored files changed but no doc/.md "
                "was updated in the same PR (Architecture §5.9.3):"
            )
            for f in anchor_files:
                print(f"  - {f}")
            # Warning, not a hard fail at this stage of the build.

    print("doctrine sync OK — all required anchors present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
