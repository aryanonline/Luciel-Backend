"""Run the scheduled sweeps a local Luciel needs (the Docker stack's ``scheduler`` service).

In AWS, EventBridge Scheduler starts one ECS task per sweep run (``scripts/run-sweep.sh``
-> ``python -m app.jobs <name>``). Locally this loop starts the same command on a short
cadence, so a conversation left idle is closed and summarised, queued hand-off pages are
retried, crawled websites re-sync and the morning brief goes out.

Left out on purpose: ``health`` and ``transcript_archive`` read stores that live in each
process's memory under mock mode (tenant secrets, the transcript archive), so from a
separate process they would mark connections "reconnect" and lose transcripts; the
mailbox, billing and retention sweeps need real providers or only matter over months.

One failed run never stops the loop; it is logged and tried again next time.
"""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class LocalSweep:
    name: str
    every_seconds: int


#: Sweep name (as ``python -m app.jobs`` knows it) and how often it runs locally.
LOCAL_SWEEPS: tuple[LocalSweep, ...] = (
    LocalSweep("session_finalize", 5 * 60),
    LocalSweep("deferred", 5 * 60),
    LocalSweep("knowledge_sync", 60 * 60),
    LocalSweep("daily_brief", 60 * 60),
    LocalSweep("unverified_reap", 24 * 60 * 60),
)

#: Give the API a moment to finish starting before the first pass.
FIRST_RUN_DELAY_SECONDS = 60


def due(last_run: dict[str, float], now: float) -> list[LocalSweep]:
    """The sweeps whose interval has passed (every sweep is due on the first pass)."""
    return [s for s in LOCAL_SWEEPS if now - last_run.get(s.name, float("-inf")) >= s.every_seconds]


def run_sweep(name: str) -> int:
    print(f"[scheduler] running {name}", flush=True)
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv from LOCAL_SWEEPS, no shell
            [sys.executable, "-m", "app.jobs", name], cwd=REPO, check=False
        )
    except OSError as exc:  # the interpreter itself could not start
        print(f"[scheduler] {name} could not start: {exc}", flush=True)
        return 1
    if result.returncode != 0:
        print(f"[scheduler] {name} exited {result.returncode}; trying again next time", flush=True)
    return result.returncode


def main() -> int:
    print(
        "[scheduler] local sweeps: "
        + ", ".join(f"{s.name} every {s.every_seconds // 60} min" for s in LOCAL_SWEEPS),
        flush=True,
    )
    time.sleep(FIRST_RUN_DELAY_SECONDS)
    last_run: dict[str, float] = {}
    while True:
        for sweep in due(last_run, time.monotonic()):
            run_sweep(sweep.name)
            last_run[sweep.name] = time.monotonic()
        time.sleep(30)


if __name__ == "__main__":
    raise SystemExit(main())
