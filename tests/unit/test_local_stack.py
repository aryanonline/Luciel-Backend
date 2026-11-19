"""The local Docker stack (docker-compose.yml + scripts/local/): what a stranger runs.

``docker compose up`` must stay a local-only, no-AWS, real-AI stack: every host port on
127.0.0.1, integration mode mock, the AI posture live unless CI says otherwise, the
key preflight before anything starts, the demo seeder refusing anything but a developer's
own computer, and a scheduler that runs only sweeps that are safe in a separate process.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml
from app.jobs.sweeps import SWEEPS

REPO = Path(__file__).resolve().parents[2]
LOCAL = REPO / "scripts" / "local"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"local_{name}", LOCAL / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Registered first: @dataclass resolves its string annotations through sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _compose() -> dict[str, Any]:
    return yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))


# --- docker-compose.yml ------------------------------------------------------------ #


def test_every_published_port_listens_on_localhost_only() -> None:
    """A laptop on café Wi-Fi must not offer postgres/postgres to the room."""
    for name, service in _compose()["services"].items():
        for port in service.get("ports", []):
            assert str(port).startswith("127.0.0.1:"), f"{name} publishes {port}"


def test_the_stack_is_mock_mode_with_real_ai_and_no_aws() -> None:
    env = _compose()["x-luciel-env"]
    assert env["LUCIEL_ENVIRONMENT"] == "development"
    assert env["LUCIEL_INTEGRATION_MODE"] == "mock"
    assert env["LUCIEL_LLM_POSTURE"] == "${LUCIEL_LLM_POSTURE:-live}"
    assert env["LUCIEL_COOKIE_SECURE"] == "false"
    # The verify links, the cookie and the widget all need "localhost", never 127.0.0.1.
    assert env["LUCIEL_APP_BASE_URL"] == "http://localhost:3000"
    assert json.loads(env["LUCIEL_DASHBOARD_ORIGINS"]) == ["http://localhost:3000"]
    secret = env["LUCIEL_PLATFORM_SECRET"]
    assert "${OPENAI_API_KEY:-}" in secret and "${ANTHROPIC_API_KEY:-}" in secret
    # Nothing in the stack names an AWS resource or credential.
    assert not any(re.search(r"AWS|BUCKET|ARN", k) for k in env)


def test_the_test_suite_can_still_start_only_the_database_and_redis() -> None:
    services = _compose()["services"]
    for name in ("db", "redis"):
        assert "depends_on" not in services[name]
    db_env = services["db"]["environment"]
    assert db_env["POSTGRES_DB"] == "luciel_dev"  # the suite uses its own luciel_test


def test_the_app_waits_for_the_migration_and_the_seed_waits_for_the_app() -> None:
    services = _compose()["services"]
    assert services["api"]["depends_on"]["migrate"]["condition"] == (
        "service_completed_successfully"
    )
    assert services["seed"]["depends_on"]["api"]["condition"] == "service_healthy"
    assert services["web"]["build"]["args"]["NEXT_PUBLIC_API_BASE_URL"] == "http://localhost:8000"


# --- scripts/local/migrate.py ------------------------------------------------------- #


def test_the_preflight_stops_a_real_ai_stack_without_a_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    migrate = _load("migrate")
    monkeypatch.setenv("LUCIEL_LLM_POSTURE", "live")
    monkeypatch.setenv(
        "LUCIEL_PLATFORM_SECRET", json.dumps({"OPENAI_API_KEY": "", "ANTHROPIC_API_KEY": ""})
    )
    with pytest.raises(SystemExit) as exc:
        migrate.preflight()
    assert exc.value.code == 2
    assert "needs an AI key" in capsys.readouterr().err


@pytest.mark.parametrize("key", ["OPENAI_API_KEY", "ANTHROPIC_API_KEY"])
def test_the_preflight_passes_with_either_key(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    migrate = _load("migrate")
    monkeypatch.setenv("LUCIEL_LLM_POSTURE", "live")
    monkeypatch.setenv("LUCIEL_PLATFORM_SECRET", json.dumps({key: "fake-key"}))
    migrate.preflight()


def test_the_preflight_is_silent_when_the_ai_follows_the_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migrate = _load("migrate")
    monkeypatch.setenv("LUCIEL_LLM_POSTURE", "follow")
    monkeypatch.setenv("LUCIEL_PLATFORM_SECRET", "{}")
    migrate.preflight()


# --- scripts/local/scheduler.py ----------------------------------------------------- #


def test_the_scheduler_runs_only_real_sweeps_that_are_safe_in_their_own_process() -> None:
    scheduler = _load("scheduler")
    names = {sweep.name for sweep in scheduler.LOCAL_SWEEPS}
    assert names <= set(SWEEPS)
    # Per-process in-memory stores under mock mode: these would mark every connection
    # "reconnect" and lose transcripts when run from a separate process.
    assert not names & {"health", "transcript_archive"}
    assert {"session_finalize", "deferred"} <= names


def test_every_sweep_is_due_first_and_then_only_after_its_interval() -> None:
    scheduler = _load("scheduler")
    everything = list(scheduler.LOCAL_SWEEPS)
    assert scheduler.due({}, now=0.0) == everything
    just_ran = {s.name: 1000.0 for s in everything}
    assert scheduler.due(just_ran, now=1000.0) == []
    five_minutes_later = scheduler.due(just_ran, now=1000.0 + 5 * 60)
    assert {s.name for s in five_minutes_later} == {"session_finalize", "deferred"}


# --- scripts/local/seed_demo.py ----------------------------------------------------- #


@pytest.mark.parametrize(
    ("environment", "mode"), [("dev", "mock"), ("prod", "live"), ("development", "live")]
)
def test_the_seeder_refuses_anything_but_a_developer_computer(
    monkeypatch: pytest.MonkeyPatch, environment: str, mode: str
) -> None:
    seed = _load("seed_demo")
    monkeypatch.setenv("LUCIEL_ENVIRONMENT", environment)
    monkeypatch.setenv("LUCIEL_INTEGRATION_MODE", mode)
    with pytest.raises(SystemExit, match="only runs on a developer's own computer"):
        seed.require_local()


def test_the_demo_knowledge_is_three_titled_documents_about_a_fictional_business() -> None:
    docs = sorted((LOCAL / "demo_knowledge").glob("*.md"))
    assert len(docs) == 3
    titles = set()
    for doc in docs:
        text = doc.read_text(encoding="utf-8")
        assert text.startswith("# Maple Lane Dental")
        assert "fictional business" in text
        titles.add(text.splitlines()[0])
    assert len(titles) == 3  # the seeder skips a document whose title already exists


def test_the_demo_password_is_long_enough_for_the_sign_in_form() -> None:
    """The dashboard's sign-up and reset forms need eight characters or more."""
    assert len(_load("seed_demo").PASSWORD) >= 8
