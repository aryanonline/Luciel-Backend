"""CLI dispatcher for the scheduled sweeps: ``python -m app.jobs <sweep>``.

This is the command EventBridge Scheduler overrides the sweep task's container
command with (``infra/lib/compute-stack.ts``), so the schedule name and the sweep
name are the same string in both halves. The result is printed as one line so the
CloudWatch log for a run says what it did, and the exit code is non-zero on failure
so a failed sweep is visible to the schedule's retry policy rather than silent.
"""

from __future__ import annotations

import asyncio
import json
import sys

from app.jobs.sweeps import SWEEPS


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1 or args[0] not in SWEEPS:
        print(f"usage: python -m app.jobs {{{'|'.join(SWEEPS)}}}", file=sys.stderr)
        return 2
    name = args[0]
    result = asyncio.run(SWEEPS[name]())
    print(json.dumps({"sweep": name, "result": result}, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
