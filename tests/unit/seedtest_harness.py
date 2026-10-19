"""Load the SeedTest harness (scripts/seed_and_livetest.py) and its ECS runner's embedded
Python for the harness's own unit tests.

The harness is a script, not a package module: it is loaded once by path and cached in
``sys.modules`` so every test file sees the same module object. Importing it has no side
effects beyond constants (the DB-env bootstrap is a no-op without LUCIEL_DB_SECRET).
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "scripts" / "seed_and_livetest.py"
RUNNER = ROOT / "scripts" / "run-seedtest-task.sh"
_NAME = "seed_and_livetest"


def load() -> ModuleType:
    cached = sys.modules.get(_NAME)
    if cached is not None:
        return cached
    # The harness has no public-host default any more (owner decision D6): it proves
    # the API the task names, or refuses. These tests drive a stand-in, so name one.
    os.environ.setdefault("LUCIEL_API_BASE", "https://api-dev.vantagemind.ai")
    spec = importlib.util.spec_from_file_location(_NAME, HARNESS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_NAME] = module
    spec.loader.exec_module(module)
    return module


def embedded_python(marker: str) -> str:
    """The ``python3 - <<'MARKER' ... MARKER`` block of run-seedtest-task.sh, verbatim, so
    the tests run exactly what the runner runs (the two cannot drift)."""
    text = RUNNER.read_text(encoding="utf-8").replace("\r\n", "\n")
    opener = f"<<'{marker}'\n"
    start = text.index(opener) + len(opener)
    return text[start : text.index(f"\n{marker}\n", start)]
