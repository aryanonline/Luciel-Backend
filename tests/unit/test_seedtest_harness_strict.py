"""The SeedTest harness proves the RIGHT environment, strictly (round 7 WP-8b, D6/D10).

``scripts/seed_and_livetest.py`` is the §10 live-behavior harness the deploy now runs
after EVERY dev deploy (owner decision D10) and as the preprod release gate:

* ``--strict``: a skipped T-case is a FAILURE unless its skip is an allowed kind — an
  opt-in case (real model spend, an owner-provided endpoint) nobody opted into. A
  missing live key in a live environment used to be a quiet "SKIP" the gate passed.
* The API it drives: ``LUCIEL_API_BASE`` (the SeedTest task injects the environment's
  own origin), else the D6 host derived from ``LUCIEL_ENV``. There is no longer a
  public-host default — that default is how a harness reading one environment's
  database drives ANOTHER environment's API.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "seed_and_livetest.py"


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    # Import the script as a module (it is not a package member). No DB secret in the
    # environment, so its import-time bootstrap is a no-op.
    monkeypatch.delenv("LUCIEL_DB_SECRET", raising=False)
    monkeypatch.setenv("LUCIEL_API_BASE", "https://api-dev.vantagemind.ai")
    spec = importlib.util.spec_from_file_location("seed_and_livetest_under_test", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({"LUCIEL_API_BASE": "https://api-dev.vantagemind.ai/"}, "https://api-dev.vantagemind.ai"),
        # The injected value wins over the environment name.
        (
            {"LUCIEL_API_BASE": "https://example.test", "LUCIEL_ENV": "prod"},
            "https://example.test",
        ),
        ({"LUCIEL_ENV": "dev"}, "https://api-dev.vantagemind.ai"),
        ({"LUCIEL_ENV": "preprod"}, "https://api-preprod.vantagemind.ai"),
        ({"LUCIEL_ENV": "prod"}, "https://api.vantagemind.ai"),
        # Nothing to go on: no guess, and especially not the public host.
        ({}, ""),
        ({"LUCIEL_ENV": "staging"}, ""),
    ],
)
def test_the_api_it_drives_is_the_environments_own(
    harness: ModuleType, environ: dict[str, str], expected: str
) -> None:
    assert harness._derive_api_base(environ) == expected


def test_without_an_api_to_prove_the_harness_refuses_to_run(
    harness: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(harness, "API_BASE", "")
    assert harness.main([]) == 2


def test_strict_fails_a_precondition_skip(harness: ModuleType) -> None:
    r = harness.Results(strict=True)
    r.skip("T5 row8-primary-to-fallback", "no OPENAI_API_KEY", kind=harness.SKIP_PRECONDITION)
    assert r.any_failed()


def test_strict_tolerates_an_opt_in_case_nobody_opted_into(harness: ModuleType) -> None:
    r = harness.Results(strict=True)
    r.skip("T3b row1b-llm-cap-live", "set LUCIEL_SEEDTEST_LIVE_CAP=1", kind=harness.SKIP_OPT_IN)
    r.skip(
        "T7 crm-custom-webhook-live", "set LUCIEL_SEEDTEST_WEBHOOK_URL", kind=harness.SKIP_OPT_IN
    )
    assert not r.any_failed()


def test_outside_strict_a_skip_stays_non_fatal(harness: ModuleType) -> None:
    r = harness.Results()
    r.skip("T5 row8-primary-to-fallback", "no OPENAI_API_KEY", kind=harness.SKIP_PRECONDITION)
    assert not r.any_failed()


def test_only_the_opt_in_kind_is_allowed_and_every_skip_names_its_kind(
    harness: ModuleType,
) -> None:
    assert harness.STRICT_ALLOWED_SKIPS == frozenset({harness.SKIP_OPT_IN})
    source = _SCRIPT.read_text(encoding="utf-8")
    skips = source.count("r.skip(")
    assert skips >= 3
    kinds = source.count("kind=SKIP_OPT_IN") + source.count("kind=SKIP_PRECONDITION")
    assert kinds == skips, "every r.skip(...) must declare why it may be skipped"


def test_the_strict_flag_and_its_environment_switch(
    harness: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[bool] = []

    class _Stop(Exception):
        pass

    def _results(*, strict: bool = False) -> None:
        seen.append(strict)
        raise _Stop

    monkeypatch.setattr(harness, "Results", _results)
    with pytest.raises(_Stop):
        harness.main(["--strict"])
    monkeypatch.setenv("LUCIEL_SEEDTEST_STRICT", "1")
    with pytest.raises(_Stop):
        harness.main([])
    monkeypatch.delenv("LUCIEL_SEEDTEST_STRICT")
    with pytest.raises(_Stop):
        harness.main([])
    assert seen == [True, True, False]
